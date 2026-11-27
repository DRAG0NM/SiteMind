"""#168 执行生命周期：默认装配（default_registry/packs）+ 真实 Sqlite store 的集成测试。

覆盖：B28 按 B47 回执映射 outcome（排队→work_order）、执行前不可行回执、第 3 层 timing 不按 layer12 判 stale、
事件单三路径状态（确认完成/到期/确认后不可行）、计量检修台账动作、未路由事件 confirm/correct、分叉子工单镜像。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from plantkg.agent.execution_policy import demo
from plantkg.agent.execution_policy.records import WorkOrderTask
from plantkg.core.contracts import (
    AgentTiming, DayEvent, ExpertProposal, Interval, ProposalCost, ProposedAction, RunBinding, Ticket,
    TicketSubscription,
)
from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario

ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
ADMIN = "entity://demo-electronics/zone/admin"
PANEL = "entity://demo-electronics/panel/P-PROD"


def _scenario(events=(), operator_actions=(), dispatch=None, policy=None, days=1) -> Scenario:
    return Scenario.model_validate({
        "scenario_id": "life", "title": "生命周期测试", "date": "2026-07-01", "days": days, "seed": 11,
        "initial": {"soc": 0.5}, "events": list(events), "operator_actions": list(operator_actions),
        "dispatch": dict(dispatch or {}), "policy": dict(policy or {})})


def _runner(tmp_path, *, events=(), operator_actions=(), dispatch=None, policy=None, name="life.db",
            group="coordinated"):
    scenario = _scenario(events, operator_actions, dispatch, policy)
    world = build_world(scenario)
    db = open_db(tmp_path / name, world.site.plant_id)
    runner = ScenarioRunner(world, db, group=group)
    runner.start()
    return runner, db, scenario


def _service(runner):
    return next(h.__self__ for h in runner.registry.hooks_for("router.urgent")
                if getattr(h, "__self__", None) is not None)


def _open_ticket(runner, *, event_id="EV-L3", event_type="equipment_fault", ticket_id="ticket://TK-L3", at=None):
    router = runner.dispatcher.router
    at = at or runner.now
    event = DayEvent(event_id=event_id, at=at, event_type=event_type, source="script", severity=3,
                     summary="测试事件", entity_id=ADMIN, nature="synthetic")
    ticket = Ticket(ticket_id=ticket_id, opened_at=at, status="open", round=1, event_ids=(event_id,),
                    experts=("equipment",),
                    subscriptions=(TicketSubscription(entity_id=event.entity_id, event_types=(event.event_type,)),))
    router.store.record_event(event, "opened", ticket_id)
    router.store.save_ticket(ticket, run_id=router.run_id)
    binding = RunBinding(run_id=router.run_id, knowledge_version=runner.knowledge_version, ticket_id=ticket_id,
                         round=1, state_version=runner.state.state_version, trigger_at=at)
    router.store.save_round(binding, ("equipment",), "test")
    return router, ticket, binding, at


def _proposal(ticket_id, binding, actions, *, confidence=0.9, diagnosis="测试诊断", timing=None):
    return ExpertProposal(expert="equipment", ticket_id=ticket_id, actions=tuple(actions), cost=ProposalCost(cny=0.0),
                          confidence=confidence, diagnosis=diagnosis, binding=binding,
                          timing=timing or AgentTiming(origin_event_at=binding.trigger_at, trigger_at=binding.trigger_at,
                                                       effective_at=binding.trigger_at + timedelta(minutes=1),
                                                       requested_at=binding.trigger_at, returned_at=binding.trigger_at,
                                                       latency_ms=0))


def test_queued_actions_make_decision_work_order(tmp_path):
    """#101：D55 第一幕动作全进工单时，DayDecision 不再是 executed。"""
    first = demo.first_act(DEMO, tmp_path)
    try:
        decision = first["decision"]
        assert decision is not None and decision.result.selected_proposal_ids
        assert decision.result.outcome == "work_order"
        assert "排队" in decision.result.summary or "工单" in decision.result.summary
        order = next(task for task in first["tasks"] if task.kind == "work_order")
        assert order.status == "proposed"
    finally:
        first["main"][0].close()


def test_execute_reports_infeasible_instead_of_silent_zero(tmp_path):
    """#104：执行前复核不可行返回 blocked_infeasible 回执，动作与事件单有可见原因。"""
    runner, db, scenario = _runner(tmp_path)
    try:
        runner.run_until(scenario.instant("00:03"))
        router, ticket, binding, at = _open_ticket(runner)
        action = ProposedAction(entity_id="entity://demo-electronics/zone/cleanroom", command="set_mode",
                                params={"mode": "off"}, interval=Interval(start=at, end=at + timedelta(minutes=5)))
        receipt = _service(runner).execute(runner, _proposal(ticket.ticket_id, binding, [action]),
                                           runner.tickets.tickets.get(ticket.ticket_id))
        assert receipt.outcome == "blocked_infeasible"
        assert "不可行" in receipt.reason
        assert receipt.action_indexes == (0,)
        records = _service(runner).store.executions(ticket.ticket_id)
        assert records and records[0].status == "infeasible" and "不可行" in records[0].detail
    finally:
        db.close()


def test_layer3_dependent_action_is_not_stale_by_layer12_timing(tmp_path):
    """#142：第 3 层按 layer3 预算重算生效时刻，依赖动作照常执行。"""
    runner, db, scenario = _runner(tmp_path, policy={"autonomy_levels": {"equipment_fault": "L2"}})
    try:
        runner.run_until(scenario.instant("00:03"))
        router, ticket, binding, at = _open_ticket(runner)
        action = ProposedAction(entity_id="entity://demo-electronics/battery/BAT1", command="discharge",
                                params={"power_kw": 100.0},
                                interval=Interval(start=at + timedelta(minutes=3), end=at + timedelta(minutes=18)),
                                readiness="needs_diagnosis", depends_on=("root_cause",))
        proposal_id = router.sink.save(_proposal(ticket.ticket_id, binding, [action]), binding)
        router.store.set_ticket_state(ticket.ticket_id, f"reply_status_1_{proposal_id}", "done")
        router.store.set_ticket_state(ticket.ticket_id, f"reply_diagnosis_1_{proposal_id}", "根因确认")

        runner.run_until(at + timedelta(minutes=3))
        result = runner.registry.hooks_for("router.replies_ready")[-1](ticket.ticket_id, 1)

        assert result is not None and result.result.outcome == "executed"
        applied = [a for a in runner.trace.actions
                   if a["source"].get("ticket_id") == ticket.ticket_id and a["source"].get("proposal_id") == proposal_id]
        assert applied and applied[0]["action"]["command"] == "discharge"
        assert router.store.ticket(ticket.ticket_id).status == "resolved"
    finally:
        db.close()


def test_human_deadline_blocks_ticket_not_waiting(tmp_path):
    """#139 到期路径：事件单标 blocked，不能把 'blocked' 当成 pending 事项。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "00:10", "id": "fault", "type": "equipment_fault", "entity_id": "AC-1", "severity": 3,
         "summary": "设备故障"}])
    try:
        runner.run_until(scenario.instant("00:12"))
        router = runner.dispatcher.router
        ticket_id = router.store.event_ticket("fault")
        service = _service(runner)
        at = runner.now
        task = WorkOrderTask(work_order_id="HD-DEADLINE", ticket_id=ticket_id, run_id=router.run_id, round=1,
                             created_at=at - timedelta(minutes=31), reason="人工待办到期测试",
                             kind="human_decision", status="proposed",
                             actions=(ProposedAction(entity_id=ADMIN, command="set_mode", params={"mode": "precool"},
                                                     interval=Interval(start=at, end=at + timedelta(minutes=5))),))
        service.store.save_task(task)

        runner.run_until(scenario.instant("00:13"))

        assert router.store.ticket(ticket_id).status == "blocked"
        pending = runner.tickets.tickets[ticket_id].pending
        assert "blocked" not in pending
        assert service.store.task("HD-DEADLINE").status == "expired"
    finally:
        db.close()


def test_confirm_all_infeasible_blocks_ticket(tmp_path):
    """#139 确认后不可行路径：动作执行失败不得把事件单写成 resolved。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "00:10", "id": "fault2", "type": "equipment_fault", "entity_id": "AC-1", "severity": 3,
         "summary": "设备故障"}])
    try:
        runner.run_until(scenario.instant("00:12"))
        router = runner.dispatcher.router
        ticket_id = router.store.event_ticket("fault2")
        service = _service(runner)
        at = runner.now
        task = WorkOrderTask(work_order_id="WO-INFEASIBLE", ticket_id=ticket_id, run_id=router.run_id, round=1,
                             created_at=at, reason="确认后不可行测试", kind="work_order", status="proposed",
                             actions=(ProposedAction(entity_id=PANEL, command="repair",
                                                     interval=Interval(start=at, end=at + timedelta(minutes=5))),))
        service.store.save_task(task)

        assert service.confirm("WO-INFEASIBLE", at) is True

        done = service.store.task("WO-INFEASIBLE")
        assert done.status == "done" and "全部不可执行" in done.basis
        assert router.store.ticket(ticket_id).status == "blocked"
        assert "resolved" not in runner.tickets.tickets[ticket_id].pending
    finally:
        db.close()


def test_metering_maintenance_is_record_only(tmp_path):
    """#84/#168 方案 4：计量检修走台账动作，确认后只落记录，不调 runner.apply、不误 done。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "00:10", "id": "fault3", "type": "equipment_fault", "entity_id": "AC-1", "severity": 3,
         "summary": "设备故障"}])
    try:
        runner.run_until(scenario.instant("00:12"))
        router = runner.dispatcher.router
        ticket_id = router.store.event_ticket("fault3")
        service = _service(runner)
        at = runner.now
        task = WorkOrderTask(work_order_id="WO-METER", ticket_id=ticket_id, run_id=router.run_id, round=1,
                             created_at=at, reason="计量检修台账", kind="work_order", status="proposed",
                             actions=(ProposedAction(entity_id=PANEL, command="log_maintenance",
                                                     params={"kind": "metering", "meter": "M-1"},
                                                     interval=Interval(start=at, end=at + timedelta(minutes=5))),))
        service.store.save_task(task)

        assert service.confirm("WO-METER", at) is True

        assert service.store.task("WO-METER").status == "done"
        record = service.store.execution(router.run_id, "wo#WO-METER#0")
        assert record is not None and record.status == "executed" and "台账" in record.detail
        assert router.store.ticket(ticket_id).status == "resolved"
        assert not [a for a in runner.trace.actions if a["action"]["entity_id"] == PANEL]
    finally:
        db.close()


def test_unrouted_confirm_and_correct_are_ignored(tmp_path):
    """#137：噪声/未路由事件的 confirm 与 correct 不中止运行，各记一条 ignored（含同分钟操作排在事件前）。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "00:05", "id": "noise", "type": "lighting_fault", "entity_id": "LT-A03", "severity": 0, "summary": "噪声"}],
        operator_actions=[{"at": "00:05", "kind": "confirm", "payload": {"event": "noise"}},
                          {"at": "00:06", "kind": "correct", "payload": {"event": "noise", "summary": "现场核实"}}])
    try:
        runner.run_until(scenario.instant("00:10"))
        kinds = [n["kind"] for n in runner.trace.notes]
        assert "confirm_ignored" in kinds and "correct_ignored" in kinds
        assert not runner.trace.tickets
    finally:
        db.close()


def test_direct_dispatcher_unrouted_confirm_and_correct_are_ignored(tmp_path):
    """#137 直派器：未路由事件没有 by_event 时 confirm/correct 只记 ignored，不 KeyError。"""
    runner, db, scenario = _runner(tmp_path, group="independent", events=[
        {"at": "00:05", "id": "noroute", "type": "mystery_alarm", "entity_id": "AC-1", "severity": 2, "summary": "未路由"}],
        operator_actions=[{"at": "00:10", "kind": "confirm", "payload": {"event": "noroute"}},
                          {"at": "00:11", "kind": "correct", "payload": {"event": "noroute", "summary": "现场核实"}}])
    try:
        runner.run_until(scenario.instant("00:15"))
        kinds = [n["kind"] for n in runner.trace.notes]
        assert any(n["kind"] == "admission" and n.get("result") == "no_route" for n in runner.trace.notes)
        assert "confirm_ignored" in kinds and "correct_ignored" in kinds
    finally:
        db.close()


def test_fork_mirrors_child_work_order_and_finishes_parent_started(tmp_path):
    """#106：proposed 子工单交接并进场景视图；父运行已确认/处置中的留在父运行记 done。"""
    runner, db, scenario = _runner(tmp_path)
    try:
        runner.run_until(scenario.instant("00:05"))
        at = runner.now
        parent_run = runner.run_info.run_id
        service = _service(runner)
        ticket_id = "ticket://TK-FORK"
        proposed = WorkOrderTask(work_order_id="WO-FORK-PROPOSED", ticket_id=ticket_id, run_id=parent_run,
                                 round=1, created_at=at, reason="分叉待确认", kind="work_order", status="proposed")
        started = WorkOrderTask(work_order_id="WO-FORK-STARTED", ticket_id=ticket_id, run_id=parent_run,
                                round=1, created_at=at, reason="分叉处置中", kind="work_order", status="in_progress")
        service.store.save_task(proposed)
        service.store.save_task(started)

        child_run = runner.fork(at)
        service.store.task(proposed.work_order_id)  # 父工单原样保留

        child = service.store.pending_task(ticket_id, child_run.run_id)
        assert child is not None and child.work_order_id in runner.trace.work_orders
        assert service.store.task("WO-FORK-PROPOSED").status == "proposed"
        done = service.store.task("WO-FORK-STARTED")
        assert done.status == "done" and "父运行" in done.basis
        assert done.work_order_id in runner.trace.work_orders
        assert service.store.pending_task(ticket_id, child_run.run_id).work_order_id != "WO-FORK-STARTED"
    finally:
        db.close()
