"""B30 第二轮回归：规则表示、消费端契约、实体归一、稳定游标与候选级幂等（#175）。

全部经默认装配（build_services 的真实 Sqlite 存储、B30 工具与钩子）走一遍。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, Interval, Locator, PlantkgError, Role, Rule
from plantkg.core.ports import AgentRunReader, AssetRepository, Gateway, JobRunner, PendingActionStore, VersionStore
from plantkg.experts.energy.planner import load_scenario_packs, published_tariff_price, with_published_tariff
from plantkg.experts.equipment.source import apply_published_rule
from plantkg.experts.elevator.pack import _apply_published_rule
from plantkg.experts.hvac.expert import HvacExpert
from plantkg.experts.hvac.install import _knowledge_expert
from plantkg.knowledge.rules.normalise import consumer_mismatch, unresolved_entity
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.packs.tariff import price_schedule
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))
PLANT = "demo-electronics"
ELEVATOR_FIXTURE = ROOT / "plantkg/knowledge/rules/fixtures/elevator-dispatch.md"
TARIFF_DOC = ROOT / "data/docs/energy/tariff-summary-2025.md"


def _setup():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-round2", as_of=AT)
    return services, settings, context


def _ingest(registry, context, tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return registry.resolve(AssetRepository).ingest(context, str(path), "process_doc", AT, nature="synthetic")


def _review(registry, context, candidate_id, revision):
    actions = registry.resolve(PendingActionStore)
    decision = registry.tools["propose_review_decision"].handler(
        context, review_id=candidate_id, decision="revise", revision=revision, rationale="测试复核")
    key = "review-" + candidate_id + "-" + json.dumps(revision, sort_keys=True, ensure_ascii=False)
    actions.confirm(decision.data["action_id"], REVIEWER, "确认复核", 0, key)


def _publish(registry, context, candidate_id):
    actions = registry.resolve(PendingActionStore)
    publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="测试发布")
    actions.confirm(publish.data["action_id"], REVIEWER, "确认发布", 0, f"publish-{candidate_id}")
    return publish


def _activate_rules(registry, rules, label):
    versions = registry.resolve(VersionStore)
    artifacts = RuleArtifacts(versions.writer, versions.plant_id)
    component = artifacts.prepare(list(rules), REVIEWER)
    manifest = versions.prepare({"rules": component}, label)
    versions.check_integrity(manifest.version_manifest_id)
    try:
        current = versions.active().version_manifest_id
    except PlantkgError:
        current = None
    versions.activate(current, manifest.version_manifest_id, REVIEWER, label)
    return manifest.version_manifest_id


def _rule(rule_id, kind, subjects, **updates):
    base = {"rule_id": rule_id, "kind": kind, "subject_ids": subjects, "quantity": "pressure_drop_rate", "unit": "bar/h",
            "value": -0.3, "effective_from": AT - timedelta(days=1), "available_at": AT - timedelta(days=1),
            "source_asset_version": "asset://demo-electronics/00000000/process/rule", "locator": Locator(line_start=1),
            "nature": "synthetic", "review_status": "approved", "reviewer": "reviewer"}
    return Rule(**{**base, **updates})


def test_elevator_same_text_does_not_conflict_and_group_publish_applies(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        _activate_rules(registry, [_rule("baseline", "operation_window",
                                         (f"entity://{settings.plant_id}/air_compressor/AC-1",),
                                         quantity="operation_start", unit="h", value=None, bounds={"lower": 0, "upper": 1})],
                        "基线规则")
        text = ELEVATOR_FIXTURE.read_text(encoding="utf-8")
        first = _ingest(registry, context, tmp_path, "elevator-a.md", text)
        second = _ingest(registry, context, tmp_path, "elevator-b.md", text)
        candidate = registry.tools["extract_rules"].handler(context, asset_version_id=first.asset_version_id).data["candidate_ids"][0]
        group = f"entity://{settings.plant_id}/elevator_group/admin_low"
        _review(registry, context, candidate, {"subject_id": group, "effective_from": AT.isoformat()})
        _publish(registry, context, candidate)

        # 同文重复上传：电梯候选不再被误判 conflicting_rule（#96）
        repeated = registry.tools["extract_rules"].handler(context, asset_version_id=second.asset_version_id)
        assert "conflicting_rule" not in repeated.data["routing"][0]["reasons"]

        # 群 ID 发布后，专家读取经 B30 映射应用（#143）
        manifest_id = registry.resolve(VersionStore).active().version_manifest_id
        state = {}
        _apply_published_rule(registry, manifest_id, AT, AT, {"entity_id": group}, state)
        assert state["published_elevator_rule"]["rule_id"] == candidate
    finally:
        services.close()


def test_single_elevator_subject_is_rejected_as_consumer_mismatch(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        _activate_rules(registry, [_rule("baseline", "operation_window",
                                         (f"entity://{settings.plant_id}/air_compressor/AC-1",),
                                         quantity="operation_start", unit="h", value=None, bounds={"lower": 0, "upper": 1})],
                        "基线规则")
        text = ELEVATOR_FIXTURE.read_text(encoding="utf-8")
        asset = _ingest(registry, context, tmp_path, "elevator.md", text)
        candidate = registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]
        single = f"entity://{settings.plant_id}/passenger_elevator/PE-1"
        _review(registry, context, candidate, {"subject_id": single, "effective_from": AT.isoformat()})
        with pytest.raises(PlantkgError) as error:
            _publish(registry, context, candidate)
        assert error.value.code == ErrorCode.PRECONDITION and "consumer_mismatch" in str(error.value)
        from plantkg.knowledge.rules.store import CandidateStore
        versions = registry.resolve(VersionStore)
        route = CandidateStore(versions.writer, settings.plant_id).route(candidate)
        assert "consumer_mismatch" in route["reasons"]

        # 改成电梯群主体后可正常发布
        group = f"entity://{settings.plant_id}/elevator_group/admin_low"
        _review(registry, context, candidate, {"subject_id": group, "effective_from": AT.isoformat()})
        _publish(registry, context, candidate)
        assert registry.resolve(VersionStore).active().components.get("rules") is not None
    finally:
        services.close()


def test_published_leak_rule_matches_air_system_and_graph_entity(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        air_system = f"entity://{settings.plant_id}/air_system/AIR"
        air_header = f"entity://{settings.plant_id}/air_header/AIR-HDR"
        manifest_id = _activate_rules(registry, [_rule("leak-air-header", "compressor_pressure_drop", (air_header,))], "泄漏规则-图谱主体")
        for state in ({"ids": {"air_system": air_system}}, {"entity_id": air_header}):
            apply_published_rule(registry, manifest_id, AT, AT, state)
            assert state["published_leak_rule"]["rule_id"] == "leak-air-header"
        untouched = {"entity_id": f"entity://{settings.plant_id}/air_compressor/AC-2"}
        apply_published_rule(registry, manifest_id, AT, AT, untouched)
        assert "published_leak_rule" not in untouched

        manifest_id = _activate_rules(registry, [_rule("leak-air-system", "compressor_pressure_drop", (air_system,))], "泄漏规则-聚合主体")
        state = {"entity_id": air_header}
        apply_published_rule(registry, manifest_id, AT, AT, state)
        assert state["published_leak_rule"]["rule_id"] == "leak-air-system"
    finally:
        services.close()


def test_published_zone_temperature_rule_changes_hvac_comfort(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        rule = _rule("comfort-admin", "zone_temperature_limit", (f"entity://{settings.plant_id}/zone/admin",),
                     quantity="temperature", unit="degC", value=None, bounds={"lower": 23.0, "upper": 25.0})
        manifest_id = _activate_rules(registry, [rule], "暖通规则")
        expert, refs = _knowledge_expert(registry, HvacExpert(), manifest_id, AT)
        assert expert.zones["admin"].comfort_c == (23.0, 25.0)
        assert refs == ("rule:comfort-admin",)
    finally:
        services.close()


def test_published_tariff_rule_matches_scenario_grid_entity(tmp_path):
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    packs = load_scenario_packs(scenario)
    slot = Interval(start=AT, end=AT + timedelta(hours=1))
    rule = _rule("tariff-grid", "tariff_tou", (packs.tariff.grid_entity,), quantity="tou_factor", unit="1", value=1.0,
                 bounds=None, condition={"category": packs.tariff.category, "timezone": "Asia/Shanghai", "periods": []})
    replaced = with_published_tariff(packs, [rule], slot)
    assert any(item.rule_id == "tariff-grid" for item in replaced.rules)
    wrong = _rule("tariff-wrong", "tariff_tou", (packs.tariff.grid_entity,), quantity="tou_factor", unit="1", value=1.0,
                  bounds=None, condition={"category": "other", "timezone": "Asia/Shanghai", "periods": []})
    assert with_published_tariff(packs, [wrong], slot) is packs


def test_review_cursor_does_not_skip_items_after_reviewing(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        candidate_ids = []
        for index in range(15):
            text = f"主管压力 10 分钟内下降超过 0.{(index + 1) * 3:02d} MPa、且空压机仍在持续供气时，按泄漏处理。"
            asset = _ingest(registry, context, tmp_path, f"leak-{index}.md", text)
            extracted = registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id)
            candidate_ids.append(extracted.data["candidate_ids"][0])
        first = registry.tools["next_review_batch"].handler(context, page_size=5)
        assert len(first.data["items"]) == 5 and first.page.next_cursor
        for item in first.data["items"]:
            _review(registry, context, item["candidate_id"],
                    {"subject_id": f"entity://{settings.plant_id}/air_header/AIR-HDR", "effective_from": AT.isoformat()})
        second = registry.tools["next_review_batch"].handler(context, cursor=first.page.next_cursor, page_size=10)
        assert len(second.data["items"]) == 10 and second.page.next_cursor is None
        reviewed = {item["candidate_id"] for item in first.data["items"]}
        remaining = {item["candidate_id"] for item in second.data["items"]}
        assert reviewed.isdisjoint(remaining) and reviewed | remaining == set(candidate_ids)
    finally:
        services.close()


def test_batch_rejects_candidate_in_another_batch_and_counts_documents(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        reviewer_context = context.model_copy(update={"actor": REVIEWER})
        single_doc = "办公室温度24±1 ℃。\n主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时按泄漏处理。"
        asset = _ingest(registry, context, tmp_path, "combined.md", single_doc)
        extracted = registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id)
        assert len(extracted.data["candidate_ids"]) == 2
        for candidate_id in extracted.data["candidate_ids"]:
            _review(registry, context, candidate_id,
                    {"subject_id": f"entity://{settings.plant_id}/air_header/AIR-HDR", "effective_from": AT.isoformat()})
        first = registry.tools["record_onboarding_batch"].handler(
            reviewer_context, batch_id="batch-1", candidate_ids=extracted.data["candidate_ids"],
            review_minutes=1.0, tokens=10, elapsed_seconds=60.0)
        assert first.data["items"] == 2 and first.data["document_count"] == 1

        other = _ingest(registry, context, tmp_path, "other.md", "主管压力 10 分钟内下降超过 0.07 MPa 时检查。")
        other_id = registry.tools["extract_rules"].handler(context, asset_version_id=other.asset_version_id).data["candidate_ids"][0]
        _review(registry, context, other_id,
                {"subject_id": f"entity://{settings.plant_id}/air_header/AIR-HDR", "effective_from": AT.isoformat()})
        with pytest.raises(PlantkgError) as error:
            registry.tools["record_onboarding_batch"].handler(
                reviewer_context, batch_id="batch-2", candidate_ids=[extracted.data["candidate_ids"][0], other_id],
                review_minutes=1.0, tokens=10, elapsed_seconds=60.0)
        assert error.value.code == ErrorCode.STATE_CONFLICT and "batch-1" in str(error.value)
    finally:
        services.close()


def test_investigation_is_idempotent_per_candidate_and_manifest(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        asset = _ingest(registry, context, tmp_path, "exception.md",
                        "主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时，按泄漏处理。")
        candidate = registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]
        assert registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id).data["routing"][0]["stage"] == "agent"
        first = registry.tools["process_onboarding"].handler(context, asset_version_id=asset.asset_version_id)
        second = registry.tools["process_onboarding"].handler(
            context.model_copy(update={"trace_id": "b30-round2-again"}),
            asset_version_id=asset.asset_version_id)
        first_jobs = {item["candidate_id"]: item["job_id"] for item in first.data["investigation_jobs"]}
        second_jobs = {item["candidate_id"]: item["job_id"] for item in second.data["investigation_jobs"]}
        assert first_jobs == second_jobs
        registry.resolve(JobRunner).get(first_jobs[candidate], wait_seconds=10)
        items = registry.tools["onboarding_investigations"].handler(context, candidate_id=candidate).data["items"]
        assert len(items) == 1
    finally:
        services.close()


def test_conflicting_tariff_change_reports_conflict_and_prior_rules(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        text = TARIFF_DOC.read_text(encoding="utf-8")
        old_asset = _ingest(registry, context, tmp_path, "tariff-old.md", text)
        old_rule = _rule(
            "old-tariff", "tariff_tou", (f"entity://{settings.plant_id}/grid_connection/MAIN",),
            quantity="tou_factor", unit="1", value=1.0, bounds=None,
            source_asset_version=old_asset.asset_version_id,
            condition={"category": "two_part_industrial_ge315kva", "timezone": "Asia/Shanghai",
                       "periods": [{"name": "peak", "ratio": 0.8}, {"name": "flat", "ratio": 0.0},
                                   {"name": "valley", "ratio": -0.65}, {"name": "sharp", "ratio": 0.2}]})
        _activate_rules(registry, [old_rule], "旧电价规程")
        bound = context.model_copy(update={"version_manifest_id": registry.resolve(VersionStore).active().version_manifest_id})

        same = _ingest(registry, context, tmp_path, "tariff-same.md", text)
        unchanged = registry.tools["extract_rules"].handler(bound, asset_version_id=same.asset_version_id)
        assert "conflicting_rule" not in unchanged.data["routing"][0]["reasons"]

        changed_asset = _ingest(registry, context, tmp_path, "tariff-new.md", text.replace("峰段上浮 80%", "峰段上浮 90%"))
        changed = registry.tools["extract_rules"].handler(bound, asset_version_id=changed_asset.asset_version_id)
        assert "conflicting_rule" in changed.data["routing"][0]["reasons"]
        candidate_id = changed.data["candidate_ids"][0]

        prompts = []
        class FakeGateway:
            async def run(self, actor, query, agent_name, as_of=None, **options):
                prompts.append(query)
                yield SimpleNamespace(agent_run_id="agent-tariff")
        class FakeReader:
            def get(self, agent_run_id):
                return SimpleNamespace(status="finished", final_answer=json.dumps(
                    {"decision": "pending", "rationale": "待核对新旧电价", "citations": [changed_asset.asset_version_id]}))
        registry.ports[Gateway] = FakeGateway()
        registry.ports[AgentRunReader] = FakeReader()
        submitted = registry.tools["investigate_onboarding"].handler(bound, candidate_id=candidate_id)
        job = registry.resolve(JobRunner).get(submitted.job_id, wait_seconds=10)
        assert job.status == "succeeded", job.error
        prompt = json.loads(prompts[0][prompts[0].index("{"):])
        assert {"old-tariff"} <= {item["rule_id"] for item in prompt["prior_rules"]}
    finally:
        services.close()


def test_device_subject_temperature_rule_maps_to_graph_zone(tmp_path):
    """设备主体（CR-AHU1）的暖通规则按图谱区域生效；映射不到区域的发布被 consumer_mismatch 拒绝。"""
    services, settings, context = _setup()
    try:
        registry = services.registry
        entity_type = registry.hooks_for("router.entity_type")[-1]
        zone_of = registry.hooks_for("router.entity_zone")[-1]
        rule = _rule("comfort-cleanroom", "zone_temperature_limit", (f"entity://{settings.plant_id}/hvac/CR-AHU1",),
                     quantity="temperature", unit="degC", value=None, bounds={"lower": 21.0, "upper": 23.0})
        assert consumer_mismatch(rule, entity_type, zone_of) is None
        unmapped = rule.model_copy(update={"subject_ids": (f"entity://{settings.plant_id}/grid/GRID",)})
        assert "不能映射到区域" in consumer_mismatch(unmapped, entity_type, zone_of)

        manifest_id = _activate_rules(registry, [rule], "暖通设备主体规则")
        expert, refs = _knowledge_expert(registry, HvacExpert(), manifest_id, AT)
        assert expert.zones["cleanroom"].comfort_c == (21.0, 23.0)
        assert refs == ("rule:comfort-cleanroom",)
    finally:
        services.close()


def test_missing_graph_hook_is_unresolvable_for_triage_and_publish():
    """没有图谱钩子时，入库判别与发布期按同一口径把主体判为不可解析（#205 第 5 条）。"""
    subject = f"entity://{PLANT}/zone/admin"
    rule = _rule("comfort-admin", "zone_temperature_limit", (subject,),
                 quantity="temperature", unit="degC", value=None, bounds={"lower": 23.0, "upper": 25.0})
    assert unresolved_entity(None, subject) is True
    assert "不能被消费端解析" in consumer_mismatch(rule, None)


def test_zone_without_thermal_model_is_rejected_by_hvac_consumer():
    """图谱有、热模型没有的区域（hazmat/park）由暖通消费端钩子解析不到，按 consumer_mismatch 拒绝（#205）。"""
    services, settings, context = _setup()
    try:
        registry = services.registry
        entity_type = registry.hooks_for("router.entity_type")[-1]
        graph_zone = registry.hooks_for("router.entity_zone")[-1]
        hvac_zone = registry.hooks_for("hvac.consumer_zone")[-1]
        # router.entity_zone 回到图谱原义：设备节点按所在区域解析，区域主体本身不在图谱节点里（#241）
        assert graph_zone("HZ-EXF1") == "hazmat"
        assert graph_zone("CR-AHU1") == "cleanroom"
        assert graph_zone("zone:hazmat") is None
        # 热模型区域过滤只在暖通自己的钩子上：hazmat 解析不到，热模型有的区域照常
        assert hvac_zone("CR-AHU1") == "cleanroom"
        assert hvac_zone("HZ-EXF1") is None
        rule = _rule("comfort-hazmat", "zone_temperature_limit", (f"entity://{settings.plant_id}/zone/hazmat",),
                     quantity="temperature", unit="degC", value=None, bounds={"lower": 21.0, "upper": 23.0})
        assert "不能映射到区域" in consumer_mismatch(rule, entity_type, hvac_zone)
        assert consumer_mismatch(rule.model_copy(update={"subject_ids": (f"entity://{settings.plant_id}/zone/admin",)}),
                                 entity_type, hvac_zone) is None
    finally:
        services.close()


def test_tariff_subject_alias_replaces_config_and_prices_once():
    """已发布电价用等价主体写法（grid/GRID）时，配置规则按同一归一口径剔除，定价不再报多覆盖。"""
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    packs = load_scenario_packs(scenario)
    slot = Interval(start=scenario.instant("08:00"), end=scenario.instant("08:00") + timedelta(minutes=15))
    config_rule = next(rule for rule in packs.rules
                       if rule.kind == "tariff_tou" and rule.condition.get("category") == packs.tariff.category
                       and rule.effective_from <= slot.start)
    published = config_rule.model_copy(update={"subject_ids": (f"entity://{PLANT}/grid/GRID",)})
    replaced = with_published_tariff(packs, [published], slot)
    tariffs = [rule for rule in replaced.rules
               if rule.kind == "tariff_tou" and rule.condition.get("category") == packs.tariff.category]
    assert len(tariffs) == 1
    assert tariffs[0].subject_ids == (packs.tariff.grid_entity,)
    assert len(price_schedule(tariffs, replaced.tariff.base_prices, [slot])) == 1
    assert published_tariff_price(scenario, [published], slot) is not None

