"""B22 设备监测专家冒烟测试：注入 → 识别 → 切换/隔离 → 维修工单；噪声不处置；MetroPT-3 与计量异常。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plantkg.core.contracts import RunBinding
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.experts.equipment.demo import run as run_demo
from plantkg.experts.equipment.metering import load_events, to_script_event, to_script_events
from plantkg.experts.equipment.source import EquipmentResultSource
from plantkg.integrations.nexent import proposals
from plantkg.world.scenario.runner import default_registry

from .conftest import ROOT

UTC = timezone.utc
IDS = {"AC-M": "entity://metro/air_compressor/AC-M", "AC-M2": "entity://metro/air_compressor/AC-M2",
       "air_system": "entity://metro/air_system/AIR"}


def _state(window, baseline: dict, *, ticket: str = "TK-1", knowledge=()) -> dict:
    return {"ticket_id": ticket, "trigger_at": window.samples[-1].at, "ids": IDS, "entity_code": "AC-M",
            "entity_id": IDS["AC-M"], "rated_kw": 110.0, "standby": "AC-M2", "knowledge": list(knowledge),
            "readings": window.reading_state(), "baseline": baseline, "evidence_refs": (f"metropt3:{window.name}",)}


def _day_normal(samples: int = 60) -> list[dict]:
    """白天正常运转：60% 时间运转、电流均值约 3.4 A、油温 70 ℃、压力平稳（configured 日间形状）。"""
    out = []
    start = datetime(2020, 5, 30, 10, 0, tzinfo=UTC)
    for i in range(samples):
        on = i < int(samples * 0.6)
        out.append({"at": (start + timedelta(minutes=i)).isoformat(), "current_a": 5.6 if on else 0.05,
                    "oil_c": 70.0, "pressure_bar": 8.5, "on": on})
    return out


def test_metropt3_windows_reproduce_card_statistics(calibration):
    leak, base = calibration.leak.stats, calibration.baseline.stats
    assert leak.running_ratio == pytest.approx(1.0) and base.running_ratio == pytest.approx(0.2333, abs=0.01)
    assert leak.current_mean_a == pytest.approx(5.6, abs=0.1) and base.current_mean_a == pytest.approx(1.0, abs=0.1)
    assert leak.oil_start_c == pytest.approx(65, abs=1) and leak.oil_end_c == pytest.approx(76, abs=1)
    assert leak.pressure_drop_bar_per_h < -0.5 and abs(base.pressure_drop_bar_per_h) < 0.05
    assert load_events() and len(load_events()) == 24


def test_leak_window_detected_and_baseline_window_not(expert, calibration):
    leak = expert.propose({"event_type": "compressor_pressure_drop", "entity_id": IDS["AC-M"]},
                          _state(calibration.leak, calibration.baseline_for(2), knowledge=["compressor_leak_rule"]))
    assert "泄漏" in leak.diagnosis and leak.confidence >= 0.7
    assert {a.command for a in leak.actions} >= {"switch_standby", "isolate_leak", "repair"}
    assert "knowledge:compressor_leak_rule" in leak.evidence_refs
    baseline = expert.propose({"event_type": "compressor_pressure_drop", "entity_id": IDS["AC-M"]},
                              _state(calibration.baseline, calibration.baseline_for(2)))
    assert baseline.actions == () and "未复现" in baseline.diagnosis and baseline.confidence < 0.3


def test_day_normal_readings_only_quiet_with_day_baseline(expert, calibration):
    """白天正常运转占比约 60%：拿夜间 23% 基线会误判，必须用同时段基线。"""
    day = {"window": "10:00～11:00", "sampling_s": 60, "nature": "simulated", "samples": _day_normal()}
    state = _state(calibration.baseline, calibration.baseline_for(10))
    state["readings"] = day
    quiet = expert.propose({"event_type": "compressor_pressure_drop", "entity_id": IDS["AC-M"]}, state)
    assert quiet.actions == () and "未复现" in quiet.diagnosis
    wrong = expert.propose({"event_type": "compressor_pressure_drop", "entity_id": IDS["AC-M"]},
                           state | {"baseline": calibration.baseline_for(2)})
    assert wrong.actions and "泄漏" in wrong.diagnosis  # 反例：夜间基线判白天，会误判——所以必须选同时段


def test_metering_anomaly_opens_metering_work_order_not_switch(expert):
    events = load_events()
    assert len(events) == 24
    event = to_script_event(events[0], plant_id="demo-electronics")
    state = {"ticket_id": "TK-meter", "trigger_at": datetime(2026, 7, 1, 3, 0, tzinfo=UTC), "ids": {}, "panel": event["entity_id"]}
    proposal = expert.propose(event, state)
    assert [a.command for a in proposal.actions] == ["log_maintenance"]
    action = proposal.actions[0]
    assert action.readiness == "needs_human" and action.params["kind"] == "metering"
    assert "数据异常" in proposal.diagnosis and "不切换设备" in proposal.diagnosis
    assert proposal.confidence == 0.9
    scripts = to_script_events(events)
    assert len(scripts) == 24 and all(s["type"] == "metering_anomaly" and s["payload"]["nature"] == "derived" for s in scripts)


def test_demo_covers_injection_recognition_switch_and_work_order():
    data = run_demo(ROOT / "configs/scenarios/factory_demo_day.yaml")
    injection = next(i for i in data["injections"] if i["kind"] == "compressor_leak")
    assert injection["nature"] == "derived" and injection["transform_steps"]
    assert "MetroPT-3" in injection["source"] and "非本厂设备" in injection["source_note"]
    leak = data["leak"]
    commands = {a["command"] for a in leak["proposal"]["actions"]}
    assert {"switch_standby", "isolate_leak", "repair"} <= commands
    assert leak["work_orders"] and leak["work_orders"][0]["status"] == "proposed"
    assert any(a["action"]["command"] == "switch_standby" for a in leak["actions"])
    run = leak["agent_run"]
    assert run["latency_ms"] is not None and run["source"] == "deterministic"
    assert datetime.fromisoformat(run["effective_at"]) == datetime.fromisoformat(run["trigger_at"]) + timedelta(minutes=1)
    metering = data["metering"]
    assert {a["command"] for a in metering["proposal"]["actions"]} == {"log_maintenance"} and metering["work_orders"]
    assert "计量检修" in metering["work_orders"][0]["reason"]
    assert data["air_energy"]["after_10_20_10_40_kwh"] > data["air_energy"]["before_10_00_10_20_kwh"]
    assert data["metropt3"]["leak"]["proposal"]["actions"] and data["metropt3"]["baseline"]["proposal"]["actions"] == []
    lie = data["sensor_lie"]
    assert "air_current_pressure" in lie["before"]["alarms"]
    assert {"sensor", "equipment"} <= set(lie["before"]["candidate_categories"])
    assert set(lie["after"]["candidate_categories"]) == {"sensor"}
    assert lie["after"]["trial"]["verdict"] == "sensor_fault"
    assert lie["after"]["trial"]["pressure_delta"] == 0 and lie["after"]["trial"]["virtual_delta"] > 0
    assert {a["command"] for a in lie["first"]["actions"]} == {"start"} and lie["first"]["actions"][0]["params"]["trial"] is True
    assert {a["command"] for a in lie["second"]["actions"]} == {"log_maintenance"}
    assert lie["work_orders"] and "卡死" in lie["work_orders"][0]["reason"]


def test_scenario_registry_uses_real_equipment_expert():
    registry = default_registry()
    source = registry.resolve_mode(AgentResultSource, "deterministic", key="equipment")
    assert isinstance(source, EquipmentResultSource)


def test_registry_registers_equipment_propose_via_b23_wrapper():
    registry = Registry()
    registry.load([ROOT / "plantkg/packs/equipment"], ["equipment"])
    assert registry.hooks_for("expert_propose:equipment")
    sink = proposals.MemoryProposalSink()
    binding = RunBinding(run_id="run-t", knowledge_version=1, ticket_id="TK-1", round=1, state_version=0,
                         trigger_at=datetime(2026, 7, 1, 3, 0, tzinfo=UTC))
    proposals.install(registry, lambda: sink, lambda context: (binding, None), lambda context, proposal_id: None, placeholders=False)
    assert "equipment_propose" in registry.tools
    result = registry.tools["equipment_propose"].handler(None, focus="空压机压力下降")
    assert result.ok and result.data["proposal_id"]
    assert sink.get(result.data["proposal_id"]).expert == "equipment"


def test_random_injections_are_reproducible():
    from plantkg.world.site.model import SiteModel

    site = SiteModel.load(ROOT / "configs/sites/factory/site.yaml")
    start, end = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC)
    from plantkg.world.sim.faults import random_injections

    first, second = random_injections(site, start, end, seed=7), random_injections(site, start, end, seed=7)
    assert [(i.kind, i.entity, i.at) for i in first] == [(i.kind, i.entity, i.at) for i in second]
    assert {i.kind for i in first} == {"compressor_leak", "lighting_circuit_loss"}


def _sensor_world(tmp_path):
    from plantkg.world.scenario import ScenarioRunner
    from plantkg.world.scenario.__main__ import build_world, open_db
    from plantkg.world.scenario.script import Scenario

    scenario = Scenario.model_validate({"scenario_id": "baseline-len", "title": "基线长度", "nature": "synthetic", "date": "2026-07-01",
                                        "days": 1, "seed": 7, "initial": {"soc": 0.5}, "events": []})
    world = build_world(scenario)
    db = open_db(tmp_path / "baseline.db", world.site.plant_id)
    runner = ScenarioRunner(world, db, mode="evaluation")
    runner.start()
    return runner, scenario, db


def test_baseline_is_really_60_minutes_and_label_matches(tmp_path):
    runner, scenario, db = _sensor_world(tmp_path)
    try:
        runner.run_until(scenario.instant("10:30"))
        end = scenario.instant("10:30")
        source = EquipmentResultSource().bind(runner)
        state = source._from_sensors("AC-2", end, end - timedelta(minutes=10))
    finally:
        db.close()
    baseline = state["baseline"]
    assert baseline["window"] == "09:20～10:20 同期基线" and baseline["samples"] == 60 and baseline["minutes"] == pytest.approx(60.0, abs=1.0)
    assert len(state["readings"]["samples"]) == 15


@pytest.mark.parametrize("with_rule", [False, True])
def test_sensor_lie_trials_before_rule_and_never_isolates(with_rule):
    from plantkg.experts.equipment.demo import _sensor_lie_case
    from plantkg.experts.equipment.expert import EquipmentExpert

    lie = _sensor_lie_case(EquipmentExpert(), with_rule=with_rule)
    assert {a["command"] for a in lie["first"]["actions"]} == {"start"} and lie["first"]["actions"][0]["params"]["trial"] is True
    assert lie["after"]["trial"]["verdict"] == "sensor_fault"
    assert {a["command"] for a in lie["second"]["actions"]} == {"log_maintenance"} and "卡死" in lie["second"]["diagnosis"]
    assert lie["trial_action"] is not None
    assert lie["after"]["trial"]["virtual_delta"] > 0  # 备用机开局停机，试探的世界变化来自真实启动


def test_trial_not_proposed_when_standby_already_running(expert):
    observation = {"alarms": ["air_current_pressure"], "trial": None,
                   "candidates": [{"id": "a", "label": "压力传感器卡死", "category": "sensor"},
                                  {"id": "b", "label": "管网泄漏", "category": "equipment"}]}
    state = {"ticket_id": "TK-1", "trigger_at": datetime(2026, 7, 1, 2, 4, tzinfo=UTC), "entity_code": "AC-2", "observation": observation,
             "ids": {"AC-1": "entity://p/air_compressor/AC-1", "AC-2": "entity://p/air_compressor/AC-2", "air_system": "entity://p/air_system/AIR"},
             "standby": "AC-1", "knowledge": ["compressor_leak_rule"]}
    event = {"event_type": "compressor_pressure_drop", "entity_id": "entity://p/air_compressor/AC-2", "severity": 3}
    proposal = expert.propose(event, state | {"standby_running": False})
    assert [a.command for a in proposal.actions] == ["start"]
    proposal = expert.propose(event, state | {"standby_running": True})
    assert proposal.actions == () and "已在运行" in proposal.diagnosis
