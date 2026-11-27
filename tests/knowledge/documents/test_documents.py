"""B26 冒烟测试：专家资料入库、按专家隔离检索、图片结构化描述、来源等级排序、资产激活率与历史工单挖掘。

全部离线：真实 Nexent 检索与 deepseek-flash 调用不进默认测试；图片识读注入 transport 替身，
仓库里的描述文件是真实调用产物（见 data/docs 与 PR_BODY）。工单挖掘用 RuleBasedInterpreter，
Agent 解读走真实平台时另记轨迹。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plantkg.core.contracts import Actor, Context, ErrorCode, PlantkgError, Role, TOOL_TEXT_MAX_BYTES
from plantkg.core.registry import Registry
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
from plantkg.knowledge.documents import (
    LEVEL_LABEL,
    AssetCandidateStore,
    Hit,
    RuleBasedInterpreter,
    SourceLevel,
    apply_activations,
    describe_image,
    ingest,
    ingest_all,
    install_document_tools,
    load_description,
    load_library,
    make_document_paths_hook,
    mine,
    publish,
    report,
    search,
    write_description,
)
from plantkg.knowledge.documents.retrieval import rank
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

ROOT = Path(__file__).resolve().parents[3]
PLANT = "demo-electronics"
AS_OF = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(writer).apply()
    catalog = SQLiteAssetCatalog(writer, PLANT, tmp_path / "blobs", base_dir=ROOT)
    context = Context(plant_id=PLANT, industry_pack="manufacturing",
                      actor=Actor(actor_id="tester", roles=(Role.operator,)), channel="offline", trace_id="t-b26", as_of=AS_OF)
    # 不读 nexent_assets/knowledge/indexes.json：测试只依赖仓库清单与派生注入路径
    library = load_library(ROOT, indexes_path=tmp_path / "no-upload.json")
    yield catalog, context, library
    writer.close()


def test_ingest_returns_versioned_asset_and_marks_identified(env):
    catalog, context, library = env
    document = library.document("elevator-maintenance-manual")
    item = ingest(catalog, context, document)
    assert item.asset.asset_type == "process_doc" and item.asset.nature == "synthetic"
    assert item.asset.asset_version_id.startswith(f"asset://manufacturing/{item.asset.content_hash[7:15]}/process_doc/")
    assert catalog.activation(context, item.asset.asset_version_id) == "identified"
    assert item.description is None


def test_elevator_expert_cannot_reach_hvac_documents(env):
    _, _, library = env
    elevator_paths = library.paths("elevator", AS_OF)
    hvac_paths = set(library.paths("hvac", AS_OF))
    assert elevator_paths and hvac_paths
    assert not set(elevator_paths) & hvac_paths
    assert all("hvac" not in path for path in elevator_paths)

    # 同一句暖通问题：电梯专家 0 条，暖通专家有结果，且没有跨专家命中
    assert search(library, "elevator", "冷冻水 预冷 设定温度", as_of=AS_OF) == []
    hvac_hits = search(library, "hvac", "冷冻水 预冷 设定温度", as_of=AS_OF)
    assert hvac_hits and {hit.document.expert for hit in hvac_hits} == {"hvac"}
    assert search(library, "hvac", "困人 救援 盘车", as_of=AS_OF) == []

    # 完成验收：电梯专家能引用自己库里的规程原文
    hits = search(library, "elevator", "困人 救援 盘车", as_of=AS_OF)
    assert hits[0].document.document_id == "elevator-safety-procedure"
    assert "按盘车规程救援" in hits[0].text and hits[0].level == SourceLevel.national_standard


def test_document_paths_hook_injects_only_the_experts_own_library(env):
    _, _, library = env
    hook = make_document_paths_hook(library, strict=False)
    elevator = SimpleNamespace(knowledge=object(), domain="elevator")
    paths = hook(elevator, AS_OF)
    assert paths and all(path.startswith("knowledge_base/elevator-") for path in paths)
    assert hook(SimpleNamespace(knowledge=None, domain="elevator"), AS_OF) is None
    assert hook(SimpleNamespace(knowledge=object(), domain="lighting"), AS_OF) == ()


def test_nameplate_image_is_turned_into_structured_description(env, tmp_path):
    catalog, context, library = env

    class FakeVision:
        def __init__(self):
            self.bodies: list[dict] = []

        def __call__(self, body):
            self.bodies.append(body)
            fields = {"kw": "0.37", "hp": "0.5", "rpm": "2730", "hz": "50", "voltage_v": "230/400",
                      "current_a": "1.67/0.97", "ip": "55", "ins_class": "F", "frame": "MS 711-2", "connection": "Δ/Y",
                      "serial_no": "1212073091"}
            return {"choices": [{"message": {"content": json.dumps(fields, ensure_ascii=False)}}]}

    vision = FakeVision()
    image = library.resolve(library.document("compressor-fan-nameplate"))
    description = describe_image(image, document_id="compressor-fan-nameplate", kind="nameplate", transport=vision)
    assert description.fields["kw"] == "0.37" and description.fields["serial_no"] == "1212073091"
    assert "0.37" in description.text
    assert "data:image/jpeg;base64," in vision.bodies[0]["messages"][0]["content"][0]["image_url"]["url"]

    written = write_description(description, tmp_path / "nameplate.md")
    front = load_description(written)
    assert front["model"] == "deepseek-v4.1-flash-1017" and front["fields"]["kw"] == "0.37"

    document = library.document("compressor-fan-nameplate").model_copy(update={"description_path": str(written)})
    item = ingest(catalog, context, document)
    assert item.asset.asset_type == "image" and item.description is not None
    assert item.description.asset_type == "process_doc" and item.description.nature == "derived"
    assert "0.37" in catalog.read_content(context, item.description.asset_version_id).decode("utf-8")


def test_retrieval_ranks_relevance_first_then_source_level(env):
    _, _, library = env
    manual = library.document("elevator-maintenance-manual")
    standard = library.document("elevator-safety-procedure")
    same = [Hit(manual, "3.2", "困人救援", 0.5, SourceLevel.equipment_manual),
            Hit(standard, "GB-5.12.1", "困人救援", 0.5, SourceLevel.national_standard)]
    assert rank(same)[0].level == SourceLevel.national_standard
    more_relevant = [Hit(manual, "3.2", "困人救援", 0.9, SourceLevel.equipment_manual),
                     Hit(standard, "GB-5.12.1", "困人救援", 0.5, SourceLevel.national_standard)]
    assert rank(more_relevant)[0].document.document_id == manual.document_id
    assert LEVEL_LABEL[SourceLevel.national_standard] == "国家标准/政策"


def test_as_of_filters_documents(env):
    _, _, library = env
    early = datetime(2025, 5, 10, tzinfo=timezone.utc)
    assert {doc.document_id for doc in library.documents("energy", early)} == {"js-2025-426-policy", "tariff-summary-2025"}
    assert len(library.documents("energy", AS_OF)) == 4


def test_activation_moves_dormant_materials_and_reports_rate(env):
    catalog, context, library = env
    ingested = ingest_all(catalog, context, library)
    before = report(catalog, context, ingested)
    assert before["by_stage"]["dormant"] == 5 and before["referenced_rate"] == 0

    applied = apply_activations(catalog, context, library, ingested)
    after = report(catalog, context, ingested)
    assert applied and after["by_stage"]["referenced"] == 11
    assert after["referenced_rate"] == pytest.approx(11 / 17)
    stages = {row["document_id"]: row["stage"] for row in after["documents"]}
    assert stages["compressor-work-orders"] == "referenced"  # 沉睡 → 已标识 → 已入图 → 被引用
    assert stages["hvac-maintenance-history"] == "dormant"  # 还没人看的资料保持沉睡
    assert after["catalog_summary"]["total"] == 19  # 17 份资料 + 2 份图片描述


def test_repeated_work_orders_cluster_into_one_class_and_rule_cites_originals(env, tmp_path):
    catalog, context, library = env
    result = mine(library, as_of=AS_OF, interpreter=RuleBasedInterpreter(), created_at=AS_OF)
    assert result.cluster_count == 4 and result.represented > 0
    ac2 = next(cluster for cluster in result.clusters if cluster.equipment == "AC-2")
    assert ac2.count == 6 and len({order.phenomenon for order in ac2.orders}) == 1
    assert result.candidate_count == 1 and result.represented == 6
    record = result.records[0]
    assert record.reading.is_pattern and record.reading.kind == "warning"
    assert all(order.order_id in record.reading.rule_text for order in ac2.orders)
    assert {order.order_id for order in record.citations} == set(ac2.citations)

    published = publish(result, store=AssetCandidateStore(catalog, context, tmp_path / "candidates"))
    assert published.adopted_count == 1
    asset_id = published.records[0].candidate_asset_version_id
    assert asset_id and asset_id.startswith("asset://manufacturing/")
    body = json.loads(catalog.read_content(context, asset_id).decode("utf-8"))
    assert body["candidate"]["candidate_id"] == record.candidate.candidate_id
    assert {citation["order_id"] for citation in body["citations"]} == set(ac2.citations)
    assert body["reading"]["nature"] == "synthetic"
    # B30 接收原工单候选资产，保留每张工单来源，仍须补齐结构化条件并由人复核。
    from plantkg.core.contracts import AssetFragment, Locator
    from plantkg.knowledge.rules.extract import DocumentRuleExtractor
    from plantkg.knowledge.rules.compile import approve_rule
    fragment = AssetFragment(fragment_id="b26-candidate", asset_version_id=asset_id, locator=Locator(line_start=1),
                             kind="text", text=json.dumps(body, ensure_ascii=False), available_at=context.as_of)
    candidate = DocumentRuleExtractor().extract(context, [fragment])[0]
    assert candidate.draft["original_candidate_id"] == record.candidate.candidate_id
    assert {citation["order_id"] for citation in candidate.draft["workorder_citations"]} == set(ac2.citations)
    assert candidate.draft["missing"]
    with pytest.raises(PlantkgError):
        approve_rule(candidate, Actor(actor_id="reviewer", roles=(Role.reviewer,)), subject_id="AC-2",
                     effective_from=context.as_of, nature="derived")


def test_publisher_port_is_used_when_registered(env):
    _, _, library = env
    result = mine(library, as_of=AS_OF, created_at=AS_OF)
    publisher = SimpleNamespace(candidates=[], propose_publish=lambda candidate: (
        publisher.candidates.append(candidate) or SimpleNamespace(action_id="pa-b26-1")))
    published = publish(result, publisher=publisher)
    assert published.adopted_count == 1 and published.records[0].publish_action_id == "pa-b26-1"
    candidate = publisher.candidates[0]
    assert candidate.kind == "knowledge" and candidate.status == "proposed"
    assert candidate.changes["rules"].ref.startswith("rules@b26-workorder-")


def test_unsupported_legacy_workorder_candidate_falls_back_to_asset(env, tmp_path):
    catalog, context, library = env
    result = mine(library, as_of=AS_OF, created_at=AS_OF)
    publisher = SimpleNamespace(supports_candidate=lambda candidate: False)
    saved = publish(result, publisher=publisher, store=AssetCandidateStore(catalog, context, tmp_path / "candidates"))
    assert saved.records[0].candidate_asset_version_id
    assert saved.records[0].publish_action_id is None
    with pytest.raises(PlantkgError):
        publish(result, publisher=publisher)


def test_workorder_search_is_registered_and_filters(env):
    _, context, library = env
    registry = Registry()
    install_document_tools(registry, library)
    assert "workorder_search" in registry.tools
    handler = registry.tools["workorder_search"].handler
    result = handler(context, equipment="AC-2", from_time="2026-04-01", to_time="2026-06-30")
    assert result.ok and result.data["total"] == 3 and result.data["matched_clusters"] == 1
    assert {item["equipment"] for item in result.data["items"]} == {"AC-2"}
    first = handler(context, equipment="AC-2", page_size=2)
    assert len(first.data["items"]) == 2 and first.page.next_cursor
    second = handler(context, equipment="AC-2", cursor=first.page.next_cursor, page_size=2)
    assert len(second.data["items"]) == 2 and second.data["total"] == 6
    leaks = handler(context, phenomenon="漏气")
    assert leaks.data["total"] == 2 and {item["equipment"] for item in leaks.data["items"]} == {"AIR-BR-P"}


def test_old_procedure_has_earlier_version_and_old_threshold(env):
    _, _, library = env
    old = library.document("compressor-station-procedure-2024")
    new = library.document("compressor-maintenance-manual")
    assert old.available_at < new.available_at and old.nature == "synthetic"
    assert "0.02 MPa" in library.resolve(old).read_text(encoding="utf-8")
    assert "0.05 MPa" in library.resolve(new).read_text(encoding="utf-8")
    assert old.document_id in {document.document_id for document in library.documents("equipment")}
    assert library.injection_path(old).endswith("compressor-station-procedure-2024.md")


def test_workorder_search_and_mine_respect_as_of(env):
    _, context, library = env
    registry = Registry()
    install_document_tools(registry, library)
    handler = registry.tools["workorder_search"].handler
    early = datetime(2026, 6, 1, tzinfo=timezone.utc)  # 工单表 2026-06-19 才可见
    result = handler(context.model_copy(update={"as_of": early}))
    assert result.data["total"] == 0 and result.data["matched_clusters"] == 0
    assert mine(library, as_of=early, created_at=early).cluster_count == 0
    assert handler(context).data["total"] == 11  # AS_OF 下全部可见


def test_workorder_search_page_size_matches_contract(env):
    _, context, library = env
    registry = Registry()
    install_document_tools(registry, library)
    handler = registry.tools["workorder_search"].handler
    for size in (20, 21, 50):
        assert handler(context, page_size=size).ok
    with pytest.raises(PlantkgError) as exc:
        handler(context, page_size=51)
    assert exc.value.code == ErrorCode.INVALID_REQUEST
    assert len(handler(context, page_size=50).to_agent_text().encode("utf-8")) <= TOOL_TEXT_MAX_BYTES


def test_strict_paths_require_upload_results(env, tmp_path):
    _, _, library = env
    with pytest.raises(PlantkgError) as exc:
        library.paths("elevator", AS_OF, strict=True)
    assert exc.value.code == ErrorCode.PRECONDITION

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"created_at": "2026-11-05T00:00:00+00:00", "documents": {
        "elevator-maintenance-manual": {"nexent_path": "knowledge_base/manual.md", "status": "COMPLETED"}}}), encoding="utf-8")
    partial_library = load_library(ROOT, indexes_path=partial)
    with pytest.raises(PlantkgError) as exc:
        partial_library.paths("elevator", AS_OF, strict=True)
    assert "elevator-safety-procedure" in exc.value.message  # 未上传的文档不会被静默剔除
    assert partial_library.paths("elevator", AS_OF, strict=False) == ("knowledge_base/manual.md",)


def test_pdf_opt_out_is_not_required_by_strict_paths(env, tmp_path):
    _, _, library = env
    indexes = {"created_at": "2026-11-05T00:00:00+00:00", "documents": {
        document.document_id: {"nexent_path": f"knowledge_base/{document.document_id}.md", "status": "COMPLETED"}
        for document in library.documents() if document.kb_upload}}
    path = tmp_path / "indexes.json"
    path.write_text(json.dumps(indexes), encoding="utf-8")
    uploaded = load_library(ROOT, indexes_path=path)
    energy_paths = uploaded.paths("energy", AS_OF, strict=True)
    assert len(energy_paths) == 3 and all("js-2025-426" not in item for item in energy_paths)


def test_manifest_documents_and_image_descriptions_exist(env):
    _, _, library = env
    for document in library.documents():
        assert library.resolve(document).is_file(), document.path
        if document.kind == "image":
            assert library.root.joinpath(document.description_path or "").is_file()
            assert load_description(library.root / (document.description_path or ""))["fields"]
