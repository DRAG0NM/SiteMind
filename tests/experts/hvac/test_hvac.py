"""B21 暖通专家冒烟测试：推迟预冷让峰、极端高温负荷上升、硬约束不动、设备容量与停机故障、情境绑定隔离。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from plantkg.core.contracts import AgentResultKind, Context, Interval, RunBinding, SimAction
from plantkg.core.ledger import LedgerMeta
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.experts.hvac import DEFER_MIN, HvacExpert, demo
from plantkg.integrations.nexent import proposals
from plantkg.world.scenario.__main__ import open_db
from plantkg.world.scenario import TrialRunner
from plantkg.world.scenario.runner import SCENARIO_HOOK, ScenarioRunner, default_registry
from plantkg.world.scenario.script import Scenario, load_scenario
from plantkg.world.sim.base import MINUTE
from plantkg.world.sim.thermal import COOLING_TYPES, ThermalSim

ROOT = Path(__file__).resolve().parents[3]
DEMO_DAY = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
HEAT_WAVE = ROOT / "configs" / "scenarios" / "extreme_heat_wave.yaml"
CTX = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=None, channel="none", trace_id="t",
              as_of=datetime(2026, 7, 1, tzinfo=timezone.utc))
BINDING = RunBinding(run_id="run-hvac", knowledge_version=1, ticket_id="TK-001", round=1, state_version=0,
                     trigger_at=datetime(2026, 7, 1, tzinfo=timezone.utc))


class Sim:
    """按分钟推进世界（ScenarioWorld.advance），测试里用来看设备功率、温度与账本。"""

    def __init__(self, world) -> None:
        self.world = world
        self.state, self.energy = world.initial_state()
        self.meta = LedgerMeta(ledger_id="test:hvac", kind="execution", run_id="test:hvac", plan_id="test:hvac",
                               plan_revision=0, version_manifest_id="test:hvac", source_refs=(world.inputs.provider_id,))

    def to(self, at: datetime, *, before=None) -> dict:
        while self.state.at < at:
            if before is not None:
                before(self.state)
            self.step()
        return self.state

    def step(self) -> dict:
        _, self.energy = self.world.advance(self.state, self.energy, self.meta)
        return self.state


def _cooling(world, zone: str) -> list:
    return [e for e in world.site.equipment if e.zone == zone and e.type in COOLING_TYPES]


def _situation(scenario, world, end):
    """跑到事件分钟，给出当分钟的区域视图（与 demo 用的是同一个推进器）。"""
    event, effects, at = demo.pick_event(scenario, world, end)
    rows, _ = demo.run_day(world, end, effects=effects)
    row = next(r for r in rows if r["at"] == at)
    proposal = HvacExpert().propose(event, {"at": at, "outdoor_c": row["outdoor_c"], "zones": row["zones"]})
    return event, effects, at, rows, proposal


def test_defer_precool_lowers_peak_window_and_counts_comfort():
    """推迟预冷：动作窗口盖住事件时长，峰段窗口峰值下降，越界分钟可计算。"""
    scenario = load_scenario(DEMO_DAY)
    world = demo.build_world(scenario)
    end = scenario.start + timedelta(hours=12)
    event, effects, at, rows, proposal = _situation(scenario, world, end)
    assert event["event_type"] == "load_spike"
    assert [a.command for a in proposal.actions] == ["set_mode"]
    assert proposal.actions[0].params == {"mode": "off"}
    spike = 40  # 剧本里突增事件持续 40 分钟：动作窗口盖住事件，不止 DEFER_MIN
    assert proposal.actions[0].interval.end - at == timedelta(minutes=max(DEFER_MIN, spike))

    window = at + timedelta(minutes=max(DEFER_MIN, spike))
    shifted, thermal = demo.run_day(world, end, actions=proposal.actions, effects=effects)
    peak_before = max(r["load_kw"] for r in rows if at <= r["at"] < window)
    peak_after = max(r["load_kw"] for r in shifted if at <= r["at"] < window)
    assert peak_after < peak_before - 20, (peak_before, peak_after)
    assert proposal.cost.comfort_min >= 0
    assert set(thermal["comfort_minutes"]) == {"production", "admin", "cleanroom", "datacenter"}
    assert all(isinstance(v, int) and v >= 0 for v in thermal["comfort_minutes"].values())
    # 让出的容量与行政楼的供冷功率一致（动作落在行政楼，不碰硬约束区域）
    assert proposal.actions[0].entity_id.endswith("/zone/admin")


def test_extreme_heat_raises_hvac_load_and_holds_hard_zone_in_normal_weather():
    """极端高温剧本：暖通电量明显上升；正常天气下洁净车间仍守在硬约束区间内。"""
    base = load_scenario(DEMO_DAY)
    heat = load_scenario(HEAT_WAVE)
    base_rows, base_thermal = demo.run_day(demo.build_world(base), base.start + timedelta(hours=18))
    heat_rows, heat_thermal = demo.run_day(demo.build_world(heat), heat.start + timedelta(hours=18))
    base_kwh = sum(r["hvac_kw"] for r in base_rows) / 60
    heat_kwh = sum(r["hvac_kw"] for r in heat_rows) / 60
    assert heat_kwh > base_kwh * 1.3, (base_kwh, heat_kwh)
    assert max(r["load_kw"] for r in heat_rows) > max(r["load_kw"] for r in base_rows)
    # 正常气温（26 ℃ 上下）洁净车间守得住 21～23 ℃；极端高温柔 +11 ℃ 时机组到容量上限，另见容量测试
    assert base_thermal["comfort_minutes"]["cleanroom"] == 0
    assert max(r["zones"]["cleanroom"]["temperature"] for r in base_rows) <= 23.0
    assert heat_thermal["comfort_minutes"]["datacenter"] == 0  # 机房负荷小，硬约束仍守住


def test_cooling_power_never_exceeds_rated_capacity():
    """满载边界：固定电耗先占额定，制冷只用剩下的；每台供冷设备的分钟功率都不超过额定。"""
    scenario = load_scenario(HEAT_WAVE)
    world = demo.build_world(scenario)
    sim = Sim(world)
    worst = {e.code: 0.0 for e in world.site.equipment if e.type in COOLING_TYPES}
    sim.to(scenario.instant("09:00"))
    saturated = False
    while sim.state.at < scenario.instant("18:00"):
        state = sim.step()
        for e in world.site.equipment:
            if e.type in COOLING_TYPES:
                power = state.power_kw.get(e.code, 0.0)
                worst[e.code] = max(worst[e.code], power)
                assert power <= e.rated_kw + 1e-6, (e.code, power, e.rated_kw)
                saturated |= power >= e.rated_kw - 1e-6
    assert saturated, worst  # 高温窗口里确实压到过额定上限，边界被真正走到


def test_stopped_or_faulted_cooling_devices_give_no_cooling():
    """停机或故障的设备不提供制冷容量：行政楼全部停止后供冷功率为 0、室温回升；只停一台时其余按份额供冷。"""
    scenario = load_scenario(DEMO_DAY)
    world = demo.build_world(scenario)
    sim = Sim(world)
    admin = world.index.zone_eid["admin"]
    cooling = _cooling(world, "admin")
    assert len(cooling) > 1
    sim.to(scenario.instant("14:00"))
    assert sum(sim.state.power_kw.get(e.code, 0.0) for e in cooling) > 0

    for e in cooling:  # 全部停机，并给其中一台注入故障
        sim.state.devices[e.entity_id]["running"] = False
    sim.state.effects.append({"kind": "fault", "entity_id": cooling[0].entity_id, "since": sim.state.at.isoformat(), "until": None})
    before = sim.state.devices[admin]["temperature"]
    state = sim.step()
    assert all(state.power_kw.get(e.code, 0.0) == 0 for e in cooling)
    assert state.devices[admin]["temperature"] > before  # 没有冷量，室温回升
    assert state.sims["thermal"]["hvac_kwh"]["admin"] >= 0

    # 恢复一台、只停另一台：停的那台功率为 0，其余按可用额定份额分摊，合计不超过可用额定
    sim.state.effects = [e for e in sim.state.effects if e["kind"] != "fault"]
    for e in cooling:
        sim.state.devices[e.entity_id]["running"] = True
    sim.state.devices[cooling[1].entity_id]["running"] = False
    state = sim.step()
    remaining = [e for e in cooling if e is not cooling[1]]
    assert state.power_kw.get(cooling[1].code, 0.0) == 0
    assert state.power_kw.get(cooling[0].code, 0.0) > 0
    assert sum(state.power_kw.get(e.code, 0.0) for e in cooling) <= sum(e.rated_kw for e in remaining) + 1e-6


def test_capacity_shed_never_touches_hard_zones():
    """容量受限时只调非硬约束区域的设定温度，洁净车间与数据机房不进提案。"""
    state = {"plant_id": "demo-electronics", "at": "2026-07-01T02:30:00+00:00", "outdoor_c": 32.0,
             "zones": {"admin": {"temperature": 25.0, "setpoint": 25.0, "power_kw": 150.0, "gain_kw": 65.0},
                       "production": {"temperature": 22.0, "setpoint": 22.0, "power_kw": 120.0, "gain_kw": 110.0}}}
    proposal = HvacExpert().propose({"event_type": "capacity_shed_request", "entity_id": None}, state)
    targets = {a.entity_id for a in proposal.actions}
    assert targets and all(a.command == "set_setpoint" for a in proposal.actions)
    assert not any("/zone/cleanroom" in t or "/zone/datacenter" in t for t in targets)
    assert "硬约束" in proposal.diagnosis


def test_temp_rule_overrides_setpoint_and_restores_after_expiry():
    """B47 临时规则用 set_setpoint 动作下发：规则期内覆盖设定温度，动作区间到期后恢复原料设定。"""
    scenario = load_scenario(DEMO_DAY)
    world = demo.build_world(scenario)
    thermal = next(s for s in world.simulators if isinstance(s, ThermalSim))
    state, _ = world.initial_state()
    admin = world.index.zone_eid["admin"]
    at = scenario.instant("13:00")
    rule = SimAction(entity_id=admin, command="set_setpoint", params={"setpoint_c": 24.0, "temp_rule": "TR-0001"},
                     interval=Interval(start=at, end=at + timedelta(minutes=60)))
    seen = []
    for minute in range(90):
        state.at = at + timedelta(minutes=minute)
        state.inputs = world.inputs.minute(state.at)
        state.power_kw = {}
        thermal.step(state, [rule] if minute < 60 else [], MINUTE)
        seen.append((state.devices[admin]["setpoint"], state.devices[admin]["temp_rule"]))
    assert seen[0] == (24.0, "TR-0001") and seen[59] == (24.0, "TR-0001")
    assert seen[60] == (25.0, None) and seen[-1] == (25.0, None)


def test_situation_is_isolated_by_full_binding():
    """情境按完整绑定存档：换运行、换事件单、换轮次都不复用；不匹配时工具返回保守提案。"""
    packs = Registry()
    packs.load([ROOT / "plantkg" / "packs" / "hvac"], ["hvac"], None)
    scenario_registry = Registry()
    packs.hooks_for(SCENARIO_HOOK)[0](scenario_registry)  # 与 B17 默认装配同一条路径
    source = scenario_registry.mode_ports[(AgentResultSource, "deterministic", "hvac")]
    event = {"event_type": "load_spike", "entity_id": "entity://demo-electronics/zone/admin"}
    view = {"at": BINDING.trigger_at.isoformat(), "outdoor_c": 30.0,
            "zones": {"admin": {"entity_id": "entity://demo-electronics/zone/admin", "temperature": 24.5, "setpoint": 25.0,
                                "mode": "normal", "power_kw": 120.0, "gain_kw": 65.0}}}
    source.get(AgentResultKind.expert, BINDING, {"event": event, "state": {"situation": view}, "ids": {}})
    assert source.situation(BINDING) is not None
    for other in (BINDING.model_copy(update={"run_id": "run-2"}), BINDING.model_copy(update={"ticket_id": "TK-002"}),
                  BINDING.model_copy(update={"round": 2}), BINDING.model_copy(update={"state_version": 5})):
        assert source.situation(other) is None

    sink = proposals.MemoryProposalSink()
    proposals.install(packs, lambda: sink, lambda context: (BINDING.model_copy(update={"run_id": "run-2"}), None),
                      lambda context, pid: None, placeholders=False)
    result = packs.tools["hvac_propose"].handler(CTX, focus="别的运行")
    assert result.ok
    saved = sink.get(result.data["proposal_id"])
    assert saved.actions == () and "保守" in (saved.diagnosis or "")  # 不把旧事件的动作带到新运行

    # 领域包只加载一次：多次 default_registry() 共用同一份 source；共用但情境仍按绑定隔离
    first, second = default_registry(), default_registry()
    shared = first.mode_ports[(AgentResultSource, "deterministic", "hvac")]
    assert shared is second.mode_ports[(AgentResultSource, "deterministic", "hvac")]
    shared.get(AgentResultKind.expert, BINDING, {"event": event, "state": {"situation": view}, "ids": {}})
    assert second.mode_ports[(AgentResultSource, "deterministic", "hvac")].situation(BINDING) is not None
    assert shared.situation(BINDING.model_copy(update={"run_id": "run-9"})) is None


def test_event_at_scenario_start_does_not_crash_runner(tmp_path):
    """首分钟（00:00）派发事件：brief 先备好当分钟输入，不推进时钟也不抛 AttributeError。"""
    scenario = Scenario.model_validate({
        "scenario_id": "t-first-minute", "title": "首分钟事件", "date": "2026-07-01", "seed": 3,
        "events": [{"at": "00:00", "id": "e0", "type": "load_spike", "entity_id": "AC-2", "severity": 3,
                    "summary": "首分钟突增", "payload": {"effects": [{"kind": "extra_load", "kw": 400, "minutes": 30}]}}]})
    world = demo.build_world(scenario)
    db = open_db(tmp_path / "start.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation")
        runner.start()
        assert runner.state.inputs is None
        runner.step()
        assert runner.state.inputs is not None
        assert [e.event_id for e in runner.trace.events] == ["e0"]
        assert runner.trace.tickets, "首分钟的事件应当开单派发"
        assert any(r.agent_name == "hvac" and r.status == "finished" for r in runner.trace.agent_runs)
    finally:
        db.close()


def test_hvac_propose_is_registered_through_registry_and_wrapped():
    """pack.yaml 登记 hvac_propose，B23 的包装把它变成“落库后只返回 proposal_id”的工具。"""
    registry = Registry()
    registry.load([ROOT / "plantkg" / "packs" / "hvac"], ["hvac"], None)
    assert "hvac_propose" in registry.tools
    sink = proposals.MemoryProposalSink()
    proposals.install(registry, lambda: sink, lambda context: (BINDING, None), lambda context, pid: None, placeholders=False)

    def situation(context, binding):
        return ({"event_type": "heat_wave", "entity_id": "entity://demo-electronics/zone/admin"},
                {"plant_id": "demo-electronics", "at": binding.trigger_at.isoformat(), "outdoor_c": 34.0,
                 "zones": {"admin": {"temperature": 25.0, "setpoint": 25.0, "power_kw": 180.0, "gain_kw": 65.0}}})

    registry.register_hook("hvac_situation", situation)
    result = registry.tools["hvac_propose"].handler(CTX, focus="午后高温")
    assert result.ok and result.data["proposal_id"].startswith("proposal://")
    saved = sink.get(result.data["proposal_id"])
    assert saved.expert == "hvac" and saved.actions[0].command == "set_mode"
    assert saved.actions[0].params == {"mode": "precool"}


def test_defer_window_is_15_minutes_without_event_duration():
    proposal = HvacExpert().propose({"event_type": "sim_limit", "entity_id": None},
                                    {"plant_id": "p", "at": BINDING.trigger_at, "outdoor_c": 30.0})
    assert proposal.actions[0].interval.end - proposal.actions[0].interval.start == timedelta(minutes=DEFER_MIN)


def test_default_assembly_installs_thermal_sim_and_hvac_expert():
    """B17 默认装配经 SCENARIO_HOOK：ThermalSim 原位替换 HvacSim，暖通专家登记为 deterministic hvac。"""
    world = demo.build_world(load_scenario(DEMO_DAY))
    assert [s.name for s in world.simulators].count("hvac") == 1
    assert any(isinstance(s, ThermalSim) for s in world.simulators)
    source = default_registry().mode_ports[(AgentResultSource, "deterministic", "hvac")]
    assert type(source).__name__ == "HvacExpertSource"


def _apply(sim: Sim, entity_id: str, command: str, minutes: int, **params) -> None:
    action = SimAction(entity_id=entity_id, command=command, params=params,
                       interval=Interval(start=sim.state.at, end=sim.state.at + timedelta(minutes=minutes)))
    sim.state.sims["actions"].append({"action": action.model_dump(mode="json"), "source": {}})


def test_hard_zone_rejects_setpoint_precool_and_temp_rule():
    """硬约束区域不接受设定温度改写、预冷与停机；行政楼的临时规则照常生效。"""
    from plantkg.world.scenario.dispatch import temp_rule_from
    scenario = load_scenario(DEMO_DAY)
    world = demo.build_world(scenario)
    sim = Sim(world)
    sim.to(scenario.instant("14:00"))
    clean, admin = world.index.zone_eid["cleanroom"], world.index.zone_eid["admin"]
    _apply(sim, clean, "set_setpoint", 30, setpoint_c=26.0)
    _apply(sim, clean, "set_mode", 30, mode="precool")
    rule, action = temp_rule_from({"text": "洁净车间 26 ℃", "zone": "cleanroom", "setpoint_c": 26, "start": "14:00", "end": "14:30"},
                                  sim.state.at, scenario, world.index, "ev", "TR-9")
    sim.state.sims["actions"].append({"action": action[0].model_dump(mode="json"), "source": {}})
    _apply(sim, admin, "set_setpoint", 30, setpoint_c=24.0, temp_rule="TR-1")
    highest = 0.0
    for _ in range(30):
        state = sim.step()
        highest = max(highest, state.devices[clean]["temperature"])
    assert state.devices[clean]["setpoint"] == 22.0 and state.devices[clean]["mode"] == "normal"
    assert highest <= 23.0 and state.sims["thermal"]["comfort_minutes"]["cleanroom"] == 0
    ignored = {i["command"] for i in state.sims["thermal"]["ignored"] if i["entity_id"] == clean}
    assert {"set_setpoint", "set_mode"} <= ignored
    assert state.devices[admin]["setpoint"] == 24.0  # 行政楼临时规则照常生效


def test_all_cooling_stopped_stops_dehumidifying():
    """冷源全停加故障：洁净车间湿度升出带外并计分钟，恢复运行后回到带内。"""
    scenario = load_scenario(DEMO_DAY)
    world = demo.build_world(scenario)
    sim = Sim(world)
    sim.to(scenario.instant("14:00"))
    clean = world.index.zone_eid["cleanroom"]
    cooling = _cooling(world, "cleanroom")
    for e in cooling:
        sim.state.devices[e.entity_id]["running"] = False
        sim.state.effects.append({"kind": "fault", "entity_id": e.entity_id, "since": sim.state.at.isoformat(), "until": None})
    for _ in range(30):
        sim.step()
    assert sim.state.devices[clean]["humidity"] > 55 and sim.state.sims["thermal"]["humidity_minutes"]["cleanroom"] > 0
    sim.state.effects = [e for e in sim.state.effects if e["kind"] != "fault"]
    for e in cooling:
        sim.state.devices[e.entity_id]["running"] = True
    for _ in range(120):
        sim.step()
    assert 40 <= sim.state.devices[clean]["humidity"] <= 50


def test_trial_bands_come_from_thermal_yaml():
    """TrialRunner 的舒适带（sims.ZONES）与 ThermalSim 的 ZoneSpec 一致，行政楼 26.3 ℃ 两边都判越界。"""
    from plantkg.world.scenario.sims import ZONES
    from plantkg.world.sim.thermal import load_zones
    for zone, spec in load_zones().items():
        assert tuple(ZONES[zone]["band"]) == spec.comfort_c and ZONES[zone]["hard"] == spec.hard
    lo, hi = ZONES["admin"]["band"]
    assert not lo <= 26.3 <= hi and not load_zones()["admin"].in_comfort(26.3)


def test_trial_flags_hard_humidity_breach_while_temperature_is_in_band(tmp_path):
    """温度合格但硬区湿度越界：TrialRunner 判不可行并逐分钟写违规；horizon 内无越界则不报，开局前的累计分钟不算。"""
    scenario = Scenario.model_validate({"scenario_id": "t-rh", "title": "湿度", "date": "2026-07-01", "seed": 7, "initial": {"soc": 0.5}})
    world = demo.build_world(scenario)
    db = open_db(tmp_path / "rh.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation")
        runner.start()
        runner.run_until(scenario.instant("14:00"))
        clean = world.index.zone_eid["cleanroom"]
        ok = TrialRunner(world).trial(runner.snapshot(), [], Interval(start=scenario.instant("14:00"), end=scenario.instant("14:15")))
        assert ok.feasible and not ok.violations
        for e in _cooling(world, "cleanroom"):
            runner.state.devices[e.entity_id]["running"] = False
            runner.state.effects.append({"kind": "fault", "entity_id": e.entity_id, "since": runner.state.at.isoformat(), "until": None})
        result = TrialRunner(world).trial(runner.snapshot(), [], Interval(start=scenario.instant("14:00"), end=scenario.instant("14:15")))
        # 空候选（#173 相对语义）：既有硬区越界记入基线，不再把本次试算判成不可行
        assert result.feasible and result.violations == ()
        rh = [v for v in result.baseline_violations if v.entity_id == clean and "湿度" in v.detail]
        assert rh and all(v.kind == "hard_zone" for v in rh)
        assert "[40.0, 50.0]" in rh[0].detail and "%RH" in rh[0].detail
        assert not [v for v in result.baseline_violations if "温度" in v.detail]
        runner.state.sims["thermal"]["humidity_minutes"]["cleanroom"] = 99  # 历史累计不是当前违规
        for e in _cooling(world, "cleanroom"):
            runner.state.devices[e.entity_id]["running"] = True
        runner.state.effects = [e for e in runner.state.effects if e["kind"] != "fault"]
        clean_run = TrialRunner(world).trial(runner.snapshot(), [], Interval(start=scenario.instant("14:00"), end=scenario.instant("14:15")))
        assert clean_run.feasible
    finally:
        db.close()
