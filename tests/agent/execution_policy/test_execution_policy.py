"""B47 验收冒烟：第一幕重大损失立即开单与预案保护动作改变账本两槽；第三幕困人工单确认后协同处置。

剧本与参数都是 synthetic/configured 演示值；一次装配跑完两幕，模块级夹具共用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plantkg.agent.execution_policy import demo, policy
from plantkg.agent.execution_policy.records import ExecutionRecord, StoredTempRule, WorkOrderTask
from plantkg.agent.execution_policy.service import ExecutionPolicyService
from plantkg.agent.execution_policy.store import ExecutionStore
from plantkg.core.contracts import DayEvent, Interval, ProposedAction, TempRule
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("b47")
    first = demo.first_act(DEMO, db_dir)
    control = demo.control_act(DEMO, db_dir)
    third = demo.third_act(DEMO, first["main"])
    main_db, registry, runner, _ = first["main"]
    service = demo._service(registry)
    trace = runner.run_until(demo._scenario(DEMO).instant("+1 16:05"))
    try:
        yield SimpleNamespace(first=first, control=control, third=third, runner=runner, trace=trace,
                              service=service, registry=registry)
    finally:
        main_db.close()


def test_first_act_major_loss_opens_work_order_and_runs_protection(run):
    first = run.first
    ticket = first["ticket"]
    assert ticket.status in ("resolved", "waiting")
    router = run.runner.dispatcher.router
    assert router.store.channel_of(ticket.ticket_id) == "urgent"
    assert first["estimate"]["loss_cny"] == 12000.0 and first["estimate"]["production_stop"] is False
    order = next(task for task in first["tasks"] if task.kind == "work_order")
    assert order.status == "proposed" and order.playbook_id == "transformer_overload"
    assert {action.command for action in order.actions} >= {"set_mode", "shed"}  # 处置方案 + 未自动执行的提案动作
    protection = first["executed"]
    assert protection and all(item["source"]["b47"] == "protection" for item in protection)
    assert {item["action"]["command"] for item in protection} >= {"discharge", "set_mode"}
    assert first["decision"] is not None and first["decision"].layer == 2
    assert first["decision"].result.selected_proposal_ids
    # 提案里的同一个放电动作不再执行第二次（预案已执行）；执行表按（提案, 序号, 轮次）幂等
    proposal_discharge = [item for item in run.service.store.executions(ticket.ticket_id)
                          if item.command == "discharge" and item.proposal_id]
    assert proposal_discharge and all(item.status == "skipped_duplicate" for item in proposal_discharge)
    assert len([item for item in protection if item["action"]["command"] == "discharge"]) == 1
    # 见过的类别在等级表里记的是实际生效等级 L2，不是“未见”的 L0
    assert run.service.store.level("load_spike|air_compressor") == ("L2", True)


def test_protection_actions_change_first_act_two_slots(run):
    main_slots, control = run.first["slots"], run.control
    for label in ("08:00", "08:15"):
        assert main_slots[label].discharge_kwh > 0
        # #258 统一窗口（D1）后对照组的第二张事件单（TK-0006）也会选中含放电的提案组合并执行：对照组同样有放电。
        # 仍成立：主运行放电不少于对照组（08:00 槽更多）、SOC 更低。
        assert main_slots[label].discharge_kwh >= control[label].discharge_kwh
        assert main_slots[label].soc_end < control[label].soc_end
        # 已反转：D1 后第一幕两槽电费、购电主运行反而高于对照组（08:00 槽 164.87 对 160.32，08:15 槽 175.71 对 168.12），
        # 这里如实断言当前方向；第一幕叙事由另开的 issue 重写，改叙事时同步改这两行。
        assert main_slots[label].cost_cny > control[label].cost_cny
        assert main_slots[label].grid_import_kwh > control[label].grid_import_kwh
    assert main_slots["08:00"].discharge_kwh > control["08:00"].discharge_kwh
    assert main_slots["08:00"].peak_minute_kw < control["08:00"].peak_minute_kw


def test_trapped_work_order_is_not_executed_before_confirmation(run):
    third = run.third
    assert third["before"] == []
    task = third["task"]
    assert task is not None and task.status == "done"
    assert {action.command for action in task.actions} >= {"set_mode", "ventilate", "broadcast", "rescue"}
    assert third["note"]["executed"] == len(third["after"]) == 5
    assert third["note"]["order_to_done_min"] == 10.0 and third["note"]["event_to_order_min"] == 0.0
    assert all(item["source"].get("work_order_id") == task.work_order_id for item in third["after"])


def test_unseen_category_l0_only_proposes(run):
    """从未见过的类别（剧本 policy 里 unknown_combo: L0）只提议：动作进人工待办，不执行。"""
    from plantkg.core.contracts import ExpertProposal, Interval, ProposalCost, ProposedAction, RunBinding

    router = run.runner.dispatcher.router
    ticket_id = router.store.event_ticket("day2-combo")
    at = run.runner.now
    binding = RunBinding(run_id=run.runner.run_info.run_id, knowledge_version=run.runner.knowledge_version,
                         ticket_id=ticket_id, round=1, state_version=run.runner.state.state_version, trigger_at=at)
    action = ProposedAction(entity_id=run.runner.world.index.eid("AC-1"), command="repair", params={},
                            interval=Interval(start=at, end=at + timedelta(minutes=1)))
    proposal = ExpertProposal(expert="equipment", ticket_id=ticket_id, actions=(action,), cost=ProposalCost(cny=0.0),
                              confidence=0.9, binding=binding)
    before = [item for item in run.trace.actions if item.get("source", {}).get("ticket_id") == ticket_id]
    receipt = run.service.execute(run.runner, proposal, run.runner.tickets.tickets[ticket_id])
    assert receipt.outcome == "queued_human" and receipt.work_order_ids  # 回执（#168）不再用 0 表示排队
    tasks = [task for task in run.service.store.tasks(ticket_id) if task.kind == "human_decision"]
    assert tasks and tasks[-1].actions and tasks[-1].status == "proposed"
    count = len(tasks)
    again = run.service.execute(run.runner, proposal, run.runner.tickets.tickets[ticket_id])
    assert again.work_order_ids == () and "没有需要执行的动作" in again.reason  # 重复提交不再开单
    assert len([task for task in run.service.store.tasks(ticket_id) if task.kind == "human_decision"]) == count
    after = [item for item in run.trace.actions if item.get("source", {}).get("ticket_id") == ticket_id]
    assert after == before
    # 驳回：不执行、事件单标受阻（Ticket.status 只用契约里的值）、并进纠错队列
    task = tasks[-1]
    assert run.service.reject(task.work_order_id, at, "现场不同意")
    assert run.service.store.task(task.work_order_id).status == "rejected"
    assert router.store.ticket(ticket_id).status == "blocked"
    assert [item for item in run.service.store.queue("correction") if item.ticket_id == ticket_id]


def test_instruction_becomes_temp_rule_with_estimated_cost(run):
    scenario = demo._scenario(DEMO)
    at = scenario.instant("+1 17:00")
    event = DayEvent(event_id="EV-B47-TEMP", at=at, event_type="operator_instruction", source="operator_message",
                     severity=2, summary="明天上午检查，行政楼保持 23 ℃", payload={
                         "text": "明天上午检查，行政楼保持 23 ℃", "zone": "admin", "setpoint_c": 23.0,
                         "start": "18:00", "end": "19:00"}, nature="synthetic")
    run.service.on_instruction(event)
    stored = [item for item in run.service.store.temp_rules() if item.rule.source_event_id == event.event_id]
    assert stored and stored[0].rule.entity_ids and stored[0].rule.estimated_cost_cny is not None
    assert any(item.get("source", {}).get("temp_rule") == stored[0].rule.rule_id for item in run.trace.actions)
    assert run.service.on_instruction(DayEvent(  # 解析不出区域与设定值：处理器退回未处理
        event_id="EV-B47-TEMP-UNPARSED", at=at, event_type="operator_instruction", source="operator_message",
        severity=2, summary="replay start", payload={"text": "replay start"}, nature="synthetic")) is False


def test_instruction_window_end_at_24_is_normalized(run):
    """24:00 归一为次日 00:00：窗口不再抛异常，指令照常转成临时规则。"""
    scenario = demo._scenario(DEMO)
    router = run.runner.dispatcher.router
    event = DayEvent(event_id="EV-B47-TEMP-24", at=scenario.instant("+1 17:00"), event_type="operator_instruction",
                     source="operator_message", severity=2, summary="今晚行政楼保持 23 ℃", payload={
                         "text": "今晚行政楼保持 23 ℃", "zone": "admin", "setpoint_c": 23.0,
                         "start": "+1 18:00", "end": "24:00"}, nature="synthetic")
    handled = router.handle_event(event)
    assert handled.result == "instruction"
    stored = [item for item in run.service.store.temp_rules() if item.rule.source_event_id == event.event_id]
    assert stored and stored[0].rule.effective.end == scenario.instant("+2 00:00")


def test_instruction_unknown_zone_is_unhandled_with_reason(run):
    """未知区域：处理器返回 False，指令记 instruction_unhandled，状态里留可读原因。"""
    scenario = demo._scenario(DEMO)
    router = run.runner.dispatcher.router
    event = DayEvent(event_id="EV-B47-TEMP-ZONE", at=scenario.instant("+1 17:00"), event_type="operator_instruction",
                     source="operator_message", severity=2, summary="北区保持 22 ℃", payload={
                         "text": "北区保持 22 ℃", "zone": "north", "setpoint_c": 22.0,
                         "start": "+1 18:00", "end": "+1 18:30"}, nature="synthetic")
    handled = router.handle_event(event)
    assert handled.result == "instruction_unhandled"
    state = router.store.ticket_state(f"instruction_{event.event_id}", "parsed")
    assert state is not None and state.startswith("failed") and "north" in state
    assert not [item for item in run.service.store.temp_rules() if item.rule.source_event_id == event.event_id]


def test_instruction_in_hard_zone_goes_to_human_without_applying(run):
    """洁净车间设定值：不下发动作、不登记生效临时规则，记 rejected 与 needs_human。"""
    scenario = demo._scenario(DEMO)
    router = run.runner.dispatcher.router
    event = DayEvent(event_id="EV-B47-TEMP-HARD", at=scenario.instant("+1 17:00"), event_type="operator_instruction",
                     source="operator_message", severity=2, summary="洁净车间保持 26 ℃", payload={
                         "text": "洁净车间保持 26 ℃", "zone": "cleanroom", "setpoint_c": 26.0,
                         "start": "+1 18:00", "end": "+1 18:30"}, nature="synthetic")
    handled = router.handle_event(event)
    assert handled.result == "instruction"
    assert not [item for item in run.service.store.temp_rules() if item.rule.source_event_id == event.event_id]
    assert not [a for a in run.trace.actions if a.get("source", {}).get("event_id") == event.event_id]
    rejected = [n for n in run.trace.notes if n.get("kind") == "rejected" and n.get("event_id") == event.event_id]
    assert rejected and "硬约束" in rejected[0]["reason"]
    assert (router.store.ticket_state(f"instruction_{event.event_id}", "status") or "").startswith("needs_human")


def test_probe_action_executes_within_observation_window(run):
    """D61 试探：可撤回动作按 L2 执行，生效区间就是观察窗口，执行记录可查。"""
    from plantkg.core.contracts import Interval, ProbeProposal, ProposedAction, RunBinding

    router = run.runner.dispatcher.router
    ticket = router.store.ticket("ticket://TK-0018")
    at = run.runner.now
    binding = RunBinding(run_id=run.runner.run_info.run_id, knowledge_version=run.runner.knowledge_version,
                         ticket_id=ticket.ticket_id, round=ticket.round,
                         state_version=run.runner.state.state_version, trigger_at=at)
    action = ProposedAction(entity_id=run.runner.world.index.eid("AC-1"), command="start", params={"kw": 30.0},
                            interval=Interval(start=at, end=at + timedelta(minutes=3)))
    probe = ProbeProposal(ticket_id=ticket.ticket_id, round=ticket.round, actions=(action,), window_min=3,
                          hypothesis="压力读数卡死", expectations=("压力不上升",), created_at=at)
    run.service.on_probe(ticket, probe, binding)
    applied = [item for item in run.trace.actions
               if item.get("source", {}).get("b47") == "probe" and item.get("source", {}).get("ticket_id") == ticket.ticket_id]
    assert applied and applied[-1]["action"]["command"] == "start"
    assert run.service.store.execution(binding.run_id, f"probe#{ticket.ticket_id}#r{ticket.round}#0") is not None


def test_execution_table_is_idempotent_and_task_status_flows(tmp_path):
    writer = SQLiteWriter(tmp_path / "b47.db")
    MigrationRunner(writer).apply()
    try:
        store = ExecutionStore(writer, "plant")
        record = ExecutionRecord(key="P-1#0#r1", run_id="run-1", ticket_id="ticket://TK-1", proposal_id="P-1",
                                 round=1, action_index=0, entity_id="entity://p/battery/B1", command="discharge",
                                 at=datetime(2026, 7, 1, 0, 5, tzinfo=timezone.utc), detail="test")
        assert store.claim_execution(record) is True
        assert store.claim_execution(record) is False
        task = WorkOrderTask(work_order_id="WO-0001", ticket_id="ticket://TK-1", created_at=record.at,
                             reason="安全标记", round=1)
        store.save_task(task)
        assert store.task("WO-0001").status == "proposed"
        store.save_task(task.model_copy(update={"status": "confirmed"}))
        assert store.task("WO-0001").status == "confirmed"
        assert store.pending_task("ticket://TK-1") is None  # 已确认的不再是待确认工单
    finally:
        writer.close()


def test_execution_keys_isolate_events_within_one_run():
    proposal = SimpleNamespace(proposal_id="P-1", expert="energy")
    first = ExecutionPolicyService._execution_key(proposal, 0, 1, "ticket-1")
    second = ExecutionPolicyService._execution_key(proposal, 0, 1, "ticket-2")
    assert first != second


def test_confirmation_rechecks_shifted_full_duration(run, monkeypatch):
    at = run.runner.now + timedelta(hours=2)
    original = Interval(start=at - timedelta(hours=2), end=at - timedelta(hours=1, minutes=15))
    action = ProposedAction(entity_id="entity://p/elevator/E1", command="ventilate", params={}, interval=original)
    task = WorkOrderTask(work_order_id="WO-PR82-LATE", ticket_id="ticket://TK-PR82-LATE",
                         run_id=run.runner.run_info.run_id, created_at=at - timedelta(hours=2),
                         reason="late confirmation", actions=(action,))
    seen = {}

    def trial(router, actions, horizon, *, force=False):
        seen["action"] = actions[0]
        seen["horizon"] = horizon
        assert force
        return SimpleNamespace(violations=[SimpleNamespace(kind="capacity", detail="blocked")])

    monkeypatch.setattr(run.service, "_trial", trial)
    run.service._confirm_task(run.runner.dispatcher.router, run.runner, task, at)
    effective = run.service._effective(at, run.runner.dispatcher.router.policy)
    assert seen["action"].interval == Interval(start=effective, end=effective + timedelta(minutes=45))
    assert seen["horizon"] == Interval(start=effective, end=effective + timedelta(minutes=45))


def test_fork_keeps_parent_and_child_work_order_and_temp_rule(run):
    store = run.service.store
    parent_id, child_id, at = "run-parent-pr82", "run-child-pr82", run.runner.now
    task = WorkOrderTask(work_order_id="WO-PR82-FORK", ticket_id="ticket://TK-PR82-FORK",
                         run_id=parent_id, created_at=at, reason="fork")
    rule = TempRule(rule_id="TR-PR82-FORK", text="rule", effective=Interval(start=at, end=at + timedelta(hours=1)),
                    entity_ids=(), created_at=at, source_event_id="EV-PR82-FORK")
    store.save_task(task)
    store.save_temp_rule(StoredTempRule(rule=rule, run_id=parent_id))
    run.service.on_fork(SimpleNamespace(run_id=parent_id), SimpleNamespace(run_id=child_id), at)
    assert store.task(task.work_order_id).run_id == parent_id
    child_task = store.pending_task(task.ticket_id, child_id)
    assert child_task is not None and child_task.run_id == child_id
    assert store.pending_task(task.ticket_id, parent_id).work_order_id == task.work_order_id
    # 分叉不换规则编号（#245）：主键是 plant_id+rule_id，只有一行，改归子运行
    assert [(item.rule.rule_id, item.run_id) for item in store.temp_rules()
            if item.rule.source_event_id == rule.source_event_id] == [(rule.rule_id, child_id)]


def test_child_round_expiration_keeps_parent_work_order_proposed(run, monkeypatch):
    store = run.service.store
    parent_id, child_id, ticket_id, at = "run-parent-expire", "run-child-expire", "ticket://TK-PR82-EXPIRE", run.runner.now
    parent = WorkOrderTask(work_order_id="WO-PR82-PARENT-EXPIRE", ticket_id=ticket_id, run_id=parent_id,
                           round=1, created_at=at, reason="parent pending", origin="manual")
    child = WorkOrderTask(work_order_id="WO-PR82-CHILD-EXPIRE", ticket_id=ticket_id, run_id=child_id,
                          round=1, created_at=at, reason="child pending", origin="manual")
    store.save_task(parent)
    store.save_task(child)
    monkeypatch.setattr(run.service, "_mirror_task", lambda runner, task: None)

    run.service._expire_older(SimpleNamespace(run_id=child_id), None, ticket_id, 2)

    assert store.task(parent.work_order_id).status == "proposed"
    assert store.task(child.work_order_id).status == "expired"


def test_child_fork_pending_action_confirms_only_child_work_order(run, monkeypatch):
    from plantkg.agent.execution_policy.service import PendingActionHandler
    from plantkg.core.contracts import Actor, PendingActionKind
    from plantkg.core.ports import PendingActionStore

    store = run.service.store
    parent_id, child_id, ticket_id, at = "run-parent-confirm", "run-child-confirm", "ticket://TK-PR82-CONFIRM", run.runner.now
    parent = WorkOrderTask(work_order_id="WO-PR82-PARENT-CONFIRM", ticket_id=ticket_id, run_id=parent_id,
                           round=1, created_at=at, reason="parent pending", kind="work_order")
    store.save_task(parent)

    class PendingStore:
        calls = []

        def propose(self, kind, payload, context):
            self.calls.append((kind, dict(payload), context))
            return SimpleNamespace(action_id=f"PA-{payload['work_order_id']}")

    pending_store = PendingStore()
    monkeypatch.setitem(run.registry.ports, PendingActionStore, pending_store)
    run.service.on_fork(SimpleNamespace(run_id=parent_id), SimpleNamespace(run_id=child_id), at)
    child = next(task for task in store.tasks(ticket_id) if task.run_id == child_id)

    assert child.pending_action_id == f"PA-{child.work_order_id}"
    assert pending_store.calls[-1][0] == PendingActionKind.work_order
    assert pending_store.calls[-1][1]["work_order_id"] == child.work_order_id
    assert pending_store.calls[-1][2].run_id == child_id

    child_router = SimpleNamespace(run_id=child_id, policy=SimpleNamespace(response_budget=SimpleNamespace(layer12_min=1)),
                                   store=SimpleNamespace(ticket=lambda _: None, set_ticket_status=lambda *args: None))
    child_runner = SimpleNamespace(run_info=SimpleNamespace(run_id=child_id), tickets=SimpleNamespace(tickets={}))
    monkeypatch.setitem(run.service._bindings, id(child_router), (child_router, child_runner))
    monkeypatch.setattr(run.service, "_router_for_ticket", lambda _: None)
    assert PendingActionHandler(run.service, "work_order").apply(
        SimpleNamespace(payload={"work_order_id": child.work_order_id}, decided_at=at, proposed_at=at),
        Actor(actor_id="operator", roles=("operator",)))
    assert store.task(child.work_order_id).status == "done"
    assert store.task(parent.work_order_id).status == "proposed"


def test_persisted_downgrade_drives_execution_policy(run):
    category = "load_spike|air_compressor"
    run.service.store.set_level(category, "L0", run.runner.now)
    level = run.service._effective_level(run.runner.dispatcher.router, category)
    assert level == "L0"
    assert policy.decide(level, policy.REVERSIBLE, 0.9, run.runner.dispatcher.router.policy).outcome == policy.PROPOSE_ONLY


def test_suspect_protection_review_defaults_to_execute_on_deadline(run, monkeypatch):
    event = DayEvent(event_id="EV-PR82-SUSPECT", at=run.runner.now, event_type="load_spike", source="script",
                     severity=4, summary="suspect reading", payload={"quality": "bad"}, nature="synthetic")
    monkeypatch.setattr(run.service, "_ask_protection_agent", lambda *args: None)
    assert run.service._protection_decision(run.runner.dispatcher.router, event, ()) is True


def test_suspect_protection_review_honors_live_agent_defer(run, monkeypatch):
    from plantkg.core.ports import AgentResultSource

    event = DayEvent(event_id="EV-PR82-DEFER", at=run.runner.now, event_type="load_spike", source="script",
                     severity=4, summary="suspect reading", payload={"quality": "bad"}, nature="synthetic")
    router = run.runner.dispatcher.router
    monkeypatch.setattr(run.service, "_ticket_for_event", lambda router, event: router.store.ticket("ticket://TK-0005"))

    class Source:
        def get(self, kind, binding, payload):
            assert "protection_actions" in payload
            return {"protection_decision": "defer"}

    router.registry.mode_ports[(AgentResultSource, "live", "investigate")] = Source()
    assert run.service._protection_decision(router, event, ()) is False


def test_demo_accepts_task_card_third_act_selector():
    assert demo._scenario_path("第三幕剧本") == demo.ROOT / "configs/scenarios/factory_demo_day.yaml"


def test_policy_matrix_and_confidence_composition():
    class Proposal:
        confidence = 0.1

    score, parts = policy.compose_confidence(Proposal(), jev_consistent=False, credibility=policy.LOW_CREDIBILITY)
    assert score == pytest.approx(0.1 * policy.JEV_DISCOUNT * policy.LOW_CREDIBILITY)
    assert set(parts) == {"calibrated", "jev", "solver", "credibility"}
    assert policy.decide("L2", policy.REVERSIBLE, 0.8, _policy()).outcome == policy.EXECUTE
    assert policy.decide("L2", policy.REVERSIBLE, 0.5, _policy()).outcome == policy.EXECUTE_SAMPLING
    assert policy.decide("L2", policy.HARD, 0.5, _policy()).outcome == policy.HUMAN_APPROVAL
    assert policy.decide("L1", policy.REVERSIBLE, 0.8, _policy()).outcome == policy.EXECUTE_REVIEW
    assert policy.decide("L0", policy.REVERSIBLE, 0.9, _policy()).outcome == policy.PROPOSE_ONLY
    assert policy.decide("L3", policy.HARD, 0.75, _policy()).outcome == policy.EXECUTE
    assert policy.decide("L2", policy.REVERSIBLE, 0.2, _policy()).outcome == policy.CORRECTION


def test_b47_executes_hvac_set_mode_outside_hard_zones(run):
    """#174 带存储默认装配：非硬区 HVAC set_mode 不再因动作词表/区域泛化被升级；洁净车间硬约束仍进工单。"""
    from plantkg.core.contracts import AgentTiming, ExpertProposal, ProposalCost, RunBinding

    router = run.runner.dispatcher.router
    ticket_id = router.store.event_ticket("act2-light")
    ticket = router.store.ticket(ticket_id)
    trigger = run.runner.now
    binding = RunBinding(run_id=run.runner.run_info.run_id, knowledge_version=run.runner.knowledge_version,
                         ticket_id=ticket_id, round=ticket.round, state_version=run.runner.state.state_version,
                         trigger_at=trigger)
    timing = AgentTiming(origin_event_at=trigger, trigger_at=trigger, effective_at=trigger + timedelta(minutes=1),
                         requested_at=trigger, returned_at=trigger)
    state = run.runner.tickets.tickets[ticket_id]

    def proposer(name, entity):
        action = ProposedAction(entity_id=entity, command="set_mode", params={"mode": "zone"},
                                interval=Interval(start=trigger + timedelta(minutes=1), end=trigger + timedelta(minutes=16)))
        return ExpertProposal(proposal_id=name, expert="hvac", ticket_id=ticket_id, actions=(action,),
                              cost=ProposalCost(cny=0.0), confidence=0.95, diagnosis="区域温度微调",
                              binding=binding, timing=timing)

    admin = run.runner.world.index.zone_eid["admin"]
    cleanroom = run.runner.world.index.zone_eid["cleanroom"]
    receipt = run.service.execute(run.runner, proposer("proposal-b47-admin", admin), state)
    assert receipt.outcome == "executed" and receipt.action_indexes == (0,)  # #168 起 execute 返回回执
    assert run.service.execute(run.runner, proposer("proposal-b47-clean", cleanroom), state).outcome != "executed"
    admin_records = [item for item in run.service.store.executions(ticket_id) if item.proposal_id == "proposal-b47-admin"]
    assert admin_records and admin_records[-1].status == "executed"
    queued = [task for task in run.service.store.tasks(ticket_id)
              if task.kind == "human_decision" and any(a.entity_id == cleanroom for a in task.actions)]
    assert queued and queued[-1].actions[0].command == "set_mode"


def test_elevator_playbook_disposal_comes_from_graph_template(run):
    """#187 第三幕：困人处置动作模板来自 graph.yaml（改模板不用改代码），工单动作随模板生效。"""
    from plantkg.knowledge.graph import GlobalGraph

    graph = GlobalGraph.load()
    playbook = next(item for item in graph.playbooks(event="elevator_stuck") if item["id"] == "elevator_entrapment")
    template = {item["command"]: item for item in playbook["actions"]["disposal"]}
    assert playbook["knowledge_version"] == graph.version
    task = run.third["task"]
    broadcast = next(action for action in task.actions if action.command == "broadcast")
    assert broadcast.params["text"] == template["broadcast"]["params"]["text"]
    assert any(action.command == "ventilate"
               and action.interval.end - action.interval.start == timedelta(minutes=template["ventilate"]["minutes"])
               for action in task.actions)


def test_playbook_template_change_flows_without_code(tmp_path):
    """图谱改一个动作模板并切换版本后，B47 计划出的动作随之变化，不需要改代码。"""
    import shutil

    import yaml

    from plantkg.agent.execution_policy import playbooks
    from plantkg.knowledge.graph import GlobalGraph

    factory = tmp_path / "factory"
    shutil.copytree(ROOT / "configs" / "sites" / "factory", factory)
    graph_yaml = factory / "graph" / "graph.yaml"
    config = yaml.safe_load(graph_yaml.read_text(encoding="utf-8"))
    elevator = next(item for item in config["playbooks"] if item["id"] == "elevator_entrapment")
    elevator["actions"]["disposal"][1]["minutes"] = 30
    graph_yaml.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    changed = GlobalGraph.load(graph_yaml)
    assert changed.version != GlobalGraph.load().version

    event = DayEvent(event_id="EV-PLAN", at=datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc), event_type="elevator_trapped",
                     source="script", severity=5, entity_id="entity://demo-electronics/freight_elevator/FE-2",
                     summary="困人", nature="synthetic")
    runner = SimpleNamespace(world=SimpleNamespace(ids=lambda: {"freight": "entity://demo-electronics/elevator_group/freight"}))
    playbook = next(item for item in changed.playbooks(event="elevator_stuck") if item["id"] == "elevator_entrapment")
    _, disposal, _ = playbooks.plan(playbook, event, runner, event.at)
    ventilate = next(action for action in disposal if action.command == "ventilate")
    assert ventilate.interval.end - ventilate.interval.start == timedelta(minutes=30)


def test_playbook_template_skips_unresolvable_entity_and_keeps_code_fallback():
    """模板里的实体角色解析不到时跳过并记 note；图谱没给动作模板时回退 playbooks.py 的代码缺省映射。"""
    from plantkg.agent.execution_policy import playbooks

    event = DayEvent(event_id="EV-PLAN", at=datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc), event_type="elevator_trapped",
                     source="script", severity=5, entity_id="entity://demo-electronics/freight_elevator/FE-2",
                     summary="困人", nature="synthetic")
    runner = SimpleNamespace(world=SimpleNamespace(ids=lambda: {"freight": "entity://demo-electronics/elevator_group/freight"}))
    with_template = {"id": "elevator_entrapment", "actions": {"protection": [], "disposal": [
        {"role": "not_assembled", "command": "set_mode", "params": {}, "minutes": 10, "readiness": "needs_human"},
        {"role": "$event", "command": "ventilate", "params": {"kw": 3.0}, "minutes": 10, "readiness": "needs_human"}]}}
    _, disposal, notes = playbooks.plan(with_template, event, runner, event.at)
    assert [action.entity_id for action in disposal] == [event.entity_id]
    assert list(notes) == [{"playbook_id": "elevator_entrapment", "role": "not_assembled", "command": "set_mode",
                            "reason": "实体解析不到，跳过"}]

    _, fallback, notes = playbooks.plan({"id": "elevator_entrapment"}, event, runner, event.at)
    assert not notes and {action.command for action in fallback} == {"set_mode", "ventilate", "broadcast"}


def _policy():
    from plantkg.core.contracts import PolicyConfig, ResponseBudget

    return PolicyConfig(policy_version="test", arbitration_weights={},
                        priority_profiles={"default": ("safety",)}, response_budget=ResponseBudget(layer12_min=1, layer3_min=3))


