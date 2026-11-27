"""B27 验收冒烟：默认装配（build_services + defaults.yaml 的 packs）下 D55/D57/D59 真实生效，不手工注入能力。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from plantkg.agent.router import ROUTER_HOOK, URGENT_HOOK
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.world.sensors import ObservationBus
from tests.agent.router.helpers import T0, make_event

PLANT = "demo-electronics"


def entity(kind: str, code: str) -> str:
    return f"entity://{PLANT}/{kind}/{code}"


@pytest.fixture
def assembled(tmp_path):
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    urgent: list[str] = []
    services.registry.register_hook(URGENT_HOOK, lambda event, reason: urgent.append(reason))
    try:
        yield services.registry, services.registry.hooks_for(ROUTER_HOOK)[-1], urgent
    finally:
        ObservationBus.clear()
        services.close()


def test_default_assembly_severity_trial_graph_and_policy(assembled):
    registry, router, urgent = assembled
    assert router.policy.major_loss_cny == 50_000 and router.severity.major_loss_cny == 50_000

    # 未保护试算（B17）：突增 800 kW，TX1 连续超容，图谱（B25）下游产线与空压站停产，不是配置的产线清单
    surge = make_event("EV-TRIP", 30, "load_spike", entity("production_line", "LINE-1"), severity=4).model_copy(
        update={"payload": {"effects": [{"kind": "extra_load", "kw": 800, "minutes": 40}]}})
    handled = router.handle_event(surge)
    estimate = router.store.severity_of(handled.ticket_id)
    assert handled.channel == "urgent" and "跳闸 TX1" in urgent[0]
    assert estimate["production_stop"] is True and "AC-1" in estimate["basis"] and "LINE-3" in estimate["basis"]

    # 同样的事件没有突增：试算不超容，不派单
    calm = make_event("EV-CALM", 30, "load_spike", entity("production_line", "LINE-2"), severity=3).model_copy(
        update={"payload": {"effects": []}})
    assert router.handle_event(calm).channel == "fast"
    assert len(urgent) == 1

    # 图谱安全标记（B25）：事件类型不在安全清单，对象自身带标记即派单
    assert router.handle_event(make_event("EV-FAN", 40, "equipment_fault", entity("exhaust_fan", "HZ-EXF1"))).channel == "urgent"

    # 慢通道工具走同一个试算台；确定性调查走 find_sources 首个候选
    slow = router.handle_event(make_event("EV-AIR", 45, "unknown_combo", entity("air_compressor", "AC-2")))
    assert slow.channel == "slow"
    assert router.store.investigation(slow.ticket_id, 1)["evidence_refs"][0] != entity("air_compressor", "AC-2")
    result = registry.tools["trial"].handler(None, [], 30)
    assert result.ok and result.data["result"]["feasible"] in (True, False)
    json.dumps(result.data)


def test_default_assembly_readings_come_from_observation_layer(assembled):
    """观测层（B22）读数经 router.readings 进严重程度：残差报警走慢通道；没有运行中的场景时不编造。"""
    _, router, urgent = assembled
    start, end = T0 - timedelta(minutes=5), T0
    assert router.sensor_readings(entity("air_compressor", "AC-2"), start, end)[0]["quality"] == "unavailable"

    at = (T0 - timedelta(minutes=1)).isoformat()
    reading = {"sensor_id": "AC-2.I", "entity": "AC-2", "metric": "current_a", "value": 90.0, "at": at,
               "quality": {"flag": "suspect", "note": "残差报警"}}
    snapshot = {"at": T0.isoformat(), "readings": {"AC-2.I": reading}, "alarms": ["air_current_pressure"],
                "relations": [{"id": "air_current_pressure", "alarm": True, "sensors": ["AC-2.I"]}]}
    ObservationBus.publish(PLANT, snapshot, {"AC-2.I": [{"at": at, "value": 90.0, "quality": "suspect"}]})

    rows = router.sensor_readings(entity("air_compressor", "AC-2"), start, end)
    assert [(r["sensor_id"], r["quality"], r["residual_alarm"]) for r in rows] == [("AC-2.I", "suspect", True)]
    handled = router.handle_event(make_event("EV-S", 0, "compressor_pressure_drop", entity("air_compressor", "AC-2")))
    assert handled.channel == "slow" and urgent == []
    assert router.store.severity_of(handled.ticket_id)["field_check"] is True


def test_capacity_trip_reads_panel_power_history(assembled):
    """P2-5：过载判定用 B22 真实字段：变压器下配电柜功率读数连续达到限额比例（TX1 演示日限额 1150 kW）。"""
    registry, router, urgent = assembled
    capacity_trip = registry.hooks_for("router.capacity_trip")[-1]
    readings = {"P-PROD.P": {"entity": "P-PROD", "metric": "power_kw"}, "HVAC-P.P": {"entity": "P-HVAC-P", "metric": "power_kw"}}

    def publish(values):
        history = {"P-PROD.P": [{"at": T0.isoformat(), "value": v, "quality": "ok"} for v in values],
                   "HVAC-P.P": [{"at": T0.isoformat(), "value": 5000.0, "quality": "ok"} for _ in values]}  # 子柜不重复计入
        ObservationBus.publish(PLANT, {"at": T0.isoformat(), "readings": readings}, history)

    publish([1200.0] * 5)
    assert capacity_trip(entity("transformer", "TX1"), 0.95, 5) is True
    publish([1200.0, 1200.0, 900.0, 1200.0, 1200.0])
    assert capacity_trip(entity("transformer", "TX1"), 0.95, 5) is False
    publish([1200.0] * 5)
    handled = router.handle_event(make_event("EV-TX", 0, "equipment_fault", entity("transformer", "TX1")))
    assert handled.channel == "urgent" and "停产" in urgent[0]


def test_main_router_mode_requires_enabled_nexent_and_key(monkeypatch):
    from types import SimpleNamespace
    from plantkg.agent.router.pack import _router_mode

    settings = SimpleNamespace(nexent=SimpleNamespace(enabled=False, api_key_env="NEXENT_API_KEY"))
    monkeypatch.delenv("NEXENT_API_KEY", raising=False)
    assert _router_mode(settings) == "evaluation"
    monkeypatch.setenv("NEXENT_API_KEY", "configured")
    assert _router_mode(settings) == "evaluation"  # 有 key 但 Nexent 未启用：不切 live
    settings.nexent.enabled = True
    assert _router_mode(settings) == "live"


def test_key_without_nexent_enabled_keeps_evaluation_and_empty_versions(tmp_path, monkeypatch):
    from plantkg.core.contracts import PlantkgError
    from plantkg.core.ports import VersionStore

    monkeypatch.setenv("NEXENT_API_KEY", "configured")
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                       "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        versions = services.registry.resolve(VersionStore)
        assert router.mode == "evaluation" and router.run_id == "router-demo-electronics"
        with pytest.raises(PlantkgError):
            versions.active()  # 没有活动清单
        assert versions.list()[0] == []  # 也没有合成引导清单
    finally:
        services.close()


def test_live_assembly_uses_bootstrap_run_without_active_manifest(tmp_path, monkeypatch):
    from plantkg.core.contracts import PlantkgError
    from plantkg.core.ports import RunStore, VersionStore

    monkeypatch.setenv("NEXENT_API_KEY", "configured")
    settings = load_settings(profile="dev", overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                                      "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series"),
                                                      "nexent.enabled": "true"})
    services = build_services(settings)
    try:
        router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        versions = services.registry.resolve(VersionStore)
        run = services.registry.resolve(RunStore).get_run(router.run_id)
        assert router.mode == "live" and run.kind == "scenario"
        assert versions.get(run.version_manifest_id).status == "verified"
        with pytest.raises(PlantkgError):
            versions.active()
    finally:
        services.close()


def test_live_router_bootstrap_registers_bootstrap_manifest_without_activating(tmp_path):
    from plantkg.agent.router.pack import _ensure_live_run
    from plantkg.core.contracts import PlantkgError
    from plantkg.core.ports import RunStore, VersionStore

    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                       "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        _ensure_live_run(services.registry, settings, router)

        versions = services.registry.resolve(VersionStore)
        run = services.registry.resolve(RunStore).get_run(router.run_id)
        manifest = versions.get(run.version_manifest_id)
        assert run.kind == "scenario" and run.clock.end - run.clock.start == timedelta(days=1)
        assert manifest.status == "verified" and router.knowledge_version == 1
        with pytest.raises(PlantkgError):
            versions.active()  # 引导清单只绑定本次运行，不设成全局活动版本
    finally:
        services.close()


def _recall_context():
    from plantkg.core.contracts import Actor, Context

    return Context(plant_id=PLANT, industry_pack="manufacturing", actor=Actor(actor_id="duty", roles=("operator",)),
                   channel="offline", trace_id="t", as_of=T0, run_id="run-1")


def test_recall_expands_entity_to_one_hop_neighbors(assembled):
    registry, _, _ = assembled
    result = registry.tools["recall"].handler(_recall_context(), entity("air_compressor", "AC-2"))
    neighbors = [item for item in result.data["items"] if item["summary"] == "一跳邻居"]
    assert neighbors and all(item["id"].startswith("entity://") for item in neighbors)
    assert "空压" in result.data["summary"] or "AC-2" in result.data["summary"]


def test_recall_reads_asset_catalog(assembled, tmp_path):
    registry, _, _ = assembled
    source = tmp_path / "leak-manual.md"
    source.write_text("空压泄漏处理手册", encoding="utf-8")
    ingested = registry.tools["ingest"].handler(_recall_context(), source_uri=str(source), asset_type="process_doc",
                                                available_at=T0.isoformat())
    result = registry.tools["recall"].handler(_recall_context(), ingested.data["asset_version_id"])
    assert "资料目录未接入" not in result.data["summary"]
    assert "leak-manual" in result.data["summary"] and "process_doc" in result.data["summary"]
