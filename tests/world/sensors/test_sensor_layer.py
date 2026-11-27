"""B22 观测层冒烟测试：读数与 Quality、5 种传感器故障、残差与候选、试探收敛、sensor_query。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from plantkg.core.contracts import Interval, SimAction
from plantkg.core.registry import Registry
from plantkg.world.sensors import ObservationBus, SensorFault, load_sensor_config
from plantkg.world.sensors.query import install as install_query

from .conftest import sensor_scenario


def _latest(runner):
    return runner.state.sims["sensors"]["latest"]


def test_sensor_layer_maps_state_to_readings_with_quality(make):
    runner, sc = make()
    runner.run_until(sc.instant("09:01"))
    snap = _latest(runner)
    assert runner.world.simulators[-1].stage == "observe"
    names = [sim.name for sim in runner.world.simulators]
    assert {"equipment_faults", "equipment", "trial_actions", "sensor_faults", "sensors"} <= set(names)
    assert names[-1] == "sensors"
    assert len(snap["readings"]) == len(load_sensor_config().sensors) >= 15
    pressure = snap["readings"]["AIR-HDR.P"]
    assert pressure["value"] == pytest.approx(7.0, abs=0.2)
    assert pressure["quality"]["flag"] == "ok" and pressure["at"] == snap["at"]
    assert {r["id"] for r in snap["relations"]} == {"energy_balance", "air_current_pressure", "hvac_power_temperature"}
    assert {v["virtual_id"] for v in snap["virtuals"].values()} == {"VS-AIR-P", "VS-GRID-P"}
    assert "air_current_pressure" not in snap["alarms"]
    assert runner.state.sims["sensors"]["history"]["AIR-HDR.P"][-1]["at"] == snap["at"]


@pytest.mark.parametrize("kind", ["stuck", "drift", "offline", "spike", "bias"])
def test_five_sensor_faults_change_readings(kind, make):
    sc = sensor_scenario()
    params = {"stuck": {"value": 5.6}, "drift": {"rate_per_min": 0.1}, "offline": {}, "spike": {"period_min": 1, "magnitude": 1.0},
              "bias": {"offset": 0.5}}[kind]
    fault = SensorFault("AIR-HDR.P", kind, sc.instant("00:01"), None, params)
    runner, sc = make(sc, faults=[fault], name=f"{kind}.db")
    runner.run_until(sc.instant("00:04"))
    readings = [_latest(runner)["readings"]["AIR-HDR.P"]]
    assert readings[0]["at"] != ""
    if kind == "stuck":
        history = [item for item in runner.state.sims["sensors"]["history"]["AIR-HDR.P"] if item["at"] >= fault.start.isoformat()]
        assert len(history) >= 3 and all(item["value"] == pytest.approx(5.6) for item in history[-3:])
        assert len({item["at"] for item in history[-3:]}) == 3
        assert _latest(runner)["readings"]["AIR-HDR.P"]["quality"]["flag"] == "suspect"
    elif kind == "drift":
        history = runner.state.sims["sensors"]["history"]["AIR-HDR.P"]
        assert history[-1]["value"] > history[-2]["value"] + 0.05
    elif kind == "offline":
        reading = _latest(runner)["readings"]["AIR-HDR.P"]
        assert reading["value"] is None and reading["quality"]["flag"] == "missing"
    elif kind == "spike":
        reading = _latest(runner)["readings"]["AIR-HDR.P"]
        assert reading["value"] > 7.5 and reading["quality"]["flag"] == "suspect"
    else:
        assert _latest(runner)["readings"]["AIR-HDR.P"]["value"] == pytest.approx(7.5, abs=0.2)


def test_stuck_pressure_triggers_residual_alarm_and_candidates(make):
    sc = sensor_scenario()
    fault = SensorFault("AIR-HDR.P", "stuck", sc.instant("09:40"), None, {"value": 5.6})
    runner, sc = make(sc, faults=[fault], name="stuck_alarm.db")
    runner.run_until(sc.instant("10:04"))
    snap = _latest(runner)
    relation = next(r for r in snap["relations"] if r["id"] == "air_current_pressure")
    assert relation["alarm"] and relation["residual"] == pytest.approx(-1.4, abs=0.3)
    assert snap["readings"]["AIR-HDR.P"]["quality"]["flag"] == "suspect"
    categories = {c["category"] for c in snap["candidates"]}
    assert categories == {"sensor", "equipment"}
    assert {"pressure_sensor_stuck", "air_leak", "compressor_fault"} <= {c["id"] for c in snap["candidates"]}


def test_trial_narrows_candidates_and_virtual_changes(make):
    sc = sensor_scenario()
    fault = SensorFault("AIR-HDR.P", "stuck", sc.instant("09:40"), None, {"value": 5.6})
    runner, sc = make(sc, faults=[fault], name="trial.db")
    runner.run_until(sc.instant("10:05"))
    before = _latest(runner)
    assert before["trial"] is None
    ac1 = runner.world.ids()["AC-1"]
    runner.apply(SimAction(entity_id=ac1, command="stop", params={},
                           interval=Interval(start=sc.instant("10:05"), end=sc.instant("10:06"))))
    runner.run_until(sc.instant("10:06"))
    runner.apply(SimAction(entity_id=ac1, command="start",
                           params={"trial": True, "kw": 30.0, "pressure_boost_bar": 0.2, "hypotheses": ["sensor", "leak"]},
                           interval=Interval(start=sc.instant("10:06"), end=sc.instant("10:09"))))
    runner.run_until(sc.instant("10:10"))
    after = _latest(runner)
    trial = after["trial"]
    assert trial["verdict"] == "sensor_fault"
    assert trial["pressure_delta"] == pytest.approx(0.0, abs=0.01) and trial["virtual_delta"] > 0
    assert {c["category"] for c in after["candidates"]} == {"sensor"}
    stamps = [item["at"] for item in trial["readings_at"]]
    assert len(stamps) >= 3 and stamps == sorted(stamps) and stamps[-1] > trial["started_at"]
    history = runner.state.sims["sensors"]["history"]["AIR-HDR.P"]
    during = [item for item in history if trial["started_at"] < item["at"] <= trial["window_end"]]
    assert len(during) >= 2 and all(item["value"] == pytest.approx(5.6) for item in during)
    assert len({item["at"] for item in during}) == len(during)
    assert before["virtuals"]["VS-AIR-P"]["value"] != trial["after"]["virtual_pressure"]


def test_trial_on_running_standby_adds_no_load_and_no_verdict(make):
    sc = sensor_scenario()
    fault = SensorFault("AIR-HDR.P", "stuck", sc.instant("09:40"), None, {"value": 5.6})
    runner, sc = make(sc, faults=[fault], name="trial_running.db")
    runner.run_until(sc.instant("10:05"))
    ac1 = runner.world.ids()["AC-1"]
    assert runner.state.devices[ac1]["running"]
    runner.apply(SimAction(entity_id=ac1, command="start", params={"trial": True, "kw": 30.0, "pressure_boost_bar": 0.2},
                           interval=Interval(start=sc.instant("10:05"), end=sc.instant("10:08"))))
    runner.run_until(sc.instant("10:10"))
    trial = _latest(runner)["trial"]
    assert not runner.state.effect("pressure_boost") and trial["verdict"] == "unfinished"


def test_sensor_query_registered_and_returns_observation(make):
    runner, sc = make()
    runner.run_until(sc.instant("00:02"))
    registry = Registry()
    install_query(registry, SimpleNamespace(plant_id=runner.world.site.plant_id))
    assert "sensor_query" in registry.tools
    binding = registry.tools["sensor_query"]
    result = binding.handler(None, entity="AIR-HDR")
    assert result.ok and result.business_status == "done"
    assert result.data["readings"] and result.data["relations"] and result.data["virtuals"]
    assert result.data["history"]["AIR-HDR.P"]
    result = binding.handler(None, sensor_id="AIR-HDR.P", minutes=5)
    assert result.ok and result.data["history"]["AIR-HDR.P"]
    assert result.data["trial"] is None


def test_sensor_query_without_scenario_is_degraded():
    ObservationBus.clear()
    registry = Registry()
    install_query(registry, SimpleNamespace(plant_id="demo-electronics"))
    result = registry.tools["sensor_query"].handler(None)
    assert result.ok and result.business_status == "degraded" and result.notice


def test_sensor_case_overlay_keeps_base_config_nature():
    """P2-5：人工确认案例只把新增条目标 synthetic，不改整份配置的 nature。"""
    from plantkg.world.sensors.config import apply_sensor_overrides

    config = load_sensor_config()
    assert config.nature == "configured"
    assert all(item.nature == "configured" for items in config.fault_matrix.values() for item in items)
    assert all(item.nature == "configured" for item in config.sensor_fault_candidates.values())
    relation_id = config.relations[0].id
    overlaid = apply_sensor_overrides(config, [{"relation_id": relation_id, "judgement": "sensor",
                                                "label": "人工确认案例", "sensors": ["AIR-HDR.P"]}])
    assert overlaid is not config
    assert overlaid.nature == config.nature  # 基准配置性质不变
    added = [item for items in overlaid.fault_matrix.values() for item in items
             if item.id.startswith("case-")]
    assert added and all(item.nature == "synthetic" for item in added)

def test_sensor_query_fallback_reports_snapshot_run_id():
    """回退到旧快照时用快照自身的运行标识（缺失为 None），不冒充请求的运行（#201）。"""
    ObservationBus.clear()
    try:
        snapshot = {"at": "2026-07-01T00:00:00+00:00", "readings": {}, "relations": [], "virtuals": {},
                    "candidates": [], "alarms": [], "run_id": "run-old"}
        ObservationBus.publish("quarry", snapshot, run_id="run-old")
        registry = Registry()
        install_query(registry, SimpleNamespace(plant_id="quarry"))
        handler = registry.tools["sensor_query"].handler
        context = SimpleNamespace(plant_id="quarry", run_id="run-new")
        seen = handler(context)
        assert seen.run_id == "run-old" and "run-new" in seen.notice
        ObservationBus.publish("quarry", snapshot | {"run_id": None}, run_id="run-legacy")
        unseen = handler(context)
        assert unseen.run_id is None and "run-new" in unseen.notice
    finally:
        ObservationBus.clear()


def test_observation_bus_keeps_bounded_runs():
    """按运行保留加上了上限：超出的最旧运行被淘汰，最近一次发布仍可取（#201）。"""
    ObservationBus.clear()
    try:
        for index in range(ObservationBus.MAX_RUNS + 2):
            ObservationBus.publish("bounded", {"at": f"2026-07-01T00:00:{index:02d}+00:00", "run_id": f"run-{index:02d}"},
                                   run_id=f"run-{index:02d}")
        assert ObservationBus.latest("bounded", "run-00") is None and ObservationBus.latest("bounded", "run-01") is None
        assert ObservationBus.latest("bounded", "run-02") is not None
        assert ObservationBus.latest("bounded")["snapshot"]["run_id"] == f"run-{ObservationBus.MAX_RUNS + 1:02d}"
        runs = [key for key in ObservationBus._latest if key[0] == "bounded" and key[1] is not None]
        assert len(runs) == ObservationBus.MAX_RUNS
    finally:
        ObservationBus.clear()
