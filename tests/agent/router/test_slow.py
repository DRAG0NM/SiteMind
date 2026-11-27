"""B27 增量冒烟：严重程度入口、快慢双通道、investigate、慢通道工具与 D61 试探接续。"""

from __future__ import annotations

import json
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from plantkg.agent.router import (
    EXPERTS_HOOK, PROBE_HOOK, PROPOSALS_READY_HOOK, URGENT_HOOK, Router, RouterConfig, RouterStore, SqliteProposalSink, install,
)
from plantkg.agent.router.severity import EventTypeRuleMatcher, SeverityEstimator
from plantkg.agent.router.triage import DeterministicInvestigation, parse_investigation
from plantkg.core.contracts import AgentResultKind, ErrorCode, Interval, PlantkgError, ProposalCost, ProposedAction, RunBinding, TrialResult, TrialViolation
from plantkg.core.ports import RUN_BINDING_HOOK, AgentResultSource
from plantkg.core.registry import Registry
from plantkg.integrations.nexent import LiveAgentResultSource
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from tests.agent.router.helpers import (
    AIR, LIGHT, PLANT, T0, CountingJudge, binding, make_event, make_proposal, run_info,
)

BASE = {"horizon_min": 60, "major_loss_cny": 50_000, "line_value_cny_per_min": 180, "stop_minutes": 30,
        "production_lines": ["LINE-1", "LINE-2"], "stop_event_types": [], "safety_event_types": [],
        "rule_covered_events": [], "equipment_damage_cny": {"air_compressor": 12000, "default": 0}}


def capacity_violations(minutes, node="TX1"):
    """形状与 B17 TrialRunner 一致：capacity 违规没有 entity_id，节点在 detail 里。"""
    return tuple(TrialViolation(kind="capacity", at=T0 + timedelta(minutes=m),
                                detail=f"{(T0 + timedelta(minutes=m)).isoformat()} {node} 1200.0 kW > 容量 1150.0 kW") for m in minutes)


class Env:
    def __init__(self, tmp_path, **kwargs):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.writer = SQLiteWriter(tmp_path / "router.db")
        MigrationRunner(self.writer).apply()
        self.registry = Registry()
        self.registry.live_fallback = True
        self.judge = CountingJudge()
        options = {"severity": SeverityEstimator(dict(BASE)),
                   "rule_matcher": EventTypeRuleMatcher({"rule_covered_events": []})}
        options.update(kwargs)
        self.router = install(self.registry, self.writer, PLANT, run_id="run-1", knowledge_version=3,
                              judge=self.judge, mode="evaluation", state_version=7, **options)
        self.sink: SqliteProposalSink = self.router.sink
        self.store: RouterStore = self.router.store

    def close(self):
        self.writer.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


def replace_source(env, key, impl):
    env.registry.mode_ports[(AgentResultSource, "deterministic", key)] = impl


class SlowSource:
    """慢通道调查的替身：指定专家、可返回试探提案。"""

    name = "investigate"

    def __init__(self, experts=("energy",), probe=None, diagnosis="调查结论", follow_probe=None):
        self.experts, self.probe, self.diagnosis, self.follow_probe = list(experts), probe, diagnosis, follow_probe
        self.calls = []

    def get(self, kind, binding, payload):
        self.calls.append(dict(payload))
        result = {"status": "finished", "diagnosis": self.diagnosis, "experts": self.experts,
                  "evidence_refs": [str(payload.get("event", {}).get("entity_id") or "")], "coverage_gap": False}
        probe = self.follow_probe if payload.get("reason") == "probe" else self.probe
        if probe:
            result["probe"] = probe
        return result


class OfflineGateway:
    """不联网的 Gateway 替身：记下真正要发出去的 query，按序返回 final_answer；异常原样抛出。"""

    main_agent = "plantkg_main"

    def __init__(self, *answers):
        self.answers, self.queries = list(answers), []

    def prepare(self, actor, query, agent, as_of, **kwargs):
        self.queries.append(query)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, text = answer if isinstance(answer, tuple) else ("finished", answer)
        return SimpleNamespace(record=SimpleNamespace(status=status, agent_run_id="ar-1", terminal_reason=None, final_answer=text,
                                                      timing=SimpleNamespace(latency_ms=1)))

    def stream(self, run):
        return ()


def go_live(env, *answers):
    gateway = OfflineGateway(*answers)
    settings = SimpleNamespace(nexent=SimpleNamespace(run_retries=0))
    env.router.mode = "live"
    env.registry.mode_ports[(AgentResultSource, "live", "investigate")] = LiveAgentResultSource(gateway, settings)
    return gateway


def answer(experts=("hvac",), probe=None):
    return json.dumps({"diagnosis": "冷却塔堵塞", "experts": list(experts), "evidence_refs": ["entity://p1/hvac/CT-1"], "probe": probe},
                      ensure_ascii=False)


LIVE_PROBE = {"actions": [{"entity_id": AIR, "command": "set_mode", "params": {"mode": "off"}}], "window_min": 3,
              "hypothesis": "假设甲：供电侧", "expectations": ["功率下降"]}


def test_safety_event_is_urgent_without_jev_or_main_agent(tmp_path):
    severity = SeverityEstimator(dict(BASE) | {"safety_event_types": ["elevator_trapped"]})
    e = Env(tmp_path, severity=severity)
    try:
        urgent, investigations = [], []
        e.registry.register_hook(URGENT_HOOK, lambda event, reason: urgent.append((event.event_id, reason)))
        jev_calls = e.judge.calls
        handled = e.router.handle_event(make_event("EV-S", 0, "elevator_trapped", "entity://p1/elevator/FE-2", severity=5))
        assert handled.result == "urgent" and handled.channel == "urgent"
        assert urgent and "安全标记" in urgent[0][1]
        assert e.judge.calls == jev_calls and not e.router.runs
        assert e.store.channel_of(handled.ticket_id) == "urgent"
        assert e.store.investigation(handled.ticket_id, 1) is None
    finally:
        e.close()


def test_urgent_escalation_passes_denied_actions_to_registered_handler(tmp_path):
    e = Env(tmp_path)
    try:
        event = make_event("EV-ACTION-SAFETY", 0, "load_spike")
        handled = e.router.handle_event(event)
        ticket_id = handled.ticket_id
        action = ProposedAction(entity_id=AIR, command="isolate_leak", params={},
                                interval=Interval(start=T0, end=T0 + timedelta(minutes=1)))
        urgent = []
        e.registry.register_hook(URGENT_HOOK, lambda got, reason: urgent.append((got, reason)))

        e.router.escalate(ticket_id, "action_safety", denied_actions=(action,))

        got, reason = urgent[0]
        assert reason == "action_safety"
        assert got.payload["denied_actions"] == [action.model_dump(mode="json")]
        assert e.store.channel_of(ticket_id) == "urgent"
    finally:
        e.close()


def test_safety_actions_append_once_after_ticket_is_already_urgent(tmp_path):
    severity = SeverityEstimator(dict(BASE) | {"safety_event_types": ["load_spike"]})
    e = Env(tmp_path, severity=severity)
    try:
        work_order = {"actions": []}
        calls = []

        def urgent_handler(event, reason):
            calls.append((event, reason))
            for action in event.payload.get("denied_actions", []):
                if action not in work_order["actions"]:
                    work_order["actions"].append(action)

        e.registry.register_hook(URGENT_HOOK, urgent_handler)
        handled = e.router.handle_event(make_event("EV-URGENT-THEN-VETO", 0, "load_spike"))
        action = ProposedAction(entity_id=AIR, command="isolate_leak", params={},
                                interval=Interval(start=T0, end=T0 + timedelta(minutes=1)))

        e.router.escalate(handled.ticket_id, "action_safety", denied_actions=(action,))
        e.router.escalate(handled.ticket_id, "action_safety", denied_actions=(action,))

        assert e.store.channel_of(handled.ticket_id) == "urgent"
        assert len(calls) == 2
        assert work_order["actions"] == [action.model_dump(mode="json")]
    finally:
        e.close()


def test_loss_over_line_is_urgent_below_line_is_not(tmp_path):
    high = Env(tmp_path / "a", severity=SeverityEstimator(dict(BASE) | {"stop_minutes": 0, "equipment_damage_cny": {"default": 60_000}}))
    try:
        handled = high.router.handle_event(make_event("EV-L", 0, "equipment_fault", AIR))
        assert handled.result == "urgent" and high.router.severity.estimate(make_event("EV-L", 0, "equipment_fault", AIR)).loss_cny == 60_000
    finally:
        high.close()
    low = Env(tmp_path / "b", severity=SeverityEstimator(dict(BASE) | {"stop_minutes": 0, "equipment_damage_cny": {"default": 1_000}}))
    try:
        assert low.router.handle_event(make_event("EV-L", 0, "equipment_fault", AIR)).result != "urgent"
    finally:
        low.close()


def test_production_stop_without_loss_line_still_urgent(tmp_path):
    severity = SeverityEstimator(dict(BASE) | {"major_loss_cny": 10**9, "stop_minutes": 30, "line_value_cny_per_min": 0,
                                              "stop_event_types": ["compressor_pressure_drop"], "equipment_damage_cny": {"default": 0}})
    e = Env(tmp_path, severity=severity)
    try:
        handled = e.router.handle_event(make_event("EV-P", 0, "compressor_pressure_drop", AIR))
        assert handled.result == "urgent"
        estimate = e.store.severity_of(handled.ticket_id)
        assert estimate["production_stop"] is True and estimate["loss_cny"] == 0
    finally:
        e.close()


def test_electricity_only_event_is_not_urgent(tmp_path):
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        handled = e.router.handle_event(make_event("EV-E", 0, "lighting_fault", LIGHT, severity=2))
        assert handled.result == "opened" and handled.channel == "fast"
        assert e.store.severity_of(handled.ticket_id)["loss_cny"] == 0
    finally:
        e.close()


def test_unmatched_rule_goes_slow_and_matched_rule_goes_fast(tmp_path):
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        slow = e.router.handle_event(make_event("EV-X", 0, "unknown_combo", AIR))
        assert slow.result == "opened" and slow.channel == "slow"
        assert e.store.channel_of(slow.ticket_id) == "slow"
        assert e.store.investigation(slow.ticket_id, 1) is not None
        investigations = len([r for r in e.router.runs if r.kind == AgentResultKind.investigate])
        jev_calls = e.judge.calls
        fast = e.router.handle_event(make_event("EV-F", 3, "lighting_fault", LIGHT, severity=2))
        assert fast.channel == "fast" and e.store.investigation(fast.ticket_id, 1) is None
        assert len([r for r in e.router.runs if r.kind == AgentResultKind.investigate]) == investigations
        assert e.judge.calls > jev_calls
    finally:
        e.close()


def test_slow_channel_dispatches_experts_named_by_main_agent(tmp_path):
    e = Env(tmp_path)
    try:
        replace_source(e, "investigate", SlowSource(experts=("hvac", "energy")))
        dispatched = []
        e.registry.register_hook(EXPERTS_HOOK, lambda ticket, b, experts: dispatched.append((ticket.ticket_id, tuple(experts))))
        handled = e.router.handle_event(make_event("EV-S1", 0, "unknown_combo", AIR))
        assert handled.channel == "slow" and handled.experts == ("hvac", "energy")
        assert dispatched == [(handled.ticket_id, ("hvac", "energy"))]
    finally:
        e.close()


def test_trial_mode_falls_back_to_deterministic_investigate_with_coverage_gap(tmp_path):
    e = Env(tmp_path)
    try:
        e.registry.mode_ports.pop((AgentResultSource, "deterministic", "investigate"))
        replace_source(e, "investigate", DeterministicInvestigation())
        handled = e.router.handle_event(make_event("EV-D", 0, "unknown_combo", AIR))
        runs = [r for r in e.router.runs if r.kind == AgentResultKind.investigate]
        assert runs and runs[0].coverage_gap is True and runs[0].source == "deterministic"
        assert runs[0].run_mode == "deterministic"
        assert e.store.investigation(handled.ticket_id, 1)["coverage_gap"] is True
    finally:
        e.close()


def test_residual_alarm_on_straight_through_opens_next_round(tmp_path):
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        first = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2))
        jev_calls = e.judge.calls
        residual = make_event("EV-2", 4, "lighting_fault", LIGHT, severity=2)
        residual = residual.model_copy(update={"payload": {"residual_alarm": True}})
        second = e.router.handle_event(residual)
        assert second.result == "straight_through" and second.ticket_id == first.ticket_id
        assert e.judge.calls == jev_calls  # 不重新研判
        assert e.store.ticket(first.ticket_id).round == 2
        investigation = e.store.investigation(first.ticket_id, 2)
        assert investigation is not None and investigation["evidence_refs"]
    finally:
        e.close()


PROBE = {"actions": [{"entity_id": AIR, "command": "set_mode", "params": {"mode": "off"},
                      "interval": {"start": T0.isoformat(), "end": (T0 + timedelta(minutes=3)).isoformat()}}],
         "window_min": 3, "hypothesis": "读数反应可区分供电侧与设备侧"}


def test_probe_window_then_follow_up_with_fresh_readings(tmp_path):
    east8 = timezone(timedelta(hours=8))
    west5 = timezone(timedelta(hours=-5))
    readings = [(T0 - timedelta(minutes=1), 1.0, "+00:00"),  # 试探前，旧读数
                (T0 - timedelta(minutes=1), 1.5, "+08:00"),  # 同样是旧读数：字面比较会误判成新读数
                (T0 + timedelta(minutes=1), 2.0, "-05:00")]  # 试探后：字面比较会误判成旧读数
    zones = {"+00:00": T0.tzinfo, "+08:00": east8, "-05:00": west5}
    seen_entities = []

    def source_readings(entity, start, end):
        seen_entities.append(entity)  # D61：读观测层用实体 ID，不是 event_id
        return [{"at": at.astimezone(zones[zone]).isoformat(), "value": value, "quality": "ok"}
                for at, value, zone in readings if start <= at <= end]

    e = Env(tmp_path, readings=source_readings)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        probes = []
        e.registry.register_hook(PROBE_HOOK, lambda ticket, proposal, b: probes.append(proposal))
        handled = e.router.handle_event(make_event("EV-PR", 0, "unknown_combo", AIR))
        assert probes and probes[0].window_min == 3
        assert e.store.ticket(handled.ticket_id).status == "waiting"
        assert e.store.probe_of(handled.ticket_id)["state"] == "executing"

        e.router.step(T0 + timedelta(minutes=3))
        follow = [call for call in source.calls if call.get("reason") == "probe"]
        assert [r["value"] for r in follow[0]["readings_after"]] == [2.0]
        assert [r["value"] for r in follow[0]["readings_before"]] == [1.0, 1.5]  # 试探前基线也注入，不混入试探后读数
        assert all(entity == AIR for entity in seen_entities)
        assert follow[0]["probe_incomplete"] is False
        assert e.store.probe_of(handled.ticket_id)["state"] == "done"
        assert e.store.ticket(handled.ticket_id).round == 2
    finally:
        e.close()


def test_closed_ticket_probe_does_not_resume(tmp_path):
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        handled = e.router.handle_event(make_event("EV-CL", 0, "unknown_combo", AIR))
        e.router.close(handled.ticket_id, T0 + timedelta(minutes=1), "值班人员关闭")
        e.router.step(T0 + timedelta(minutes=5))
        assert [c for c in source.calls if c.get("reason") == "probe"] == []
        assert e.store.ticket(handled.ticket_id).status == "closed"
    finally:
        e.close()


def test_fork_skips_waiting_probe_and_urgent_tickets(tmp_path):
    e = Env(tmp_path)
    try:
        normal = e.router.handle_event(make_event("EV-N1", 0, "unknown_combo", AIR))
        waiting = e.router.handle_event(make_event("EV-W1", 1, "unknown_combo", "entity://p1/air_system/AC-3"))
        urgent = e.router.handle_event(make_event("EV-U1", 2, "equipment_fault", "entity://p1/transformer/TX1"))
        e.router.escalate(urgent.ticket_id, "测试：转操作工单")
        assert e.store.channel_of(urgent.ticket_id) == "urgent"
        e.store.set_ticket_status(waiting.ticket_id, "waiting")
        e.store.save_probe(waiting.ticket_id, {"proposal": {"round": 1}, "state": "executing",
                                               "effective_at": T0.isoformat(), "window_end": (T0 + timedelta(minutes=99)).isoformat()})
        e.router.on_fork(run_info("run-1"), run_info("run-2"), T0 + timedelta(minutes=10))
        dispatched = e.router.resume_after_fork()
        assert [d.ticket.ticket_id for d in dispatched] == [normal.ticket_id]
    finally:
        e.close()


def test_repeated_residual_alarm_opens_only_one_round(tmp_path):
    """残差只在“正常 → 报警”跳变时开轮；持续报警不再因残差开新一轮（普通直通重派另见直通测试）。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}),
            config=RouterConfig(redispatch_severity=99))
    try:
        first = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2))
        residual = make_event("EV-2", 2, "lighting_fault", LIGHT, severity=2).model_copy(
            update={"payload": {"residual_alarm": True}})
        e.router.handle_event(residual)
        round_after_first = e.store.ticket(first.ticket_id).round
        again = make_event("EV-3", 4, "lighting_fault", LIGHT, severity=2).model_copy(
            update={"payload": {"residual_alarm": True}})
        e.router.handle_event(again)
        assert e.store.ticket(first.ticket_id).round == round_after_first  # 持续报警不重复开轮
    finally:
        e.close()


def test_policy_major_loss_line_changes_urgent_outcome(tmp_path):
    from plantkg.core.contracts import PolicyConfig, ResponseBudget

    def policy(line):
        return PolicyConfig(policy_version="p1", arbitration_weights={}, priority_profiles={"default": ("safety",)},
                            response_budget=ResponseBudget(layer12_min=1, layer3_min=3), major_loss_cny=line)

    def build(path, line):
        estimator = SeverityEstimator(dict(BASE) | {"stop_minutes": 0, "equipment_damage_cny": {"default": 60_000}},
                                      policy=policy(line))
        return Env(path, severity=estimator)

    low = build(tmp_path / "low", 10_000)
    high = build(tmp_path / "high", 10**9)
    try:
        assert low.router.handle_event(make_event("EV-L1", 0, "equipment_fault", AIR)).result == "urgent"
        assert high.router.handle_event(make_event("EV-L2", 0, "equipment_fault", AIR)).result != "urgent"
    finally:
        low.close()
        high.close()


def test_severity_runs_unprotected_trial_from_runner(tmp_path):
    calls = []

    def trial(snapshot, candidate, horizon):
        calls.append((snapshot, tuple(candidate), horizon))
        return TrialResult(feasible=False, violations=capacity_violations(range(5)), soft_cost=ProposalCost(cny=1.0))

    types = {"LINE-1": "production_line", "AC-1": "air_compressor", "AHU-P1": "ahu"}
    estimator = SeverityEstimator(dict(BASE) | {"capacity_minutes": 5, "capacity_ratio": 0.95,
                                                "downstream_stop_types": ["production_line", "air_compressor"]},
                                  trial=trial, snapshot=lambda event: event.event_id,
                                  downstream_lines=lambda node: list(types) if node == "TX1" else [],
                                  entity_type=types.get)
    e = Env(tmp_path, severity=estimator)
    try:
        urgent = []
        e.registry.register_hook(URGENT_HOOK, lambda event, reason: urgent.append(reason))
        handled = e.router.handle_event(make_event("EV-TR", 0, "lighting_fault", LIGHT, severity=2))
        assert calls[0][0] == "EV-TR" and calls[0][1] == ()  # 当前快照，不采取任何动作
        assert calls[0][2].end - calls[0][2].start == timedelta(minutes=60)
        assert handled.channel == "urgent" and "跳闸 TX1" in urgent[0]
        estimate = e.store.severity_of(handled.ticket_id)
        # 下游只算生产线与空压站：30 分钟 × 2 个对象 × 180 元
        assert estimate["production_stop"] is True and estimate["loss_cny"] == 30 * 2 * 180
    finally:
        e.close()


def test_trial_stop_counts_minutes_by_timestamp_not_violation_rows():
    """P1-3：同一分钟 5 条、断续 5 分钟都不算持续 5 分钟；低于 capacity_ratio 的超容也不算。"""
    def stopped(violations, **config):
        estimator = SeverityEstimator(dict(BASE) | {"capacity_minutes": 5, "capacity_ratio": 0.95} | config, snapshot=lambda event: "s",
                                      trial=lambda snap, candidate, horizon: TrialResult(feasible=False, violations=violations,
                                                                                         soft_cost=ProposalCost(cny=0.0)))
        return estimator.estimate(make_event("EV", 0, "lighting_fault", LIGHT)).production_stop

    assert stopped(capacity_violations([0, 0, 0, 0, 0])) is False
    assert stopped(capacity_violations([0, 1, 3, 4, 6])) is False
    assert stopped(capacity_violations(range(5))) is True
    assert stopped(capacity_violations(range(5)), capacity_ratio=1.5) is False
    assert stopped(capacity_violations(range(3)) + capacity_violations(range(3, 6), node="TX2")) is False  # 不同节点不累加


def test_running_residual_transition_gets_investigation_after_round(tmp_path):
    """P1-4：专家在跑时的残差跳变不被消费掉，complete_round 后补慢通道调查。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        e.registry.register_hook(EXPERTS_HOOK, lambda ticket, b, experts: None)  # 不结束：专家保持运行中
        first = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2))
        residual = make_event("EV-2", 2, "lighting_fault", LIGHT, severity=2).model_copy(update={"payload": {"residual_alarm": True}})
        assert e.router.handle_event(residual).result == "straight_through"
        assert e.store.ticket(first.ticket_id).round == 1 and e.store.investigation(first.ticket_id, 2) is None

        dispatch = e.router.complete_round(first.ticket_id, 1)
        assert dispatch.binding.round == 2 and e.store.investigation(first.ticket_id, 2) is not None
        assert e.router.complete_round(first.ticket_id, 2) is None  # 只补一次
        assert e.store.investigation(first.ticket_id, 3) is None
    finally:
        e.close()


def test_unknown_event_type_enters_slow_channel(tmp_path):
    """P1-5：从未见过的事件类型走慢通道，不被白名单丢弃；显式配置 event_types 时才按白名单过滤。"""
    e = Env(tmp_path)
    try:
        handled = e.router.handle_event(make_event("EV-NEW", 0, "vendor_alert", None, severity=4))
        assert handled.result == "opened" and handled.channel == "slow"
        assert e.store.investigation(handled.ticket_id, 1) is not None
    finally:
        e.close()
    strict = Env(tmp_path / "strict", config=RouterConfig(event_types=frozenset({"load_spike"})))
    try:
        assert strict.router.handle_event(make_event("EV-NEW", 0, "vendor_alert", None, severity=4)).result == "dropped_whitelist"
    finally:
        strict.close()


def probe_binding(e, ticket_id):
    return RunBinding(run_id=e.router.run_id, knowledge_version=e.router.knowledge_version, ticket_id=ticket_id,
                      round=e.store.probe_of(ticket_id)["proposal"]["round"], state_version=0, trigger_at=T0)


def test_missing_readings_mark_probe_incomplete(tmp_path):
    """P1-2：没有观测层时不伪造 ok 读数，接续调查标“试探未完成”，且这不算读数可疑。"""
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        handled = e.router.handle_event(make_event("EV-NR", 0, "unknown_combo", AIR))
        assert e.store.severity_of(handled.ticket_id)["field_check"] is False
        e.router.step(T0 + timedelta(minutes=3))
        follow = [call for call in source.calls if call.get("reason") == "probe"][0]
        assert follow["probe_incomplete"] is True and follow["readings_after"] == []
        assert [(r["value"], r["quality"]) for r in follow["readings_before"]] == [(None, "unavailable")]
    finally:
        e.close()


def test_done_probe_ticket_is_redispatched_after_fork(tmp_path):
    """P2-1：试探接续完的事件单分叉后照常重派；P2-2：done 只由接续调查写入。"""
    e = Env(tmp_path)
    try:
        replace_source(e, "investigate", SlowSource(experts=("equipment",), probe=PROBE))
        handled = e.router.handle_event(make_event("EV-F", 0, "unknown_combo", AIR))
        with pytest.raises(PlantkgError):
            e.router.mark_probe(probe_binding(e, handled.ticket_id), "done")
        e.router.step(T0 + timedelta(minutes=3))
        assert e.store.probe_of(handled.ticket_id)["state"] == "done"
        e.router.on_fork(run_info("run-1"), run_info("run-2"), T0 + timedelta(minutes=10))
        assert [d.ticket.ticket_id for d in e.router.resume_after_fork()] == [handled.ticket_id]
    finally:
        e.close()


def test_follow_up_investigation_keeps_jev_weights(tmp_path):
    """P2-4：D61 不重新研判，但领域权重延续开单时的 Jev 结果。"""
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        handled = e.router.handle_event(make_event("EV-W", 0, "unknown_combo", AIR))
        e.router.step(T0 + timedelta(minutes=3))
        follow = [call for call in source.calls if call.get("reason") == "probe"][0]
        assert follow["domains"] == e.store.triage(handled.ticket_id, 1)["domains"]
        assert set(follow["domains"]) == {"energy", "elevator", "hvac", "equipment"}
    finally:
        e.close()


def test_safety_with_suspect_reading_notes_field_check_once(tmp_path):
    """P2-7：安全标记 + 可疑读数，工单依据只写一次“请先现场核实”。"""
    suspect = [{"at": T0.isoformat(), "quality": "suspect"}]
    estimator = SeverityEstimator(dict(BASE) | {"safety_event_types": ["elevator_trapped"]}, readings=lambda entity, start, end: suspect)
    e = Env(tmp_path, severity=estimator)
    try:
        reasons = []
        e.registry.register_hook(URGENT_HOOK, lambda event, reason: reasons.append(reason))
        e.router.handle_event(make_event("EV-S", 0, "elevator_trapped", AIR))
        assert reasons[0].count("请先现场核实") == 1
    finally:
        e.close()


def test_rejected_probe_still_starts_follow_up_marked_incomplete(tmp_path):
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        handled = e.router.handle_event(make_event("EV-PR2", 0, "unknown_combo", AIR))
        e.router.mark_probe(probe_binding(e, handled.ticket_id), "rejected", "值班人员拒绝试探")
        e.router.step(T0 + timedelta(minutes=3))
        follow = [call for call in source.calls if call.get("reason") == "probe"]
        assert follow and follow[-1]["probe_incomplete"] is True
        assert "试探未完成" in follow[-1]["probe"]["status"]
    finally:
        e.close()


def test_operator_tools_never_guess_between_open_tickets(tmp_path):
    """模型没传 ticket_id：运行绑定优先；两张打开又没有绑定时报错，不落到最新一张。"""
    e = Env(tmp_path)
    try:
        first = e.router.handle_event(make_event("EV-T1", 0, "unknown_combo", AIR)).ticket_id
        second = e.router.handle_event(make_event("EV-T2", 1, "lighting_fault", "entity://demo-electronics/lighting/LT-P")).ticket_id
        assert first != second
        ask = e.registry.tools["ask_operator"].handler
        assert ask(SimpleNamespace(), "现场有人吗？").business_status != "done"
        assert ask(SimpleNamespace(), "现场有人吗？", first).data["ticket_id"] == first
        assert ask(SimpleNamespace(), "现场有人吗？", "TK-9999").business_status != "done"  # 模型编的单号不写孤儿问题
        e.store.set_ticket_status(second, "resolved")
        before = e.store.channel_of(second)
        assert e.registry.tools["escalate"].handler(SimpleNamespace(), "再升级", second).business_status != "done"
        assert e.store.channel_of(second) == before and e.store.ticket(second).status == "resolved"  # 已结束的单不重开
        e.store.set_ticket_status(second, "open")
        e.registry.register_hook(RUN_BINDING_HOOK, lambda context: (SimpleNamespace(ticket_id=first), None))
        assert ask(SimpleNamespace(), "现场有人吗？", second).data["ticket_id"] == first  # 绑定优先于模型填值
    finally:
        e.close()


def test_slow_channel_tools_ask_escalate_trial(tmp_path):
    e = Env(tmp_path)
    try:
        urgent = []
        e.registry.register_hook(URGENT_HOOK, lambda event, reason: urgent.append(reason))
        handled = e.router.handle_event(make_event("EV-T", 0, "unknown_combo", AIR))
        tools = {name: e.registry.tools[name].handler for name in ("ask_operator", "escalate", "trial")}
        asked = tools["ask_operator"](None, "现场有人吗？", handled.ticket_id)
        question = asked.data["question_id"]
        assert e.store.questions(handled.ticket_id)[0]["status"] == "pending"
        e.router.answer_question(question, "没有")
        assert e.store.questions(handled.ticket_id)[0]["answer"] == "没有"

        escalated = tools["escalate"](None, "根因不明，转工单", handled.ticket_id)
        assert escalated.ok and urgent and e.store.channel_of(handled.ticket_id) == "urgent"

        trial = tools["trial"](None, [], None, handled.ticket_id)
        assert trial.ok is False and trial.error.code == ErrorCode.DEPENDENCY_UNAVAILABLE
        e.router.trial_runner = lambda snapshot, actions, horizon: {"feasible": True, "violations": []}
        e.router.snapshot = lambda event: object()
        assert tools["trial"](None, [], 30).data["result"]["feasible"] is True
    finally:
        e.close()


def test_repo_severity_does_not_treat_pressure_drop_as_stop(tmp_path):
    """卡里要的“传感器说谎”幕：压力类事件不在停产清单，默认配置下不会直接紧急派单。"""
    e = Env(tmp_path, severity=SeverityEstimator())
    try:
        handled = e.router.handle_event(make_event("EV-PD", 0, "compressor_pressure_drop", AIR))
        assert handled.result != "urgent"
        assert e.store.severity_of(handled.ticket_id)["production_stop"] is False
    finally:
        e.close()


def test_suspect_reading_goes_slow_even_when_rule_matches(tmp_path):
    """关键读数有残差报警：即使命中规则也直接进慢通道，不按停产派单。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["compressor_pressure_drop"]}))
    try:
        event = make_event("EV-R", 0, "compressor_pressure_drop", AIR).model_copy(
            update={"payload": {"reading": {"sensor_id": "pressure", "quality": "suspect", "residual_alarm": True}}})
        handled = e.router.handle_event(event)
        assert handled.result == "opened" and handled.channel == "slow"
        assert e.store.channel_of(handled.ticket_id) == "slow"
        assert e.store.investigation(handled.ticket_id, 1) is not None
    finally:
        e.close()


def test_suspect_reading_blocks_urgent_and_notes_field_check(tmp_path):
    """停产清单命中但有可疑读数：不立即派单，工单依据写“请先现场核实”，保护动作交 Agent。"""
    cfg = dict(BASE) | {"stop_event_types": ["compressor_pressure_drop"], "equipment_damage_cny": {"default": 0}}
    suspect = [{"at": T0.isoformat(), "sensor_id": "pressure", "quality": "suspect"}]
    estimator = SeverityEstimator(cfg, readings=lambda entity, start, end: suspect)
    e = Env(tmp_path, severity=estimator)
    try:
        handled = e.router.handle_event(make_event("EV-C", 0, "compressor_pressure_drop", AIR))
        assert handled.result == "opened" and handled.channel == "slow"
        estimate = e.store.severity_of(handled.ticket_id)
        assert estimate["field_check"] is True and estimate["production_stop"] is False and estimate["loss_cny"] == 0
        assert "请先现场核实" in estimate["basis"] and "默认执行" in estimate["basis"]
    finally:
        e.close()

    good = [{"at": T0.isoformat(), "sensor_id": "pressure", "quality": "ok"}]
    estimator = SeverityEstimator(cfg, readings=lambda entity, start, end: good)
    e = Env(tmp_path / "ok", severity=estimator)
    try:
        handled = e.router.handle_event(make_event("EV-G", 0, "compressor_pressure_drop", AIR))
        assert handled.result == "urgent" and handled.channel == "urgent"
    finally:
        e.close()


def recording_hooks(env):
    dispatched, probes = [], []
    env.registry.register_hook(EXPERTS_HOOK, lambda ticket, b, experts: dispatched.append((b.round, tuple(experts))))
    env.registry.register_hook(PROBE_HOOK, lambda ticket, proposal, b: probes.append(proposal))
    return dispatched, probes


def test_live_answer_drives_experts_and_probe(tmp_path):
    """三审 P1-1：真实 LiveAgentResultSource 返回形状下，final_answer 的专家、诊断、依据与试探都生效。"""
    e = Env(tmp_path)
    try:
        gateway = go_live(e, f"调查完成：\n```json\n{answer()}\n```", answer(experts=(), probe=LIVE_PROBE), answer(("equipment",)))
        dispatched, probes = recording_hooks(e)
        first = e.router.handle_event(make_event("EV-L1", 0, "unknown_combo", AIR))
        assert dispatched == [(1, ("hvac",))]
        stored = e.store.investigation(first.ticket_id, 1)
        assert stored["diagnosis"] == "冷却塔堵塞" and stored["evidence_refs"] == ["entity://p1/hvac/CT-1"]
        assert stored["coverage_gap"] is False and stored["source"] == "nexent"

        second = e.router.handle_event(make_event("EV-L2", 1, "unknown_combo", LIGHT))
        assert probes[0].hypothesis == "假设甲：供电侧" and probes[0].actions[0].interval.start == T0 + timedelta(minutes=1)
        assert e.store.ticket(second.ticket_id).status == "waiting" and e.store.investigation(second.ticket_id, 1)["probe"]
        assert "只输出一个 JSON 对象" in gateway.queries[0]
    finally:
        e.close()


@pytest.mark.parametrize("bad", ["我认为是冷却塔堵塞。", '{"diagnosis": "缺专家"}', answer(probe={"hypothesis": "没有动作"}),
                                 ("failed", answer()), PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "模型入口不可用")])
def test_live_failure_degrades_with_coverage_gap(tmp_path, bad):
    """三审 P1-1：解析或运行失败时不拿半截结果派发，明确降级并记覆盖不足。"""
    e = Env(tmp_path)
    try:
        go_live(e, bad)
        dispatched, probes = recording_hooks(e)
        handled = e.router.handle_event(make_event("EV-B", 0, "unknown_combo", AIR))
        stored = e.store.investigation(handled.ticket_id, 1)
        assert stored["coverage_gap"] is True and stored["source"] == "deterministic" and "已降级" in stored["diagnosis"]
        assert len(dispatched) == 1 and not probes and stored["probe"] is None
    finally:
        e.close()


def test_follow_up_query_carries_probe_context_to_gateway(tmp_path):
    """三审 P1-2：基线、试探后读数、假设、结果与不完整标记都在 Gateway 收到的 query 里。"""
    rows = [(T0 - timedelta(minutes=1), 611.111), (T0 + timedelta(minutes=2), 622.222)]
    e = Env(tmp_path, readings=lambda entity, start, end: [{"at": at.isoformat(), "value": value, "quality": "ok"}
                                                           for at, value in rows if start <= at <= end])
    try:
        gateway = go_live(e, answer(experts=(), probe=LIVE_PROBE), answer(("equipment",)))
        e.router.handle_event(make_event("EV-Q", 0, "unknown_combo", AIR))
        e.router.step(T0 + timedelta(minutes=3))
        query = gateway.queries[1]
        for text in ("611.111", "622.222", rows[0][0].isoformat(), rows[1][0].isoformat(), "假设甲：供电侧", "功率下降",
                     '"result": "executing"', '"probe_incomplete": false'):
            assert text in query
    finally:
        e.close()

    many = [{"at": (T0 + timedelta(seconds=i)).isoformat(), "value": i, "quality": "ok", "sensor_id": "pressure"} for i in range(1, 181)]
    e = Env(tmp_path / "many", readings=lambda entity, start, end: many)
    try:
        gateway = go_live(e, answer(experts=(), probe=LIVE_PROBE), answer(("equipment",)))
        handled = e.router.handle_event(make_event("EV-M", 0, "unknown_combo", AIR))
        e.router.mark_probe(probe_binding(e, handled.ticket_id), "rejected", "值班人员拒绝试探")
        e.router.step(T0 + timedelta(minutes=1))  # 失败后不等窗口结束
        query = gateway.queries[1]
        assert len(query) <= 8000 and '"probe_incomplete": true' in query and "试探未完成" in query and "值班人员拒绝试探" in query
        assert many[-1]["at"] in query and many[0]["at"] not in query  # 放不下时留最新读数
        assert e.store.investigation(handled.ticket_id, 2)["coverage_gap"] is False
    finally:
        e.close()


def test_residual_and_follow_up_probes_go_to_probe_handler(tmp_path):
    """三审 P1-3：残差调查与接续调查给出试探时都进待试探，不派专家，并记慢通道。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        replace_source(e, "investigate", SlowSource(probe=PROBE, follow_probe=PROBE))
        dispatched, probes = recording_hooks(e)
        first = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2))
        e.router.complete_round(first.ticket_id, 1)
        assert dispatched == [(1, ("energy",))] and e.store.channel_of(first.ticket_id) == "fast"

        residual = make_event("EV-2", 2, "lighting_fault", LIGHT, severity=2).model_copy(update={"payload": {"residual_alarm": True}})
        e.router.handle_event(residual)
        assert [p.round for p in probes] == [2] and e.store.ticket(first.ticket_id).status == "waiting"
        assert e.store.channel_of(first.ticket_id) == "slow" and len(dispatched) == 1

        e.router.step(T0 + timedelta(minutes=5))  # 接续调查又提试探：继续等，不派专家，新试探不被置 done
        assert [p.round for p in probes] == [2, 3] and len(dispatched) == 1
        assert e.store.ticket(first.ticket_id).status == "waiting" and e.store.probe_of(first.ticket_id)["state"] == "executing"
    finally:
        e.close()


def test_events_during_probe_only_accumulate(tmp_path):
    """三审 P1-4：试探期间的直通事件不另开专家轮次，窗口结束后统一接续，轮次一致。"""
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE)
        replace_source(e, "investigate", source)
        dispatched, _ = recording_hooks(e)
        handled = e.router.handle_event(make_event("EV-A", 0, "unknown_combo", AIR))
        e.router.handle_event(make_event("EV-B", 1, "unknown_combo", AIR, severity=5))
        alarm = make_event("EV-C", 2, "unknown_combo", AIR).model_copy(update={"payload": {"residual_alarm": True}})
        e.router.handle_event(alarm)
        assert dispatched == [] and e.store.ticket(handled.ticket_id).status == "waiting"
        assert e.store.investigation(handled.ticket_id, 2) is None

        e.router.step(T0 + timedelta(minutes=3))
        assert dispatched == [(2, ("equipment",))]
        assert e.store.ticket(handled.ticket_id).round == 2 and e.store.investigation(handled.ticket_id, 2)["experts"] == ["equipment"]
        follow = [c for c in source.calls if c.get("reason") == "probe"]
        assert len(follow) == 1 and [x["event_id"] for x in follow[0]["events_during_probe"]] == ["EV-B", "EV-C"]
        assert follow[0]["events_during_probe"][1]["residual_alarm"] is True

        e.router.mark_probe(probe_binding(e, handled.ticket_id), "expired")  # 接续后迟到的回写不再触发一轮
        e.router.step(T0 + timedelta(minutes=4))
        assert len(dispatched) == 1
    finally:
        e.close()


def test_late_probe_result_leaves_next_probe_running(tmp_path):
    """四审 P1：第 2 次试探进行中收到第 1 次的失败回写，只入历史；本轮回写仍正常触发接续。"""
    e = Env(tmp_path)
    try:
        source = SlowSource(experts=("equipment",), probe=PROBE, follow_probe=PROBE)
        replace_source(e, "investigate", source)
        handled = e.router.handle_event(make_event("EV-LP", 0, "unknown_combo", AIR))
        first = probe_binding(e, handled.ticket_id)
        e.router.step(T0 + timedelta(minutes=3))
        second = e.store.probe_of(handled.ticket_id)
        assert second["state"] == "executing" and second["proposal"]["round"] > first.round
        follows = lambda: [c for c in source.calls if c.get("reason") == "probe"]  # noqa: E731

        e.router.mark_probe(first, "expired", "第 1 次审批超时")
        assert e.store.probe_of(handled.ticket_id) == second
        e.router.step(T0 + timedelta(minutes=4))  # 窗口没到，不额外接续
        assert len(follows()) == 1

        e.router.mark_probe(probe_binding(e, handled.ticket_id), "rejected", "值班人员拒绝")
        e.router.step(T0 + timedelta(minutes=5))
        assert len(follows()) == 2 and follows()[-1]["probe_incomplete"] is True
    finally:
        e.close()


def test_late_and_repeated_completion_change_nothing(tmp_path):
    """三审 P1-5：旧轮迟到与重复完成不改状态、不调钩子；派发序列 [1, 2]，就绪钩子 [1]。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        dispatched, _ = recording_hooks(e)
        ready = []
        e.registry.register_hook(PROPOSALS_READY_HOOK, lambda ticket_id, round_: ready.append(round_))
        ticket_id = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2)).ticket_id
        e.router.handle_event(make_event("EV-2", 2, "lighting_fault", LIGHT, severity=2))
        assert e.router.complete_round(ticket_id, 1).binding.round == 2

        assert e.router.complete_round(ticket_id, 1) is None  # 第 2 轮在跑时第 1 轮再次回调
        assert [r for r, _ in dispatched] == [1, 2] and ready == [1]
        assert e.store.is_running(ticket_id) and e.store.ticket(ticket_id).round == 2
        assert e.store.ticket_state(ticket_id, "late_round_1") == "stale_round"

        assert e.router.complete_round(ticket_id, 2) is None
        assert e.router.complete_round(ticket_id, 2) is None  # 重复完成
        assert ready == [1, 2] and e.store.ticket_state(ticket_id, "late_round_2") == "duplicate"
    finally:
        e.close()


def test_parent_run_callback_after_fork_is_history_only(tmp_path):
    """三审 P1-5：分叉后父运行的回调，子运行重派前后都不生效。"""
    e = Env(tmp_path, rule_matcher=EventTypeRuleMatcher({"rule_covered_events": ["lighting_fault"]}))
    try:
        dispatched, _ = recording_hooks(e)
        ready = []
        e.registry.register_hook(PROPOSALS_READY_HOOK, lambda ticket_id, round_: ready.append(round_))
        ticket_id = e.router.handle_event(make_event("EV-1", 0, "lighting_fault", LIGHT, severity=2)).ticket_id
        e.router.on_fork(run_info("run-1"), run_info("run-2"), T0 + timedelta(minutes=5))

        assert e.router.complete_round(ticket_id, 1) is None  # 还没重派
        assert e.store.ticket_state(ticket_id, "late_round_1") == "other_run" and ready == []

        e.router.resume_after_fork(knowledge_version=4)
        assert e.router.complete_round(ticket_id, 1) is None  # 重派之后
        assert e.store.is_running(ticket_id) and ready == []
        assert e.router.complete_round(ticket_id, 2) is None and ready == [2]  # 子运行的结果照常
    finally:
        e.close()


def test_repeated_answer_uses_last_object():
    """模型重复作答或带代码块时取最后一个 JSON 对象；没有对象按不符合契约处理。"""
    first, last = ('{"diagnosis": "%s", "experts": ["energy"], "evidence_refs": [], "probe": null}' % d for d in ("先", "后"))
    wrapped = "\n".join((first, "```json", last, "```"))
    parsed = parse_investigation({"status": "finished", "final_answer": wrapped})
    assert parsed["diagnosis"] == "后"
    trailing = parse_investigation({"status": "finished", "final_answer": wrapped + ' 依据：{"ref": "graph://TX1"}'})
    assert trailing["diagnosis"] == "后"
    with pytest.raises(ValueError):
        parse_investigation({"status": "finished", "final_answer": "## 结论：立即救援"})
