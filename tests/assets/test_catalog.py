from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.core.contracts import Actor, Context, Entity, ErrorCode, PlantkgError, ToolResult
from plantkg.core.ports import AssetRepository
from plantkg.core.registry import Registry
from plantkg.knowledge.assets import ACTIVATION_NOTE, SQLiteAssetCatalog, register
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.testing import install_fakes

PLANT = "demo-electronics"
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


def ctx(as_of=T0 + timedelta(days=30), plant=PLANT, write=True):
    actor = Actor(actor_id="tester", roles=("operator",)) if write else None
    return Context(plant_id=plant, industry_pack="manufacturing", actor=actor, channel="offline" if write else "none",
                   trace_id="t-b06", as_of=as_of)


@pytest.fixture
def env(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(writer).apply()
    docs = tmp_path / "docs"
    docs.mkdir()
    catalog = SQLiteAssetCatalog(writer, PLANT, tmp_path / "blobs", base_dir=docs)
    yield catalog, docs, writer
    writer.close()


def write(docs, name, text):
    (docs / name).write_text(text, encoding="utf-8")
    return name


def battery(number, label):
    return Entity(entity_id=f"entity://{PLANT}/battery/{number}", entity_type="battery", label=label, plant_id=PLANT)


def test_same_content_returns_same_version(env):
    catalog, docs, _ = env
    write(docs, "tou-2026.md", "峰时 8:00-11:00")
    first, existed_first = catalog.ingest_detail(ctx(), "tou-2026.md", "policy", T0)
    second, existed_second = catalog.ingest_detail(ctx(), "tou-2026.md", "policy", T0)
    assert first == second and (existed_first, existed_second) == (False, True)
    assert first.asset_version_id == f"asset://manufacturing/{first.content_hash[7:15]}/policy/tou-2026"
    assert len(catalog.list(ctx())[0]) == 1


def test_same_bytes_elsewhere_deduplicate_by_full_hash(env):
    catalog, docs, _ = env
    write(docs, "a.md", "同一份内容")
    write(docs, "b.md", "同一份内容")
    assert catalog.ingest(ctx(), "a.md", "policy", T0) == catalog.ingest(ctx(), "b.md", "policy", T0)


def test_revision_creates_new_version_and_keeps_entity(env):
    catalog, docs, _ = env
    entity = catalog.register_entity(ctx(), battery("BESS1", "储能 1#"))
    write(docs, "bess.md", "额定 500 kWh")
    v1 = catalog.ingest(ctx(), "bess.md", "datasheet", T0, entity_ids=[entity.entity_id])
    write(docs, "bess.md", "额定 520 kWh（修订）")
    v2 = catalog.ingest(ctx(), "bess.md", "datasheet", T0 + timedelta(days=1))
    assert v2.asset_version_id != v1.asset_version_id
    assert v2.parent_version_id == v1.asset_version_id
    assert v2.entity_ids == v1.entity_ids == (entity.entity_id,)
    assert catalog.resolve_entity(ctx(), entity.entity_id) == entity
    assert catalog.lineage(ctx(), v2.asset_version_id) == [v1.asset_version_id]


def test_lineage_follows_latest_ingest_of_each_source(env):
    catalog, docs, _ = env
    write(docs, "spec.md", "A")
    v1 = catalog.ingest(ctx(), "spec.md", "datasheet", T0)
    write(docs, "spec.md", "B")
    catalog.ingest(ctx(), "spec.md", "datasheet", T0)
    write(docs, "spec.md", "A")
    assert catalog.ingest(ctx(), "spec.md", "datasheet", T0 + timedelta(days=1)) == v1  # 改回旧内容即旧版本
    write(docs, "spec.md", "C")
    assert catalog.ingest(ctx(), "spec.md", "datasheet", T0).parent_version_id == v1.asset_version_id
    write(docs, "copy.md", "A")
    catalog.ingest(ctx(), "copy.md", "datasheet", T0)  # 同字节另一路径：命中 v1，也记为 copy.md 的来源
    write(docs, "copy.md", "D")
    assert catalog.ingest(ctx(), "copy.md", "datasheet", T0).parent_version_id == v1.asset_version_id


def test_file_uri_forms(env):
    catalog, docs, _ = env
    write(docs, "uri.md", "x")
    absolute = (docs / "uri.md").resolve()
    a = catalog.ingest(ctx(), absolute.as_uri(), "policy", T0)
    assert catalog.ingest(ctx(), absolute.as_uri().replace("file://", "file://localhost"), "policy", T0) == a
    (docs / "datasets").mkdir()
    write(docs, "datasets/uri2.md", "y")
    assert catalog.ingest(ctx(), "file://datasets/uri2.md", "policy", T0).asset_version_id.endswith("/uri2")


def test_same_name_different_devices_not_merged(env):
    catalog, docs, _ = env
    a = catalog.register_entity(ctx(), battery("BESS1", "储能柜"))
    b = catalog.register_entity(ctx(), battery("BESS2", "储能柜"))
    assert catalog.find_entities(ctx(), "储能柜") == [a, b]
    write(docs, "cab1.md", "储能柜 1 号")
    write(docs, "cab2.md", "储能柜 2 号")
    v1 = catalog.ingest(ctx(), "cab1.md", "datasheet", T0, entity_ids=[a.entity_id])
    v2 = catalog.ingest(ctx(), "cab2.md", "datasheet", T0, entity_ids=[b.entity_id])
    assert v1.entity_ids != v2.entity_ids
    with pytest.raises(PlantkgError) as err:
        catalog.register_entity(ctx(), battery("BESS1", "另一个名字"))
    assert err.value.code == ErrorCode.IDEMPOTENCY_MISMATCH


def test_identical_datasheet_for_two_devices_keeps_both_links(env):
    catalog, docs, _ = env
    a = catalog.register_entity(ctx(), battery("BESS1", "储能 1#"))
    b = catalog.register_entity(ctx(), battery("BESS2", "储能 2#"))
    write(docs, "bess1.pdf", "同型号说明书")
    write(docs, "bess2.pdf", "同型号说明书")
    first = catalog.ingest(ctx(), "bess1.pdf", "datasheet", T0, entity_ids=[a.entity_id])
    later = T0 + timedelta(days=10)
    second = catalog.ingest(ctx(), "bess2.pdf", "datasheet", later, entity_ids=[b.entity_id])
    assert second.asset_version_id == first.asset_version_id
    assert second.entity_ids == (a.entity_id, b.entity_id)
    assert catalog.get(ctx(as_of=T0 + timedelta(days=1)), first.asset_version_id).entity_ids == (a.entity_id,)  # 新关联不回溯
    assert catalog.get(ctx(), first.asset_version_id).entity_ids == (a.entity_id, b.entity_id)


def test_inherited_links_keep_their_own_effective_time(env):
    catalog, docs, _ = env
    a = catalog.register_entity(ctx(), battery("BESS1", "储能 1#"))
    b = catalog.register_entity(ctx(), battery("BESS2", "储能 2#"))
    write(docs, "spec.md", "v1")
    v1 = catalog.ingest(ctx(), "spec.md", "datasheet", T0, entity_ids=[a.entity_id])
    write(docs, "copy.md", "v1")
    catalog.ingest(ctx(), "copy.md", "datasheet", T0 + timedelta(days=20), entity_ids=[b.entity_id])  # 第 20 天给 v1 追加 B
    write(docs, "spec.md", "v2")
    v2 = catalog.ingest(ctx(), "spec.md", "datasheet", T0 + timedelta(days=10))  # 第 10 天改版，继承关联
    day11 = ctx(as_of=T0 + timedelta(days=11))
    assert catalog.get(day11, v1.asset_version_id).entity_ids == (a.entity_id,)
    assert catalog.get(day11, v2.asset_version_id).entity_ids == (a.entity_id,)
    assert {x.asset_version_id: x.entity_ids for x in catalog.list(day11)[0]}[v2.asset_version_id] == (a.entity_id,)
    day21 = ctx(as_of=T0 + timedelta(days=21))
    assert catalog.get(day21, v2.asset_version_id).entity_ids == (a.entity_id, b.entity_id)
    assert catalog.get(ctx(as_of=T0 + timedelta(days=5)), v1.asset_version_id).entity_ids == (a.entity_id,)


def test_multiple_sources_same_device_share_one_entity(env):
    catalog, docs, _ = env
    entity = catalog.register_entity(ctx(), battery("BESS1", "储能 1#").model_copy(update={"aliases": ("BESS-1",)}))
    write(docs, "manual.md", "BESS-1 操作手册")
    write(docs, "ledger.csv", "BESS-1,2026-07-01,480")
    manual = catalog.ingest(ctx(), "manual.md", "process_doc", T0, entity_ids=[entity.entity_id])
    table = catalog.ingest(ctx(), "ledger.csv", "table", T0, entity_ids=[entity.entity_id])
    assert manual.entity_ids == table.entity_ids == (entity.entity_id,)
    assert catalog.find_entities(ctx(), "bess-1") == [entity]


def test_snapshot_survives_source_edit(env):
    catalog, docs, _ = env
    write(docs, "rule.md", "原文")
    asset = catalog.ingest(ctx(), "rule.md", "policy", T0)
    write(docs, "rule.md", "被改过")
    assert catalog.read_content(ctx(), asset.asset_version_id).decode("utf-8") == "原文"


def test_visibility_and_plant_isolation(env):
    catalog, docs, writer = env
    write(docs, "future.md", "下月生效")
    asset = catalog.ingest(ctx(), "future.md", "policy", T0 + timedelta(days=60))
    assert catalog.list(ctx())[0] == []
    with pytest.raises(PlantkgError) as err:
        catalog.get(ctx(), asset.asset_version_id)
    assert err.value.code == ErrorCode.NOT_VISIBLE
    assert catalog.get(ctx(as_of=T0 + timedelta(days=61)), asset.asset_version_id) == asset
    with pytest.raises(PlantkgError) as err:
        catalog.list(ctx(plant="other-plant"))
    assert err.value.code == ErrorCode.CROSS_PLANT
    other = SQLiteAssetCatalog(writer, "other-plant", catalog.blob_dir, base_dir=docs)
    assert other.list(ctx(as_of=T0 + timedelta(days=61), plant="other-plant"))[0] == []


def test_write_rules(env):
    catalog, docs, _ = env
    write(docs, "p.md", "x")
    with pytest.raises(PlantkgError) as err:
        catalog.ingest(ctx(write=False), "p.md", "policy", T0)
    assert err.value.code == ErrorCode.NO_CTX
    first = catalog.ingest(ctx(), "p.md", "policy", T0)
    assert catalog.ingest(ctx(), "p.md", "policy", T0 + timedelta(hours=1)) == first  # 内容早已可见，沿用首次时间
    with pytest.raises(PlantkgError) as err:
        catalog.ingest(ctx(), "p.md", "policy", T0, entity_ids=[f"entity://{PLANT}/battery/NOPE"])
    assert err.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(PlantkgError) as err:
        catalog.ingest(ctx(), "missing.md", "policy", T0)
    assert err.value.code == ErrorCode.NOT_FOUND


def test_activation_stages(env):
    catalog, docs, _ = env
    write(docs, "tou.md", "电价")
    write(docs, "idle.md", "没人用")
    asset = catalog.ingest(ctx(), "tou.md", "policy", T0)
    catalog.ingest(ctx(), "idle.md", "policy", T0)
    assert catalog.activation(ctx(), asset.asset_version_id) == "dormant"
    assert catalog.record_activation(ctx(), asset.asset_version_id, "in_graph") == "in_graph"
    with pytest.raises(PlantkgError):
        catalog.record_activation(ctx(), asset.asset_version_id, "referenced")
    later = T0 + timedelta(days=40)
    catalog.record_activation(ctx(), asset.asset_version_id, "referenced", ref="rule://tariff/peak", available_at=later)
    assert catalog.activation(ctx(), asset.asset_version_id) == "in_graph"  # 引用在 as_of 之后才可见
    summary = catalog.activation_summary(ctx(as_of=later))
    assert summary["by_stage"]["referenced"] == 1 and summary["by_stage"]["dormant"] == 1
    assert summary["referenced_rate"] == 0.5 and summary["note"] == ACTIVATION_NOTE
    catalog.record_activation(ctx(), asset.asset_version_id, "referenced", ref="rule://tariff/peak", available_at=T0)
    assert catalog.activation(ctx(), asset.asset_version_id) == "referenced"  # 回填更早时间生效
    write(docs, "next.md", "下季度政策")
    future = catalog.ingest(ctx(), "next.md", "policy", later)
    assert catalog.record_activation(ctx(), future.asset_version_id, "in_graph", available_at=later) == "in_graph"


def test_http_routes_serve_asset_ids_with_slashes(tmp_path):
    from urllib.parse import quote

    from starlette.testclient import TestClient

    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings

    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    catalog = services.registry.resolve(AssetRepository)
    assert isinstance(catalog, SQLiteAssetCatalog) and "list_assets" not in services.registry.fallbacks
    write(tmp_path, "tou.md", "峰谷电价")
    plant = services.settings.plant_id
    writer_ctx = ctx(plant=plant).model_copy(update={"industry_pack": services.settings.industry_pack})
    asset = catalog.ingest(writer_ctx, str(tmp_path / "tou.md"), "policy", datetime(2000, 1, 1, tzinfo=timezone.utc))
    write(tmp_path, "fragments.md", "名字恰好是 fragments 的资料")
    named = catalog.ingest(writer_ctx, str(tmp_path / "fragments.md"), "policy", datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert named.asset_version_id.endswith("/policy/fragments")
    with TestClient(create_app(services)) as client:
        assert len(client.get("/api/v1/assets").json()["items"]) == 2
        for target in (asset, named):  # 普通资料与 fragments.md 的明细都返回 200
            for path in (target.asset_version_id, quote(target.asset_version_id, safe="")):
                response = client.get(f"/api/v1/assets/{path}")
                assert response.status_code == 200 and response.json()["content_hash"] == target.content_hash
        for target in (asset, named):  # 真实片段由 B26 从 B06 不可变原文提供
            response = client.get(f"/api/v1/assets/{target.asset_version_id}/fragments")
            assert response.status_code == 200 and response.json()["items"]
        assert client.get("/api/v1/assets/asset://manufacturing/00000000/policy/nope").status_code == 404
        assert client.get("/api/v1/assets/not-an-id").status_code == 404


def test_tools_replace_fakes_and_paginate(env, tmp_path):
    catalog, docs, _ = env
    registry = Registry()
    install_fakes(registry, None)
    register(registry, catalog)
    assert registry.resolve(AssetRepository) is catalog
    assert "list_assets" not in registry.fallbacks and ("GET", "/assets") not in registry.fallbacks
    tools = {name: binding.handler for name, binding in registry.tools.items()}
    for i in range(3):
        write(docs, f"doc{i}.md", f"内容 {i}")
        result = tools["ingest"](ctx(), f"doc{i}.md", "policy", "2026-07-01T00:00:00Z")
        assert result.ok and result.data["deduplicated"] is False
    seen, cursor = [], None
    while True:
        page = tools["list_assets"](ctx(), cursor=cursor, page_size=2)
        seen += [item["asset_version_id"] for item in page.data["items"]]
        cursor = page.page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 3
    detail = tools["get_asset"](ctx(), seen[0])
    assert isinstance(detail, ToolResult) and detail.data["activation"] == "dormant"
    json.loads(detail.to_agent_text())
    rest = registry.rest[("GET", "/assets")](ctx(), {}, {"page_size": "50"})
    assert len(rest.items) == 3
