"""B17 场景世界冒烟测试：按任务卡 tests 逐条。"""
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest

from plantkg.core.contracts import Interval, PlantkgError, RunInfo, SimAction, StorageParams
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger
from plantkg.core.ports import AgentResultSource
from plantkg.world.scenario import TrialRunner, check_slot_summary, load_scenario, state_from_frames, summarize_slot
from plantkg.world.scenario.export import export_day
from plantkg.world.scenario.ledger import MinuteRecord
from plantkg.world.scenario.runner import ScenarioExecutor, _detach_inputs, default_registry, tracked
from plantkg.world.sim.base import MINUTE, MinuteInputs, WorldState

from .conftest import local, scenario

ROOT = Path(__file__).resolve().parents[3]
BAT = "entity://demo-electronics/battery/BAT1"
ADMIN = "entity://demo-electronics/zone/admin"


def discharge(start, minutes, kw=400.0):
    return SimAction(entity_id=BAT, command="discharge", params={"power_kw": kw}, interval=Interval(start=start, end=start + minutes * MINUTE))


class FakeSim:
    """假仿真器：所有设备按输入的基础负荷运行，不读动作。"""

    name = "fake"

    def init(self, state):
        state.devices["entity://demo-electronics/zone/admin"] = {"temperature": 25.0}

    def step(self, state, sim_actions, dt):
        state.power_kw.update(state.inputs.equipment_kw)
        return state


def test_minimal_script_runs_a_day_and_balances(make):
    sc = scenario(events=[{"at": "09:00", "type": "lighting_fault", "entity_id": "LT-A03", "summary": "照明故障"}])
    runner = make(sc, simulators=[FakeSim()])
    trace = runner.run()
    assert len(trace.ledger) == 1440 and all(m.balanced for m in trace.ledger)
    assert len(trace.slots) == 96 and all(s.minutes_balanced for s in trace.slots) and not trace.slot_problems
    assert [e.event_type for e in trace.events] == ["lighting_fault"]
    assert runner.db.runs.get_run(trace.run_id).status == "finished"


def test_same_script_same_seed_twice_is_identical(make):
    sc = load_scenario(ROOT / "configs/scenarios/extreme_peak_conflict.yaml")
    a = make(sc, name="a.db").run()
    b = make(sc, name="b.db").run()
    assert [m.entry for m in a.ledger] == [m.entry for m in b.ledger]
    assert a.states == b.states
    assert a.actions == b.actions  # 动作与来源（事件单、提案编号）都一致
    assert [r.agent_run_id for r in a.agent_runs] == [r.agent_run_id for r in b.agent_runs]


def test_storage_action_minute_ledger_slot_summary_and_fork(make):
    runner = make(scenario(initial={"soc": 0.5}))
    runner.run(until=local("08:05"))
    runner.apply(discharge(local("08:05"), 10))
    runner.run(until=local("08:10"))
    parent = runner.run_info
    parent_energy = runner.energy
    child = runner.fork(local("08:10"))
    assert child.forked_from == parent.run_id and child.forked_at == local("08:10")
    assert runner.snapshot().pack_states["storage"]["energy_kwh"] == parent_energy
    assert runner.db.runs.get_run(parent.run_id).status == "finished"
    trace = runner.run(until=local("08:30"))
    minutes = [m for m in trace.ledger if local("08:05") <= m.entry.interval.start < local("08:15")]
    assert len(minutes) == 10 and all(m.balanced for m in minutes)
    assert all(m.entry.discharge_kwh == pytest.approx(400 / 60) for m in minutes)
    store = runner.db.store
    slot = next(s for s in store.slot_summaries(child.run_id) if s.interval.start == local("08:00"))
    assert slot.discharge_kwh == pytest.approx(66.667, abs=1e-3) and slot.minutes_balanced
    before = [m for m in store.ledger_minutes(child.run_id) if local("08:05") <= m.entry.interval.start < local("08:10")]
    assert len(before) == 5 and all(m.entry.discharge_kwh > 0 for m in before)  # 分叉前的账本在子运行里可读
    assert len([m for m in store.ledger_minutes(parent.run_id) if m.entry.interval.start >= local("08:00")]) == 10
    assert store.snapshot(child.run_id, local("08:10"))["pack_states"]["storage"]["energy_kwh"] == parent_energy


def _minute(at, storage, **kw):
    meta = LedgerMeta(ledger_id="t", kind="execution", run_id="r", plan_id="scenario:r", plan_revision=0, version_manifest_id="vm", source_refs=("t",))
    energy = kw.pop("energy", 0.0)
    result = compute_ledger([SlotInput(interval=Interval(start=at, end=at + MINUTE), **kw)], meta, storage, energy)
    return MinuteRecord(entry=result.ledger.entries[0], balanced=result.ledger.balanced, violations=tuple(result.problems), capacity_violations=())


def test_counterexample_one_sums_minutes_not_average_power():
    t0 = local("12:00")
    mins = [_minute(t0 + i * MINUTE, None, pv_available_kw=600 if i < 5 else 0, load_kw=0 if i < 5 else 600, price_cny_per_kwh=1.0)
            for i in range(15)]
    assert all(m.balanced for m in mins)
    s = summarize_slot(t0, mins)
    assert s.grid_import_kwh == pytest.approx(100) and s.pv_curtailed_kwh == pytest.approx(50) and s.cost_cny == pytest.approx(100)
    assert not check_slot_summary(s)


def test_counterexample_two_charge_then_discharge_in_one_slot_is_legal():
    sp = StorageParams(capacity_kwh=1000, eta_c=1.0, eta_d=1.0, soc_min=0.1, soc_max=0.9)
    t0, energy, mins = local("12:00"), 500.0, []
    for i in range(15):
        m = _minute(t0 + i * MINUTE, sp, pv_available_kw=0, load_kw=100, charge_kw=60 if i < 5 else 0,
                    discharge_kw=0 if i < 5 else 60, price_cny_per_kwh=1.0, energy=energy)
        energy = m.entry.energy_end_kwh
        mins.append(m)
    assert all(m.balanced for m in mins)
    s = summarize_slot(t0, mins)
    assert s.charge_kwh == pytest.approx(5) and s.discharge_kwh == pytest.approx(10) and s.minutes_balanced
    assert check_slot_summary(s) == []


class SlowExpert:
    """live 结果源：每次调用让墙钟走 90 秒再返回确定性提案。"""

    source_kind = "nexent"

    def __init__(self, clock):
        self.clock, self.inner = clock, default_registry().resolve_mode(AgentResultSource, "deterministic", key="elevator")

    def get(self, kind, binding, payload):
        self.clock[0] += timedelta(seconds=90)
        return self.inner.get(kind, binding, payload)


def test_delayed_live_result_takes_effect_at_budget_and_matches_deterministic(make):
    events = [{"at": "08:05", "type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "summary": "拥堵"}]
    wall = [local("08:00") + timedelta(days=200)]
    registry = default_registry()
    registry.register_mode(AgentResultSource, "live", SlowExpert(wall), key="elevator")
    live = make(scenario(events=events), mode="live", registry=registry, wall=lambda: wall[0], name="live.db").run(until=local("09:00"))
    det = make(scenario(events=events), mode="evaluation", name="det.db").run(until=local("09:00"))
    run = live.agent_runs[0]
    assert run.status == "finished" and run.timing.latency_ms == 90_000 and run.source == "nexent"
    assert run.timing.effective_at == local("08:06") == run.timing.trigger_at + timedelta(minutes=1)
    assert [a["action"] for a in live.actions] == [a["action"] for a in det.actions]
    assert live.actions[0]["action"]["interval"]["start"] == local("08:06").isoformat().replace("+00:00", "Z")


def test_late_result_goes_to_history_only(make):
    events = [{"at": "08:05", "type": "load_spike", "entity_id": "AC-1", "summary": "突增"}]
    runner = make(scenario(events=events))
    runner.run(until=local("08:40"))
    proposal = runner.trace.proposals[0]
    slots_before = runner.db.store.slot_summaries(runner.run_info.run_id)
    actions_before = len(runner.trace.actions)
    assert runner.dispatcher.deliver(proposal) == 0  # 生效时刻 08:06 所在槽已汇总
    old_round = proposal.model_copy(update={"binding": proposal.binding.model_copy(update={"round": 9}),
                                            "timing": proposal.timing.model_copy(update={"effective_at": local("09:00")})})
    assert runner.dispatcher.deliver(old_round) == 0  # round 已过期
    assert len(runner.trace.actions) == actions_before
    assert runner.db.store.slot_summaries(runner.run_info.run_id) == slots_before
    history = [n for n in runner.trace.notes if n["kind"] == "history"]
    assert len(history) == 2 and "已结算" in history[0]["reason"] and "过期" in history[1]["reason"]


def test_trial_is_isolated_and_order_independent(make):
    runner = make(scenario())
    runner.run(until=local("08:00"))
    snap = runner.snapshot()
    state_before, ledger_before = runner.state.to_json(), len(runner.trace.ledger)
    trial = TrialRunner(runner.world)
    horizon = Interval(start=local("08:00"), end=local("09:00"))
    a = [discharge(local("08:00"), 30, 300)]
    b = [SimAction(entity_id=ADMIN, command="set_mode", params={"mode": "off"}, interval=Interval(start=local("08:00"), end=local("08:30")))]
    first = [trial.trial(snap, c, horizon) for c in (a, b)]
    second = [trial.trial(snap, c, horizon) for c in (b, a)][::-1]
    assert first == second
    best = lambda rs: min(range(2), key=lambda i: rs[i].soft_cost.cny)  # noqa: E731
    assert best(first) == best(second)
    assert runner.state.to_json() == state_before and len(runner.trace.ledger) == ledger_before
    assert runner.db.store.snapshot(runner.run_info.run_id, local("08:00")) is None  # 试算不写正式库


def stop_standby(runner):
    """备用机 AC-1 开局停机：设备专家的试探才有意义（备用机已在运行时只转人工）。"""
    start = runner.now
    runner.apply(SimAction(entity_id=runner.world.ids()["AC-1"], command="stop", params={}, interval=Interval(start=start, end=start + MINUTE)))


def test_fork_hands_over_temp_rule_and_open_ticket(make):
    sc = scenario(events=[{"at": "15:00", "id": "leak", "type": "compressor_pressure_drop", "entity_id": "AC-2", "summary": "压力异常"}],
                  operator_actions=[{"at": "12:30", "kind": "instruction",
                                     "payload": {"text": "行政楼保持 24 ℃", "zone": "admin", "setpoint_c": 24, "start": "13:00", "end": "17:00"}},
                                    {"at": "16:00", "kind": "upload", "payload": {"document": "空压机手册", "adds": ["compressor_leak_rule"]}}])
    runner = make(sc)
    stop_standby(runner)
    runner.run(until=local("16:00"))
    parent = runner.run_info
    stale = runner.trace.proposals[-1]  # 父运行第 1 轮的提案
    assert runner.trace.tickets["TK-001"]["status"] == "open"  # 缺规则：待诊断，未关闭
    trace = runner.run(until=local("18:00"))
    child = runner.run_info
    assert child.forked_from == parent.run_id and child.forked_at == local("16:00")
    assert trace.knowledge_versions[-1].version == 2
    assert trace.state_at(local("16:30"))[ADMIN]["setpoint"] == 24.0 and trace.state_at(local("17:30"))[ADMIN]["setpoint"] == 25.0
    assert runner.temp_rules.active(local("16:30")) and not runner.temp_rules.active(local("17:00"))
    t = trace.tickets["TK-001"]
    assert t["round"] == 2 and t["trigger_at"] == local("16:00").isoformat()
    new_round = [r for r in trace.agent_runs if r.binding.run_id == child.run_id]
    assert new_round and new_round[0].binding.knowledge_version == 2
    assert any(a["action"]["command"] == "start" and a["action"]["params"].get("trial") for a in trace.actions)  # 有规则也先试探
    assert runner.dispatcher.deliver(stale) == 0  # 父运行的迟到结果不在子运行执行
    assert "已不是当前运行" in trace.notes[-1]["reason"]
    store = runner.db.store  # 分叉恰在槽边界：分叉前的槽汇总与触发分叉的上传事件都在子运行里
    assert len(store.slot_summaries(child.run_id)) == len(trace.slots)
    assert any(e["event_type"] == "document_upload" for e in store.records(child.run_id, "event"))


def test_correct_resolved_ticket_after_fork(make):
    sc = scenario(events=[{"at": "09:00", "id": "light", "type": "lighting_fault", "entity_id": "LT-A03", "summary": "照明故障"}],
                  operator_actions=[{"at": "09:30", "kind": "upload", "payload": {"document": "手册", "adds": ["x"]}},
                                    {"at": "10:00", "kind": "correct", "payload": {"event": "light", "adds": ["y"], "summary": "纠正"}}])
    trace = make(sc).run(until=local("10:30"))
    assert trace.tickets["TK-001"]["status"] == "closed" and len(trace.runs) == 3


def test_fork_redispatch_uses_forked_at_and_keeps_origin(make):
    sc = scenario(events=[{"at": "08:05", "id": "leak", "type": "compressor_pressure_drop", "entity_id": "AC-2", "summary": "压力异常"}],
                  operator_actions=[{"at": "09:30", "kind": "upload", "payload": {"document": "空压机手册", "adds": ["compressor_leak_rule"]}}],
                  policy={"response_budget": {"layer12_min": 1, "layer3_min": 3}})
    runner = make(sc)
    stop_standby(runner)
    runner.run(until=local("09:30"))
    parent = runner.run_info
    parent_ticket = [r for r in runner.db.store.records(parent.run_id, "ticket") if r["ticket_id"] == "TK-001"][-1]
    trace = runner.run(until=local("10:00"))
    isolate = next(a for a in trace.actions if a["action"]["command"] == "start" and a["action"]["params"].get("trial"))  # 有规则也先试探，再决定是否隔离
    assert isolate["action"]["interval"]["start"] == local("09:31").isoformat().replace("+00:00", "Z")
    assert isolate["source"]["origin_event_at"] == local("08:05").isoformat()
    timing = trace.agent_runs[-1].timing
    assert timing.trigger_at == local("09:30") and timing.origin_event_at == local("08:05") and timing.effective_at == local("09:31")
    again = [r for r in runner.db.store.records(parent.run_id, "ticket") if r["ticket_id"] == "TK-001"][-1]
    assert again == parent_ticket and again["round"] == 1  # 父运行里的原记录不变


def test_capacity_violation_recorded_and_trial_infeasible(make):
    sc = scenario(params={"capacity_kw": {"TX1": 1000}},
                  events=[{"at": "08:05", "type": "load_spike", "entity_id": "AC-1", "summary": "突增",
                           "payload": {"effects": [{"kind": "extra_load", "kw": 600, "minutes": 5}]}}], dispatch={"load_spike": []})
    runner = make(sc)
    trace = runner.run(until=local("08:15"))
    hit = [m for m in trace.ledger if m.capacity_violations]
    assert hit and all("TX1" in v for m in hit for v in m.capacity_violations) and max(m.flows_kw["TX1"] for m in hit) > 1000
    assert trace.slots[-1].capacity_violations
    snap = runner.snapshot()
    charge = SimAction(entity_id=BAT, command="charge", params={"power_kw": 500}, interval=Interval(start=local("08:15"), end=local("08:45")))
    result = TrialRunner(runner.world).trial(snap, [charge], Interval(start=local("08:15"), end=local("08:45")))
    assert result.feasible is False and {v.kind for v in result.violations} == {"capacity"}
    assert TrialRunner(runner.world).trial(snap, [], Interval(start=local("08:15"), end=local("08:45"))).feasible


def test_state_series_rebuilds_trace_and_exports_day(make):
    runner = make(load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml"))
    trace = runner.run(until=local("00:00", day=2))
    frames = runner.db.store.frames(trace.run_id)
    assert len(frames) == 96
    for at in (local("08:05"), local("16:00"), local("14:37")):
        assert state_from_frames(frames, at) == trace.state_at(at)
    day = export_day(runner.db, trace.run_id, "2026-07-01")
    assert [r.run_id for r in day.runs] == [r.run_id for r in trace.runs] and len(day.runs) == 4
    assert len(day.ledger_minutes) == 1440 and len(day.state_series) == 96
    assert day.proposals and day.agent_runs and all(r.data_sources for r in day.runs)  # 提案与运行记录按链导出
    assert state_from_frames(day.state_series, local("16:00")) == trace.state_at(local("16:00"))


def test_executor_only_accepts_scenario_runs(make):
    runner = make(scenario())
    replay = RunInfo.model_validate(runner.run_info.model_dump() | {"kind": "replay_day"})
    with pytest.raises(PlantkgError):
        ScenarioExecutor(runner.world).execute_minute(replay, runner.state, runner.energy)


@pytest.mark.parametrize("path", sorted((ROOT / "configs/scenarios").glob("*.yaml")), ids=lambda p: p.stem)
def test_every_script_loads_without_code_changes(path):
    sc = load_scenario(path)
    assert sc.nature == "synthetic" and sc.expanded_events() or sc.operator_actions


def test_closed_loop_action_changes_later_state(make):
    runner = make(scenario())
    runner.run(until=local("08:00"))
    snap = runner.snapshot()
    trial = TrialRunner(runner.world)
    horizon = Interval(start=local("08:00"), end=local("08:30"))
    off = SimAction(entity_id=ADMIN, command="set_mode", params={"mode": "off"}, interval=horizon)
    base, cut = trial.trial(snap, [], horizon), trial.trial(snap, [off], horizon)
    assert cut.soft_cost.cny < base.soft_cost.cny and cut.soft_cost.comfort_min >= base.soft_cost.comfort_min


class StaleBindingExpert:
    """结果源：返回已绑定旧运行（或旧轮次）的合法提案，模拟分叉前在途结果迟到。"""

    source_kind = "nexent"

    def __init__(self, **stale):
        self.stale, self.inner = stale, default_registry().resolve_mode(AgentResultSource, "deterministic", key="energy")

    def get(self, kind, binding, payload):
        result = self.inner.get(kind, binding, payload)
        old = binding.model_copy(update=self.stale)
        return result | {"proposal": dict(result["proposal"]) | {"proposal_id": "pr-old", "binding": old.model_dump(mode="json")}}


@pytest.mark.parametrize("stale,reason", [({"run_id": "run-parent"}, "已不是当前运行"), ({"round": 9}, "过期")])
def test_request_keeps_stale_binding_and_sends_to_history(make, stale, reason):
    registry = default_registry()
    registry.register_mode(AgentResultSource, "evaluation", StaleBindingExpert(**stale), key="energy")
    events = [{"at": "08:05", "type": "load_spike", "entity_id": "AC-1", "summary": "突增"}]
    trace = make(scenario(events=events), registry=registry).run(until=local("08:30"))
    prop = trace.proposals[0]
    assert prop.proposal_id == "pr-old" and prop.binding.model_dump(include=set(stale)) == stale  # 不被改绑到当前运行
    assert not [a for a in trace.actions if a["source"].get("expert") == "energy"]
    assert any(n["kind"] == "history" and reason in n["reason"] for n in trace.notes)


def test_confirmed_repair_executes_once(make):
    sc = scenario(events=[{"at": "08:05", "id": "fault", "type": "equipment_fault", "entity_id": "AC-1", "summary": "故障"}],
                  operator_actions=[{"at": "08:10", "kind": "confirm", "payload": {"event": "fault"}},
                                    {"at": "08:20", "kind": "confirm", "payload": {"event": "fault"}}])
    trace = make(sc).run(until=local("08:40"))
    repairs = [a for a in trace.actions if a["action"]["command"] == "repair"]
    assert len(repairs) == 1  # 确认后执行，重复确认不再执行
    assert sorted(wo.status for wo in trace.work_orders.values()) == ["confirmed"]
    assert trace.tickets["TK-001"]["status"] == "resolved"
    assert any(n["kind"] == "confirm_ignored" for n in trace.notes)


def test_hourly_inputs_continuous_across_utc_and_local_midnight(make):
    inputs = make(scenario(days=2)).world.inputs
    t0 = local("00:00")
    for days in (inputs.pv_days, inputs.t2m_days):
        flat = [v for d in days for v in d]
        step = max(abs(b - a) for a, b in zip(flat, flat[1:] + flat[:1])) / 60
        series = [inputs._hourly(days, t0 + m * MINUTE) for m in range(2 * 24 * 60)]
        assert max(abs(b - a) for a, b in zip(series, series[1:])) <= step + 1e-9  # 逐分钟不跳变
    for hhmm in ("08:00", "00:00"):  # 当地 08:00 = UTC 0 点；当地 0 点；第 2 天的同一时刻
        for day in (1, 2):
            t = local(hhmm, day)
            assert abs(inputs.pv_kw(t) - inputs.pv_kw(t - MINUTE)) < 5 and abs(inputs.outdoor_c(t) - inputs.outdoor_c(t - MINUTE)) < 0.1
    assert inputs.pv_kw(local("00:30")) == 0 and inputs.pv_kw(local("12:00")) > 0


def test_trial_freezes_inputs_per_snapshot(make):
    runner = make(scenario())
    runner.run(until=local("08:00"))
    snap, horizon = runner.snapshot(), Interval(start=local("08:00"), end=local("09:00"))
    trial = TrialRunner(runner.world)
    a = [discharge(local("08:00"), 30, 300)]
    before = [trial.trial(snap, c, horizon) for c in ((), a)]
    inputs = runner.world.inputs
    inputs.prices[:] = [p * 2 for p in inputs.prices]  # 输入源刷新电价
    after = [trial.trial(snap, c, horizon) for c in (a, ())][::-1]
    assert before == after  # 同一快照的候选共用冻结输入，评估顺序与输入更新都不影响结果
    assert inputs.minute(local("08:00")).price_cny_per_kwh == 2 * before[0].minute_ledger[0].price_cny_per_kwh  # 正式运行仍读最新输入


class ShiftingExpert:
    """结果源：第一次提议停 AC-1（needs_human），之后改提停 AC-2，模拟确认后专家改主意。"""

    source_kind = "nexent"

    def __init__(self):
        self.inner, self.calls = default_registry().resolve_mode(AgentResultSource, "deterministic", key="equipment"), 0

    def get(self, kind, binding, payload):
        self.calls += 1
        result = self.inner.get(kind, binding, payload)
        target = payload["ids"]["AC-1" if self.calls == 1 else "AC-2"]
        prop = dict(result["proposal"])
        prop["actions"] = [dict(prop["actions"][0]) | {"entity_id": target, "command": "stop"}]
        return result | {"proposal": prop}


def test_confirm_executes_only_approved_actions(make):
    registry = default_registry()
    registry.register_mode(AgentResultSource, "evaluation", ShiftingExpert(), key="equipment")
    sc = scenario(events=[{"at": "08:05", "id": "fault", "type": "equipment_fault", "entity_id": "AC-1", "summary": "故障"}],
                  operator_actions=[{"at": "08:10", "kind": "confirm", "payload": {"event": "fault"}}])
    runner = make(sc, registry=registry)
    trace = runner.run(until=local("08:30"))
    approved = [(a.entity_id, a.command, a.params) for wo in trace.work_orders.values() for a in wo.actions]
    done = [(a["action"]["entity_id"], a["action"]["command"], a["action"]["params"]) for a in trace.actions if a["source"].get("work_order_id")]
    assert done == approved and approved[0][0] == runner.world.ids()["AC-1"]  # 执行的就是批准的那条，不是确认后新提的 AC-2


def test_confirm_leak_repair_does_not_replay_isolation(make):
    sc = scenario(events=[{"at": "08:30", "id": "leak", "type": "compressor_pressure_drop", "entity_id": "AC-2", "severity": 3, "summary": "压力下降"}],
                  operator_actions=[{"at": "08:00", "kind": "upload", "payload": {"document": "手册", "adds": ["compressor_leak_rule"]}},
                                    {"at": "08:40", "kind": "confirm", "payload": {"event": "leak"}}])
    trace = make(sc).run(until=local("09:00"))
    commands = [a["action"]["command"] for a in trace.actions]
    assert commands.count("isolate_leak") == 1 and commands.count("repair") == 1


class OwnTimingExpert:
    """结果源：绑定合法，但自带的 timing 把生效时刻排到 1 小时后。"""

    source_kind = "nexent"

    def __init__(self):
        self.inner = default_registry().resolve_mode(AgentResultSource, "deterministic", key="energy")

    def get(self, kind, binding, payload):
        result = self.inner.get(kind, binding, payload)
        t = binding.trigger_at
        timing = {"origin_event_at": t, "trigger_at": t, "effective_at": t + timedelta(hours=1), "requested_at": t}
        return result | {"proposal": dict(result["proposal"]) | {"binding": binding.model_dump(mode="json"),
                                                                 "timing": {k: v.isoformat() for k, v in timing.items()}}}


def test_source_timing_must_match_response_budget(make):
    registry = default_registry()
    registry.register_mode(AgentResultSource, "evaluation", OwnTimingExpert(), key="energy")
    events = [{"at": "08:05", "type": "load_spike", "entity_id": "AC-1", "summary": "突增"}]
    trace = make(scenario(events=events), registry=registry).run(until=local("09:30"))
    assert not [a for a in trace.actions if a["source"].get("expert") == "energy"]
    assert any(n["kind"] == "history" and "响应预算" in n["reason"] for n in trace.notes)
    assert [r.timing.effective_at for r in trace.agent_runs if r.agent_name == "energy"] == [local("08:06")]


def test_snapshot_inputs_shared_across_trial_runners_and_horizons(make):
    runner = make(scenario())
    runner.run(until=local("08:00"))
    snap = runner.snapshot()
    inputs = runner.world.inputs
    short = TrialRunner(runner.world).trial(snap, (), Interval(start=local("08:00"), end=local("08:05")))
    inputs.prices[:] = [p * 2 for p in inputs.prices]  # 刷新电价
    long = TrialRunner(runner.world).trial(snap, (), Interval(start=local("08:00"), end=local("08:15")))  # 新实例、更长 horizon
    prices = {e.price_cny_per_kwh for e in long.minute_ledger}
    assert long.minute_ledger[:5] == short.minute_ledger and prices == {short.minute_ledger[0].price_cny_per_kwh}


@pytest.mark.parametrize("budget", [1, 3])
def test_confirmed_actions_wait_for_response_budget(make, budget):
    sc = scenario(events=[{"at": "14:20", "id": "trap", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5, "summary": "困人"}],
                  operator_actions=[{"at": "14:30", "kind": "confirm", "payload": {"event": "trap"}},
                                    {"at": "14:40", "kind": "confirm", "payload": {"event": "trap"}}],
                  policy={"response_budget": {"layer12_min": budget, "layer3_min": 3}})
    runner = make(sc)
    car = runner.world.ids()["FE-2"]
    effective = local("14:30") + timedelta(minutes=budget)
    runner.run_until(effective)  # 预算到期前（生效分钟尚未推进）仍困人
    assert runner.state.devices[car]["trapped"]
    runner.step()
    assert not runner.state.devices[car]["trapped"]
    trace = runner.run_until(local("15:00"))
    confirmed = [a for a in trace.actions if a["source"].get("work_order_id")]
    assert {a["action"]["interval"]["start"] for a in confirmed} == {effective.isoformat().replace("+00:00", "Z")}
    assert {a["source"]["trigger_at"] for a in confirmed} == {local("14:30").isoformat()}
    assert [a["action"]["command"] for a in confirmed].count("rescue") == 1  # 重复确认不重复执行
    assert trace.tickets["TK-001"]["trigger_at"] == local("14:30").isoformat()


def test_fork_redispatch_does_not_replay_executed_actions(make):
    sc = scenario(events=[{"at": "08:30", "id": "leak", "type": "compressor_pressure_drop", "entity_id": "AC-2", "severity": 3, "summary": "压力下降"}],
                  operator_actions=[{"at": "08:00", "kind": "upload", "payload": {"document": "手册", "adds": ["compressor_leak_rule"]}},
                                    {"at": "09:00", "kind": "upload", "payload": {"document": "补充", "adds": ["x"]}}])
    trace = make(sc).run(until=local("09:30"))
    assert len(trace.runs) == 3 and [a["action"]["command"] for a in trace.actions].count("isolate_leak") == 1
    assert any(n["kind"] == "skipped_duplicate" for n in trace.notes)


def test_tracked_snapshot_does_not_share_device_fields_with_live_state():
    """tracked 快照与实时 state 不共享内层对象（浅拷贝契约）：改一边不能改到另一边。"""
    live = {"entity://demo-electronics/battery/BAT1": {"soc": 0.5, "level": 2}}
    state = WorldState(at=local("00:00"), devices=live)
    snap = tracked(state)
    assert snap == state.devices and snap is not state.devices
    assert snap["entity://demo-electronics/battery/BAT1"] is not live["entity://demo-electronics/battery/BAT1"]
    snap["entity://demo-electronics/battery/BAT1"]["soc"] = 0.0
    assert live["entity://demo-electronics/battery/BAT1"]["soc"] == 0.5
    live["entity://demo-electronics/battery/BAT1"]["level"] = 3
    assert snap["entity://demo-electronics/battery/BAT1"]["level"] == 2


def test_detach_inputs_copies_every_minute_field():
    """_detach_inputs 拷贝所有 dict 字段；MinuteInputs 加字段时这条测试会提醒同步（浅拷贝契约）。"""
    expected = {"at", "equipment_kw", "pv_kw", "price_cny_per_kwh", "outdoor_c", "occupancy"}
    actual = {f.name for f in fields(MinuteInputs)}
    assert actual == expected, f"MinuteInputs 字段变了：同步 runner._detach_inputs 的拷贝逻辑（当前按 {sorted(expected)} 拷贝）"
    value = MinuteInputs(at=local("00:00"), equipment_kw={"PV": 1.0}, pv_kw=2.0, price_cny_per_kwh=None,
                         outdoor_c=30.0, occupancy={"admin": 3})
    detached = _detach_inputs(value)
    assert detached == value and detached is not value
    assert detached.equipment_kw is not value.equipment_kw and detached.occupancy is not value.occupancy
    detached.equipment_kw["PV"] = 9.0
    detached.occupancy["admin"] = 0
    assert value.equipment_kw["PV"] == 1.0 and value.occupancy["admin"] == 3
