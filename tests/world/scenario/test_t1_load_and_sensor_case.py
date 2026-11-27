"""#238 方案 2 / #236：T1 负载读数进状态帧与观测层，B22 读数可疑时落 sensor_case 记录。

都经真实默认装配（`ScenarioRunner`、真实 SQLite、`sensor_query`）验证，不测内部函数。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from plantkg.core.registry import Registry
from plantkg.world.scenario import ScenarioRunner, load_scenario
from plantkg.world.scenario.__main__ import ROOT, build_world, open_db
from plantkg.world.scenario.export import export_day
from plantkg.world.scenario.runner import state_from_frames
from plantkg.world.sensors.query import install as install_query

from .conftest import local, scenario

TX1 = "entity://demo-electronics/transformer/TX1"
EXTREME = ROOT / "configs" / "scenarios" / "extreme_sensor_lie.yaml"


def test_transformer_load_ratio_every_minute_and_queryable(tmp_path):
    sc = scenario(events=[{"at": "08:05", "type": "load_spike", "entity_id": "AC-2", "severity": 4, "summary": "总用电突增"}],
                  params={"capacity_kw": {"TX1": 1150}})
    world = build_world(sc)
    db = open_db(tmp_path / "t1.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=local("08:15"))
        assert trace.states and all(snap.get(TX1, {}).get("load_ratio") for _, snap in trace.states)  # 每分钟都有读数
        states = {t: snap[TX1] for t, snap in trace.states}
        before, after = states[local("08:05")], states[local("08:06")]
        assert before["load_ratio"] > after["load_ratio"] + 0.2  # 08:06 储能放电后负载率回落
        assert before["power_kw"] - after["power_kw"] == pytest.approx(400.0, abs=40.0)
        frames = db.store.frames(trace.run_id)
        rebuilt = state_from_frames(frames, local("08:06"))[TX1]  # 状态帧能按分钟重建
        assert rebuilt["load_ratio"] == pytest.approx(after["load_ratio"], abs=1e-9)
        registry = Registry()
        install_query(registry, SimpleNamespace(plant_id=world.site.plant_id))
        latest = trace.states[-1][1][TX1]  # sensor_query 读最近一分钟快照
        reading = registry.tools["sensor_query"].handler(None, sensor_id="TX1.P").data["readings"][0]
        assert reading["value"] == pytest.approx(latest["power_kw"], abs=1.0)
        readings = registry.tools["sensor_query"].handler(None, entity="TX1").data["readings"]
        assert next(r for r in readings if r["sensor_id"] == "TX1.LOAD")["value"] == pytest.approx(latest["load_ratio"], abs=1e-3)
    finally:
        db.close()


def test_sensor_lie_writes_sensor_case_with_trial_conclusion(tmp_path):
    """极端剧本：压力表卡死 → B22 判读数可疑写 sensor_case → 试探后覆盖同一记录的结论字段。"""
    sc = load_scenario(EXTREME)
    world = build_world(sc)
    db = open_db(tmp_path / "sensor_lie.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=sc.instant("10:10"))
        cases = db.store.records(trace.run_id, "sensor_case")
    finally:
        db.close()
    assert len(cases) == 1  # 同一触发事件只写一条，试探结束覆盖而不是新开
    case = cases[0]
    event = next(e for e in trace.events if e.event_id == case["event_id"])
    trial = [a for a in trace.actions if a["action"]["params"].get("trial")]
    assert event.event_type == "compressor_pressure_drop" and trial and trial[0]["action"]["command"] == "start"
    assert case["sensor"] == "AIR-HDR.P" and case["quality"] == "suspect" and case["nature"] == "simulated"
    assert case["reading"] == pytest.approx(5.6) and case["baseline"] == pytest.approx(7.0, abs=0.2)
    assert case["residual_c"] < -case["residual_limit_c"]  # 残差超出限值才判可疑
    assert {c["category"] for c in case["candidates"]} == {"sensor", "equipment"}
    assert case["conclusion"] == "sensor_fault" and case["verified_by"] and case["resolution"]
    assert set(case["excluded"]) == {"air_leak", "compressor_fault"}  # 试探排除了设备侧候选


def test_sensor_case_conclusion_waits_for_its_own_day(tmp_path):
    """跨日：23:58 可疑、次日 00:0x 出结论；7-01 导出只有可疑字段，7-02 导出（结论当天）完整显示。"""
    sc = scenario(days=2, events=[
        {"at": "23:58", "id": "late-lie", "type": "compressor_pressure_drop", "entity_id": "AC-2", "severity": 3,
         "summary": "深夜压力骤降", "payload": {"effects": []}},
    ], initial_actions=[{"entity": "AC-1", "command": "stop"}],
       sensor_faults=[{"sensor_id": "AIR-HDR.P", "kind": "stuck", "at": "23:40", "params": {"value": 5.6}}])
    world = build_world(sc)
    db = open_db(tmp_path / "late_sensor.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        runner.start()
        trace = runner.run_until(local("00:05", day=2))
        cases = db.store.records(trace.run_id, "sensor_case")
        assert cases and cases[0].get("verified_at")
        verified_at = datetime.fromisoformat(cases[0]["verified_at"].replace("Z", "+00:00"))
        assert verified_at >= local("00:00", day=2)  # 结论产生在 7-02
        ticket = next(iter(runner.tickets.tickets.values()))
        runner.dispatcher.dispatch(ticket, local("00:05", day=2), reason="trial")  # 结论后重派，让 7-02 导出引用该单与事件
        runner.run_until(local("00:10", day=2))
        day1 = export_day(db, trace.run_id, "2026-07-01")
        day2 = export_day(db, trace.run_id, "2026-07-02")
    finally:
        db.close()
    day1_event = next(e for e in day1.events if e.event_id == "late-lie")
    assert day1_event.payload["sensor"] == "AIR-HDR.P" and day1_event.payload["quality"] == "suspect"
    assert {"conclusion", "verified_by", "excluded", "resolution"}.isdisjoint(day1_event.payload)  # 不提前暴露次日结论
    day2_event = next(e for e in day2.context.events if e.event_id == "late-lie")
    assert day2_event.payload["conclusion"] == "sensor_fault" and day2_event.payload["resolution"]


def test_sensor_tick_does_not_disable_storage_playbook(tmp_path):
    """回归（pr-check 发现）：设备包的每分钟结论钩子不能冒充 B19 分钟层，B47 保护动作仍要下发电池放电。"""
    sc = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    world = build_world(sc)
    db = open_db(tmp_path / "tick.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run(until=sc.instant("08:10"))
    finally:
        db.close()
    skipped = [n for n in trace.notes if n.get("kind") == "b47_playbook_skipped" and "分钟层接管" in str(n.get("reason"))]
    assert not skipped  # 没有 B19 分钟层时，B47 不能把储能保护动作跳过
    assert any(a["action"]["command"] == "discharge" and a["action"]["entity_id"].endswith("/battery/BAT1")
               for a in trace.actions)
