from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from plantkg.agent.arbitration.service import ArbitrationService
from plantkg.agent.consultation.consilium import Consultation
from plantkg.agent.router import RouterStore, SqliteProposalSink
from plantkg.agent.router.standin import SyntheticConsilium
from plantkg.core.contracts import (
    DayEvent,
    ErrorCode,
    ExpertProposal,
    ExpertReply,
    Interval,
    DecisionConversion,
    PlantkgError,
    PolicyConfig,
    ProposalCost,
    ProposedAction,
    ResponseBudget,
    RunBinding,
    Ticket,
    TrialResult,
    TrialViolation,
)
from plantkg.core.ports import AgentResultSource
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


AT = datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc)


class Registry:
    def __init__(self, consultation, source=None):
        self.consultation = consultation
        self.source = source
        self.tools = {}

    def hooks_for(self, name):
        return [self.consultation] if name == "consultation.service" else []

    def resolve_mode(self, port, mode, key):
        assert port is AgentResultSource and key == "arbitration"
        return self.source


def proposal(*, safety_tags=(), readiness="ready"):
    action = ProposedAction(entity_id="entity://plant/zone/admin", command="set_mode", params={"mode": "precool"},
                            interval=Interval(start=AT, end=AT.replace(minute=20)), readiness=readiness,
                            depends_on=("root_cause",) if readiness == "needs_diagnosis" else ())
    return ExpertProposal(proposal_id="proposal-1", expert="hvac", ticket_id="TK-001", actions=(action,),
                          cost=ProposalCost(cny=0), confidence=0.9, diagnosis="cooling failure",
                          safety_tags=safety_tags, binding=RunBinding(run_id="run-1", knowledge_version=1,
                                                                      ticket_id="TK-001", round=1, state_version=0,
                                                                      trigger_at=AT))


def setup(proposal_item, *, source=None, reply_status="done"):
    ticket = Ticket(ticket_id="TK-001", opened_at=AT, status="open", round=1, event_ids=())
    ticket_state = {}
    items = proposal_item if isinstance(proposal_item, tuple) else (proposal_item,)
    for item in items:
        if item.proposal_id and item.binding:
            key = f"reply_status_{item.binding.round}_{item.proposal_id}"
            ticket_state[key] = reply_status
            if reply_status == "done" and item.diagnosis:
                ticket_state[f"reply_diagnosis_{item.binding.round}_{item.proposal_id}"] = item.diagnosis
    store = SimpleNamespace(ticket=lambda _: ticket, event=lambda _: None, round_trigger=lambda *_: AT,
                            set_ticket_status=lambda *_: None,
                            set_ticket_state=lambda _, key, value: ticket_state.__setitem__(key, value),
                            ticket_state=lambda _, key: ticket_state.get(key))
    router = SimpleNamespace(store=store, sink=SimpleNamespace(list=lambda *_: list(items),
                                                               replied=lambda _: True,
                                                               reply_status=lambda _: reply_status),
                             trial_runner=lambda *args: SimpleNamespace(feasible=True, soft_cost={"cny": 1}),
                             policy=PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                                                 priority_profiles={"default": ("safety", "hvac")},
                                                 response_budget=ResponseBudget(layer12_min=1, layer3_min=3)),
                             mode="evaluation", _wall=lambda: AT + timedelta(minutes=2))
    router.registry = Registry(None, source)
    executed = []
    decisions = []
    work_orders = {}
    runner = SimpleNamespace(dispatcher=SimpleNamespace(deliver=lambda p, state: executed.append(p)),
                             record_decision=decisions.append, tickets=SimpleNamespace(tickets={}, work_orders=work_orders),
                             now=AT, next_id=lambda prefix: f"{prefix}-0001",
                             snapshot=lambda: object(), trial_runner=lambda *args: SimpleNamespace(feasible=True, soft_cost={"cny": 1}),
                             save_work_order=lambda order: work_orders.update({order.work_order_id: order}),
                             trace=SimpleNamespace(proposals=[]),
                             record_proposal=lambda item: runner.trace.proposals.append(item))
    consultation = SimpleNamespace(find=lambda *_: (router, runner), held_status={})
    router.registry.consultation = consultation
    router.escalate = lambda ticket_id, reason, *, denied_actions=(): executed.append((ticket_id, reason, denied_actions))
    return router, runner, executed, decisions, router.registry


def test_safety_tag_escalates_before_any_simulation_action() -> None:
    item = proposal(safety_tags=("safety",))
    router, runner, executed, decisions, registry = setup(item)
    service = ArbitrationService(registry)

    result = service.on_proposals_ready("TK-001", 1)

    assert result is not None
    assert executed == [("TK-001", "action_safety", item.actions)]
    assert result.result.outcome == "work_order"
    assert decisions == [result]
    order = next(iter(runner.tickets.work_orders.values()))
    assert order.status == "proposed" and order.actions == item.actions


def test_safety_veto_is_limited_to_marked_proposal_actions() -> None:
    unsafe = proposal(safety_tags=("safety",))
    safe = proposal().model_copy(update={"proposal_id": "proposal-2", "expert": "energy"})
    router, runner, executed, _, registry = setup((unsafe, safe))
    service = ArbitrationService(registry)
    service.arbitrate = lambda proposals, **_: ((safe,), DecisionConversion(policy_version="p1", scores={"proposal-2": 2.0}))
    service._deliver = lambda runner, item, state: executed.append(item)

    result = service.on_proposals_ready("TK-001", 1)

    assert result.result.selected_proposal_ids == ("proposal-2",)
    assert len(executed) == 2
    assert executed[-1].proposal_id == "proposal-2"
    order = next(iter(runner.tickets.work_orders.values()))
    assert order.actions == unsafe.actions


def test_safety_veto_actions_reach_registered_urgent_handler() -> None:
    item = proposal(safety_tags=("safety",))
    router, runner, _, _, registry = setup(item)
    handled = []
    hooks_for = router.registry.hooks_for
    urgent_handler = lambda event, reason: handled.append((event, reason))
    router.registry.hooks_for = lambda name: [urgent_handler] if name == "router.urgent" else hooks_for(name)
    router.escalate = lambda ticket_id, reason, *, denied_actions=(): [
        handler(SimpleNamespace(payload={"denied_actions": [a.model_dump(mode="json") for a in denied_actions]}), reason)
        for handler in router.registry.hooks_for("router.urgent")]
    service = ArbitrationService(registry)

    service.on_proposals_ready("TK-001", 1)

    assert handled[0][0].payload["denied_actions"] == [action.model_dump(mode="json") for action in item.actions]


def test_waiting_expert_reply_cannot_unlock_diagnosis_action() -> None:
    item = proposal(readiness="needs_diagnosis")
    router, _, executed, _, registry = setup(item, reply_status="waiting")
    router.mode = "live"
    source = SimpleNamespace(get=lambda *args: {"selected_proposal_id": "proposal-1"})
    router.registry.source = source
    service = ArbitrationService(registry)
    service._deliver = lambda runner, item, state: executed.append(item)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result is not None
    assert result.result.outcome == "blocked"
    assert executed == [("TK-001", "diagnosis_unresolved", item.actions)]


def test_diagnosis_dependent_action_is_sent_to_execution_after_layer_three() -> None:
    item = proposal(readiness="needs_diagnosis")
    source = SimpleNamespace(get=lambda kind, binding, payload: {"selected_proposal_id": "proposal-1", "summary": "根因确认"})
    router, runner, executed, decisions, registry = setup(item, source=source)
    router.mode = "live"
    service = ArbitrationService(registry)
    service._deliver = lambda runner, item, state: executed.append(item)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result is not None
    assert len(executed) == 1
    assert executed[0].actions[0].readiness == "ready"
    assert result.result.selected_proposal_ids == ("proposal-1",)
    assert decisions == [result]


def test_synthetic_consilium_reply_unlocks_diagnosis_with_sqlite_sink(tmp_path) -> None:
    item = proposal(readiness="needs_diagnosis")
    router, runner, executed, _, registry = setup(item)
    writer = SQLiteWriter(tmp_path / "replies.db")
    MigrationRunner(writer).apply()
    try:
        router.store = RouterStore(writer, "plant")
        router.sink = SqliteProposalSink(writer, "plant")
        event = DayEvent(event_id="EV-1", at=AT, event_type="load_spike", source="script",
                         severity=3, summary="synthetic diagnosis", nature="synthetic")
        ticket = Ticket(ticket_id=item.ticket_id, opened_at=AT, status="open", round=1,
                        experts=(item.expert,), event_ids=(event.event_id,))
        router.store.save_ticket(ticket, run_id=item.binding.run_id)
        router.store.record_event(event, "opened", ticket.ticket_id)
        router.complete_round = lambda *_: None
        router._wall = lambda: AT
        standin = SyntheticConsilium({item.expert: lambda *_: item.model_copy(update={"proposal_id": None, "binding": None})})
        standin(router, ticket, item.binding)
        stored = router.sink.list(ticket.ticket_id, 1)[0]
        registry.source = SimpleNamespace(get=lambda *_: {"selected_proposal_id": stored.proposal_id})
        service = ArbitrationService(registry)
        service._deliver = lambda runner, selected, state: executed.append(selected)

        result = service.on_replies_ready(ticket.ticket_id, 1)

        assert result.result.outcome == "executed"
        assert executed[0].actions[0].readiness == "ready"
        assert router.sink.replied(stored.proposal_id)
        assert router.store.ticket_state(ticket.ticket_id, f"reply_status_1_{stored.proposal_id}") == "done"
        assert router.store.ticket_state(ticket.ticket_id, f"reply_diagnosis_1_{stored.proposal_id}") == item.diagnosis
    finally:
        writer.close()


def test_agent_cannot_select_diagnosis_proposal_with_waiting_reply() -> None:
    waiting = proposal(readiness="needs_diagnosis")
    done = proposal(readiness="needs_diagnosis").model_copy(update={"proposal_id": "proposal-2", "expert": "energy"})
    source = SimpleNamespace(get=lambda *_: {"selected_proposal_id": waiting.proposal_id})
    router, _, executed, _, registry = setup((waiting, done), source=source)
    router.mode = "live"
    for item, status in ((waiting, "waiting"), (done, "done")):
        key = f"{item.binding.round}_{item.proposal_id}"
        router.store.set_ticket_state(item.ticket_id, f"reply_status_{key}", status)
        router.store.set_ticket_state(item.ticket_id, f"reply_diagnosis_{key}", item.diagnosis)
    service = ArbitrationService(registry)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result.result.outcome == "blocked"
    assert result.result.selected_proposal_ids == ()
    assert executed == []


def test_done_reply_without_its_own_diagnosis_does_not_unlock_action() -> None:
    item = proposal(readiness="needs_diagnosis")
    source = SimpleNamespace(get=lambda *_: {"selected_proposal_id": item.proposal_id})
    router, _, executed, _, registry = setup(item, source=source)
    router.store.set_ticket_state(item.ticket_id, "reply_status_1_proposal-1", "done")
    router.store.set_ticket_state(item.ticket_id, "reply_diagnosis_1_proposal-1", None)
    router.mode = "live"
    service = ArbitrationService(registry)
    service._deliver = lambda runner, selected, state: executed.append(selected)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result.result.outcome == "blocked"
    assert result.result.selected_proposal_ids == ()
    assert executed == [("TK-001", "diagnosis_unresolved", item.actions)]


def test_router_step_cannot_arbitrate_between_reply_and_diagnosis_persistence() -> None:
    item = proposal(readiness="needs_diagnosis")
    source = SimpleNamespace(get=lambda *_: {"selected_proposal_id": item.proposal_id})
    router, _, executed, _, registry = setup(item, source=source)
    router.mode = "live"
    router.run_id = item.binding.run_id
    router.knowledge_version = item.binding.knowledge_version
    router._wall = lambda: AT
    service = ArbitrationService(registry)
    service._deliver = lambda runner, selected, state: executed.append(selected)
    service._pending_layer3.add((item.ticket_id, item.binding.round))
    router.store.set_ticket_state(item.ticket_id, "reply_status_1_proposal-1", None)
    router.store.set_ticket_state(item.ticket_id, "reply_diagnosis_1_proposal-1", None)
    replied = False
    observed = []

    def attach_reply(_proposal_id, _reply):
        nonlocal replied
        replied = True
        service.step(item.binding.run_id, AT)
        observed.append(service.wait_for_decision(item.ticket_id, item.binding.round, timeout=1))

    router.sink.replied = lambda _proposal_id: replied
    router.sink.get = lambda _proposal_id: item
    router.sink.attach_reply = attach_reply
    consultation = Consultation(registry)

    consultation._attach_reply(router, item.binding,
                               ExpertReply(status="done", proposal_id=item.proposal_id, diagnosis="root cause"),
                               [item.proposal_id], item.expert)
    service.step(item.binding.run_id, AT)
    result = service.wait_for_decision(item.ticket_id, item.binding.round, timeout=1)

    assert observed[0] is not None and observed[0].result.outcome == "executed"
    assert result is observed[0]
    assert executed[0].actions[0].readiness == "ready"


def test_live_unanswered_diagnosis_does_not_release_dependent_action() -> None:
    item = proposal(readiness="needs_diagnosis")
    source = SimpleNamespace(get=lambda *args: {"selected_proposal_id": "proposal-1"})
    router, runner, executed, decisions, registry = setup(item, source=source)
    router.mode = "live"
    router.sink.replied = lambda _: False
    service = ArbitrationService(registry)
    router.mode = "live"

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result is not None
    assert executed == [("TK-001", "diagnosis_unresolved", item.actions)]
    assert result.result.outcome == "blocked"
    assert decisions == [result]


def test_proposals_ready_hook_returns_while_handler_is_still_running() -> None:
    router, _, _, _, registry = setup(proposal())
    router.mode = "live"
    service = ArbitrationService(registry)
    started, release = threading.Event(), threading.Event()

    def slow_handler(ticket_id, round_):
        started.set()
        release.wait(1)

    service._process_proposals_ready = slow_handler
    try:
        assert service.on_proposals_ready("TK-001", 1) is None
        assert started.wait(1)
        assert not service._jobs[("TK-001", 1, 2)].done()
    finally:
        release.set()
    assert service.wait_for_decision("TK-001", 1, timeout=1) is None

    service = ArbitrationService(registry)
    started.clear()
    release.clear()
    service._process_replies_ready = slow_handler
    try:
        assert service.on_replies_ready("TK-001", 1) is None
        assert started.wait(1)
        assert not service._jobs[("TK-001", 1, 3)].done()
    finally:
        release.set()
    assert service.wait_for_decision("TK-001", 1, timeout=1) is None


def _layer3_payload(registry, event, item, *, policy_profiles=None):
    """经默认装配的 B28 第 3 层跑一轮，抓取发给主 Agent 的裁决 payload。"""
    from plantkg.agent.arbitration.service import ARBITRATION_KEY
    from plantkg.agent.router.router import REPLIES_READY_HOOK
    from plantkg.core.ports import AgentResultSource

    service = registry.hooks_for(REPLIES_READY_HOOK)[-1].__self__
    captured: dict = {}

    class CapturingSource:
        def get(self, kind, binding, payload):
            captured.update(payload)
            return {"selected_proposal_id": None}

    registry.register_mode(AgentResultSource, "evaluation", CapturingSource(), key=ARBITRATION_KEY)
    ticket = Ticket(ticket_id=item.ticket_id, opened_at=event.at, status="open", round=1, event_ids=(event.event_id,))
    states = {f"reply_status_1_{item.proposal_id}": "done",
              f"reply_diagnosis_1_{item.proposal_id}": item.diagnosis}
    store = SimpleNamespace(ticket=lambda _: ticket, event=lambda _: {"event": event},
                            round_trigger=lambda *_: event.at, set_ticket_status=lambda *_: None,
                            ticket_state=lambda _, key: states.get(key))
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1.0},
                          priority_profiles=policy_profiles or {"default": ("safety", "production", "comfort", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    router = SimpleNamespace(store=store, sink=SimpleNamespace(list=lambda *_: [item], replied=lambda _: True),
                             policy=policy, mode="evaluation", registry=registry, run_id=item.binding.run_id,
                             knowledge_version=1, _wall=lambda: event.at)
    runner = SimpleNamespace(world=SimpleNamespace(index=SimpleNamespace(zone_of=lambda _: "admin")),
                             tickets=SimpleNamespace(tickets={}, work_orders={}), dispatcher=SimpleNamespace(),
                             record_decision=lambda decision: None, now=event.at, save_work_order=lambda order: None,
                             run_info=SimpleNamespace(run_id=item.binding.run_id),
                             trace=SimpleNamespace(proposals=[]))
    registry.register_hook("consultation.service",
                           SimpleNamespace(find=lambda *_: (router, runner), held_status={}))
    service.on_replies_ready(item.ticket_id, 1)
    return captured


def test_layer3_priority_and_knowledge_version_come_from_graph():
    """B28 第 3 层先问图谱钩子：priority 与图谱知识版本都进 payload。"""
    from plantkg.world.scenario.runner import default_registry, pack_registry

    registry = default_registry(plugins=pack_registry())
    event = DayEvent(event_id="EV-PRI", at=AT, event_type="load_spike", source="script", severity=3,
                     summary="行政楼早高峰负荷", entity_id="entity://demo-electronics/ahu/AHU-A01", nature="synthetic")
    captured = _layer3_payload(registry, event, proposal(readiness="needs_diagnosis"))

    hook = registry.hooks_for("router.priority")[-1]
    expected = hook("admin", AT)
    assert captured["priority"] == expected["order"]
    assert captured["priority"][:2] == ["safety", "comfort"]  # 08:05 当地 = 行政楼高峰
    assert captured["knowledge_version"] == expected["knowledge_version"]
    assert captured["knowledge_version"].startswith("v1@")


def test_explicit_policy_profiles_override_graph_priority():
    """PolicyConfig 显式配了非 default 的 profiles 才覆盖图谱。"""
    from plantkg.world.scenario.runner import default_registry, pack_registry

    registry = default_registry(plugins=pack_registry())
    event = DayEvent(event_id="EV-PRI", at=AT, event_type="load_spike", source="script", severity=3,
                     summary="行政楼早高峰负荷", entity_id="entity://demo-electronics/ahu/AHU-A01", nature="synthetic")
    captured = _layer3_payload(registry, event, proposal(readiness="needs_diagnosis"),
                               policy_profiles={"default": ("safety", "production", "comfort", "cost"),
                                                "load_spike": ("safety", "energy", "hvac")})

    assert captured["priority"] == ["safety", "energy", "hvac"]
    assert captured["knowledge_version"] == 1  # 显式覆盖时用运行的 knowledge 版本


def test_priority_falls_back_to_default_when_graph_is_missing():
    """图谱缺失（没有 router.priority 钩子）回退 PolicyConfig 的 default。"""
    from plantkg.world.scenario.runner import default_registry

    registry = default_registry(packs=("consultation", "arbitration"))
    assert registry.hooks_for("router.priority") == ()
    event = DayEvent(event_id="EV-PRI", at=AT, event_type="load_spike", source="script", severity=3,
                     summary="行政楼早高峰负荷", entity_id="entity://demo-electronics/ahu/AHU-A01", nature="synthetic")
    captured = _layer3_payload(registry, event, proposal(readiness="needs_diagnosis"))

    assert captured["priority"] == ["safety", "production", "comfort", "cost"]
    assert captured["knowledge_version"] == 1


def test_arbitration_records_existing_violations_on_baseline() -> None:
    """#173 相对语义：试算基线上有既有越界时，裁决记录“环境既有越界”，不留未解释的缺口。"""
    first = proposal().model_copy(update={"proposal_id": "proposal-a", "expert": "hvac"})
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                          priority_profiles={"default": ("safety", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    existing = TrialViolation(kind="hard_zone", entity_id="entity://demo-electronics/zone/cleanroom", at=AT,
                              detail="cleanroom 温度 30.3 ℃ 超出 [21.0, 23.0]")

    def trial(snapshot, actions, horizon):
        return TrialResult(feasible=True, soft_cost={"cny": 1.0}, baseline_violations=(existing,))

    selected, conversion = ArbitrationService(None).arbitrate([first], policy=policy, snapshot=object(), trial_runner=trial)
    assert selected and conversion.notes and "环境既有越界" in conversion.notes[0]
    assert conversion.infeasible == ()


def test_arbitration_prefers_candidate_that_leaves_fewer_existing_violations() -> None:
    """#201-5：先比动作后剩余越界分钟、再比代价；能减少既有越界的候选优先，notes 写出分钟数变化。"""
    action = proposal().actions[0]
    cheaper = proposal().model_copy(update={"proposal_id": "proposal-a", "expert": "hvac",
                                            "actions": (action.model_copy(update={"command": "hold"}),)})
    reducing = proposal().model_copy(update={"proposal_id": "proposal-b", "expert": "energy",
                                             "actions": (action.model_copy(update={"command": "precool"}),)})
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                          priority_profiles={"default": ("safety", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    cleanroom = "entity://demo-electronics/zone/cleanroom"
    existing = tuple(TrialViolation(kind="hard_zone", entity_id=cleanroom, at=AT + timedelta(minutes=index),
                                    detail=f"cleanroom 温度 {24.0 + index:.1f} ℃ 超出 [21.0, 23.0]")
                     for index in range(3))

    def trial(snapshot, actions, horizon):
        if actions[0].command == "precool":  # 代价更高，但动作后既有越界分钟为 0
            return TrialResult(feasible=True, soft_cost={"cny": 9.0}, baseline_violations=existing,
                               remaining_violation_minutes=0)
        return TrialResult(feasible=True, soft_cost={"cny": 1.0}, baseline_violations=existing,
                           remaining_violation_minutes=2)

    selected, conversion = ArbitrationService(None).arbitrate([cheaper, reducing], policy=policy,
                                                              snapshot=object(), trial_runner=trial)
    assert [p.proposal_id for p in selected] == ["proposal-b"]
    assert conversion.notes and "优先减少越界" in conversion.notes[0]
    assert "cleanroom越界 3 分钟 → 0 分钟" in conversion.notes[0]


def test_arbitration_uses_unified_window_across_candidates() -> None:
    """#240 复审 P1：组合时长不同也按同一比较窗口试算，短窗口不能靠少算既有越界分钟占便宜。"""
    action = proposal().actions[0]
    short = proposal().model_copy(update={"proposal_id": "proposal-a", "expert": "hvac", "actions": (
        action.model_copy(update={"command": "hold", "interval": Interval(start=AT, end=AT + timedelta(minutes=5))}),)})
    long = proposal().model_copy(update={"proposal_id": "proposal-b", "expert": "energy", "actions": (
        action.model_copy(update={"command": "precool", "interval": Interval(start=AT, end=AT + timedelta(minutes=30))}),)})
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                          priority_profiles={"default": ("safety", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    cleanroom = "entity://demo-electronics/zone/cleanroom"
    existing = tuple(TrialViolation(kind="hard_zone", entity_id=cleanroom, at=AT + timedelta(minutes=index),
                                    detail=f"cleanroom 温度 {24.0 + index:.1f} ℃ 超出 [21.0, 23.0]")
                     for index in range(3))
    windows: list[tuple[datetime, datetime]] = []

    def trial(snapshot, actions, horizon):
        windows.append((horizon.start, horizon.end))
        if actions[0].command == "precool":  # 长动作代价更高，但在同一窗口里能减少越界
            return TrialResult(feasible=True, soft_cost={"cny": 9.0}, baseline_violations=existing,
                               remaining_violation_minutes=0)
        return TrialResult(feasible=True, soft_cost={"cny": 1.0}, baseline_violations=existing,
                           remaining_violation_minutes=3)

    selected, conversion = ArbitrationService(None).arbitrate([short, long], policy=policy,
                                                              snapshot=object(), trial_runner=trial)
    assert [p.proposal_id for p in selected] == ["proposal-b"]
    assert conversion.notes and "cleanroom越界 3 分钟 → 0 分钟" in conversion.notes[0]
    # 有既有越界时全部组合都会在统一窗口上重算；短候选也拿到覆盖最长动作的窗口
    assert (AT, AT + timedelta(minutes=30)) in windows


def test_arbitration_score_and_winner_do_not_depend_on_proposal_order() -> None:
    first = proposal().model_copy(update={"proposal_id": "proposal-a", "expert": "hvac"})
    second = proposal().model_copy(update={"proposal_id": "proposal-b", "expert": "energy"})
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                          priority_profiles={"default": ("safety", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    def trial(snapshot, actions, horizon):
        score = 10.0 if len(actions) == 1 else 15.0
        return TrialResult(feasible=True, soft_cost={"cny": score})

    service = ArbitrationService(None)
    winner_a, score_a = service.arbitrate([first, second], policy=policy, snapshot=object(), trial_runner=trial)
    winner_b, score_b = service.arbitrate([second, first], policy=policy, snapshot=object(), trial_runner=trial)

    assert tuple(p.proposal_id for p in winner_a) == tuple(p.proposal_id for p in winner_b)
    assert score_a.scores == score_b.scores


def test_layer_three_live_query_and_final_answer_are_parsed() -> None:
    hvac = proposal(readiness="needs_diagnosis")
    energy = proposal().model_copy(update={"proposal_id": "proposal-2", "expert": "energy",
                                          "diagnosis": "电价低谷未到，不需要削峰"})
    seen: dict = {}

    def get(kind, binding, payload):
        # 复现 #100：live 来源要求 payload["query"]，缺了直接抛 INVALID_REQUEST
        if not payload.get("query"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"{kind.value} 需要 payload.query")
        seen.update(payload)
        return {"status": "finished", "agent_run_id": "ar-arb", "terminal_reason": None,
                "final_answer": json.dumps({"selected_proposal_id": "proposal-1", "summary": "暖通诊断成立，按预冷执行",
                                            "question": None}, ensure_ascii=False)}

    router, _, executed, _, registry = setup((hvac, energy), source=SimpleNamespace(get=get))
    router.mode = "live"
    service = ArbitrationService(registry)
    service._deliver = lambda runner, item, state: executed.append(item)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert seen["query"] and "proposal-1" in seen["query"] and "proposal-2" in seen["query"]
    assert "证据缺口" in seen["query"] and "优先级配置" in seen["query"]
    assert seen["as_of"] == AT.isoformat()
    assert result is not None
    assert result.result.selected_proposal_ids == ("proposal-1",)
    assert result.result.summary == "暖通诊断成立，按预冷执行"
    assert executed[0].actions[0].readiness == "ready"
    assert all("降级" not in note and "不可用" not in note for note in result.conversion.notes)


def test_layer_three_live_answer_without_json_degrades_to_layer_two() -> None:
    item = proposal(readiness="needs_diagnosis")
    source = SimpleNamespace(get=lambda *_: {"status": "finished", "agent_run_id": "ar-arb", "terminal_reason": None,
                                             "final_answer": "我认为应该按暖通诊断执行"})
    router, _, executed, decisions, registry = setup(item, source=source)
    router.mode = "live"
    service = ArbitrationService(registry)
    service._deliver = lambda runner, selected, state: executed.append(selected)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result.result.outcome == "blocked"
    assert result.result.selected_proposal_ids == ()
    assert executed == [] and decisions == [result]
    assert any("回退第 2 层结果" in note for note in result.conversion.notes)


def test_layer_three_live_answer_with_unknown_proposal_id_degrades() -> None:
    item = proposal(readiness="needs_diagnosis")
    answer = json.dumps({"selected_proposal_id": "proposal-9", "summary": "选中不存在的提案", "question": None})
    source = SimpleNamespace(get=lambda *_: {"status": "finished", "agent_run_id": "ar-arb", "terminal_reason": None,
                                             "final_answer": answer})
    router, _, executed, _, registry = setup(item, source=source)
    router.mode = "live"
    service = ArbitrationService(registry)
    service._deliver = lambda runner, selected, state: executed.append(selected)

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result.result.outcome == "blocked" and result.result.selected_proposal_ids == ()
    assert executed == []
    assert any("不在本轮候选里" in note for note in result.conversion.notes)


def test_layer_three_decision_at_uses_round_trigger_not_wall_clock() -> None:
    item = proposal(readiness="needs_diagnosis")
    answer = json.dumps({"selected_proposal_id": item.proposal_id, "summary": "暖通诊断成立", "question": None})
    source = SimpleNamespace(get=lambda *_: {"status": "finished", "agent_run_id": "ar-arb", "terminal_reason": None,
                                             "final_answer": answer})
    router, _, _, _, registry = setup(item, source=source)
    router.mode = "live"
    wall = AT + timedelta(minutes=5)
    router._wall = lambda: wall
    service = ArbitrationService(registry)
    service._deliver = lambda runner, selected, state: None

    assert service.on_replies_ready("TK-001", 1) is None
    result = service.wait_for_decision("TK-001", 1, timeout=1)

    assert result.at == router.store.round_trigger("TK-001", 1)  # 第 3 层按仿真触发时刻落库，导出按 at 分日
    assert result.at != wall  # 不再取实际决策时刻（墙钟）


def test_arbitration_unified_window_when_gap_hides_existing_violation() -> None:
    """#258：候选窗口不一致时无条件按统一窗口复算；自身窗口看不到既有越界也不能让短窗口占便宜。

    假试算器按 horizon 返回不同 remaining（5 分钟窗口 0、30 分钟窗口 2），自身窗口都没有基线越界：
    旧实现在自身窗口里选了短候选，新实现按统一 30 分钟窗口比较，两者剩余相同后按代价选长候选。
    """
    action = proposal().actions[0]
    short = proposal().model_copy(update={"proposal_id": "proposal-a", "expert": "hvac", "actions": (
        action.model_copy(update={"command": "hold", "interval": Interval(start=AT, end=AT + timedelta(minutes=5))}),)})
    long = proposal().model_copy(update={"proposal_id": "proposal-b", "expert": "energy", "actions": (
        action.model_copy(update={"command": "precool",
                                  "interval": Interval(start=AT + timedelta(minutes=10), end=AT + timedelta(minutes=30))}),)})
    policy = PolicyConfig(policy_version="p1", arbitration_weights={"cny": 1},
                          priority_profiles={"default": ("safety", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    windows: list[timedelta] = []

    def trial(snapshot, actions, horizon):
        length = horizon.end - horizon.start
        windows.append(length)
        cost = sum(9.0 if item.command == "hold" else 1.0 for item in actions)
        return TrialResult(feasible=True, soft_cost={"cny": cost},
                           remaining_violation_minutes=0 if length <= timedelta(minutes=5) else 2)

    selected, _ = ArbitrationService(None).arbitrate([short, long], policy=policy, snapshot=object(), trial_runner=trial)
    assert [p.proposal_id for p in selected] == ["proposal-b"]
    assert windows and all(length == timedelta(minutes=30) for length in windows)  # 只按统一窗口试算一次
