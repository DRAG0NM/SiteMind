"""#240 方案 1/2/7（含追加 #234）：统一端点拒绝回写、确认回执、拒绝修订。

全部走默认装配（build_services + 真实 Sqlite store + HTTP 端点），只读核对执行表、事件单与 B32 反馈。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from plantkg.agent.execution_policy import demo
from plantkg.agent.execution_policy.service import TICKET_WRITEBACK_HOOK
from plantkg.agent.execution_policy.records import WorkOrderTask
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    AgentTiming, DayEvent, ExpertProposal, Interval, PendingActionKind, ProbeProposal, ProposalCost, ProposedAction,
    RunBinding, Ticket, TicketSubscription,
)
from plantkg.core.ports import PendingActionStore
from plantkg.evolution.store import EvolutionStore
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
HEADERS = {"x-plantkg-operator-token": "", "x-plantkg-actor": "u-reviewer"}
ADMIN = "entity://demo-electronics/zone/admin"
PANEL = "entity://demo-electronics/panel/P-PROD"


@pytest.fixture()
def plant(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series"),
        "actions.reviewers": "[u-reviewer]"}))
    scene = demo._scenario(DEMO)
    policy = dict(scene.policy)
    policy["autonomy_levels"] = {**(policy.get("autonomy_levels") or {}), "probe": "L1"}
    scene = scene.model_copy(update={"policy": policy})
    world = build_world(scene)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(services.storage.writer, world.site.plant_id),
                            registry=default_registry(services.registry), mode="evaluation", group="coordinated")
    runner.start()
    runner.run_until(scene.instant("08:31"))
    service = services.registry.hooks_for("router.urgent")[0].__self__
    store = services.registry.resolve(PendingActionStore)
    client = TestClient(create_app(services))
    try:
        yield SimpleNamespace(services=services, runner=runner, service=service, store=store, client=client,
                              ticket_id=runner.dispatcher.router.store.event_ticket("act1-spike"))
    finally:
        client.close()
        services.close()


def _headers(plant) -> dict[str, str]:
    return HEADERS | {"x-plantkg-operator-token": plant.services.version_run.operator.token}


def _decide(plant, action_id: str, decision: str, revision: int, key: str, *, edits=None):
    body = {"reason": "冒烟复核", "expected_revision": revision, "idempotency_key": key}
    if edits is not None:
        body["edits"] = edits
    return plant.client.post(f"/api/v1/actions/{action_id}/{decision}", json=body, headers=_headers(plant))


def _probe(plant, round_: int, **kwargs) -> None:
    runner, router = plant.runner, plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    binding = RunBinding(run_id=runner.run_info.run_id, knowledge_version=runner.knowledge_version,
                         ticket_id=ticket.ticket_id, round=ticket.round,
                         state_version=runner.state.state_version, trigger_at=runner.now)
    proposal = ProbeProposal(ticket_id=ticket.ticket_id, round=round_, actions=(ProposedAction(
        entity_id=kwargs.get("entity", runner.world.index.eid("zone:admin")), command="set_mode",
        params=kwargs.get("params", {"mode": "precool"}),
        interval=Interval(start=runner.now, end=runner.now + timedelta(minutes=2))),),
        window_min=2, hypothesis="冒烟探针", expectations=("读数有变化",), created_at=runner.now)
    plant.runner.registry.hooks_for("router.probe")[-1](ticket, proposal, binding)


def _review_item(plant, round_: int):
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    item_id = f"review-{ticket.ticket_id.rsplit('/', 1)[-1]}-{round_}-manual-0"
    return item_id, next(item for item in plant.service.store.queue("review") if item.item_id == item_id)


def _pending_review(plant, item_id: str):
    return next(a for a in plant.store.list(kind="review")[0] if a.payload.get("item_id") == item_id)


def _event_category(plant) -> str:
    router = plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    event = router.store.event(ticket.event_ids[-1])["event"]
    for hook in reversed(plant.runner.registry.hooks_for("router.entity_type")):
        entity_type = hook(event.entity_id)
        if entity_type:
            return plant.service.store.category(event.event_type, entity_type)
    return plant.service.store.category(event.event_type, None)


def _b32_store(plant) -> EvolutionStore:
    return EvolutionStore(plant.services.storage.writer, plant.services.settings.plant_id)


def _new_task(plant, kind: str, *, actions=()):
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    task = WorkOrderTask(work_order_id=plant.service.store.next_id("HD" if kind == "human_decision" else "WO"),
                         ticket_id=ticket.ticket_id, run_id=plant.runner.run_info.run_id, round=ticket.round,
                         created_at=plant.runner.now, reason="统一端点拒绝冒烟", kind=kind, actions=actions)
    task = plant.service.store.save_task(task)
    plant.service._propose_pending(task, PendingActionKind(kind))
    return next(action for action in plant.store.list(kind=kind)[0] if action.target_ref == task.work_order_id)


def _action(plant, entity: str, command: str, params=None, *, start_minutes: int = 0) -> ProposedAction:
    at = plant.runner.now + timedelta(minutes=start_minutes)
    return ProposedAction(entity_id=entity, command=command, params=dict(params or {}),
                          interval=Interval(start=at, end=at + timedelta(minutes=5)))


def _open_ticket(plant, *, event_id: str = "EV-R240", event_type: str = "equipment_fault",
                 ticket_id: str = "ticket://TK-R240", entity_id: str = ADMIN):
    """另开一张非 D55 事件单：走执行策略矩阵，不受第一幕 urgent 整单等人工的影响。"""
    runner, router, at = plant.runner, plant.runner.dispatcher.router, plant.runner.now
    event = DayEvent(event_id=event_id, at=at, event_type=event_type, source="script", severity=3,
                     summary="测试事件", entity_id=entity_id, nature="synthetic")
    ticket = Ticket(ticket_id=ticket_id, opened_at=at, status="open", round=1, event_ids=(event_id,),
                    experts=("equipment",),
                    subscriptions=(TicketSubscription(entity_id=entity_id, event_types=(event_type,)),))
    router.store.record_event(event, "opened", ticket_id)
    router.store.save_ticket(ticket, run_id=router.run_id)
    binding = RunBinding(run_id=router.run_id, knowledge_version=runner.knowledge_version, ticket_id=ticket_id,
                         round=1, state_version=runner.state.state_version, trigger_at=at)
    router.store.save_round(binding, ("equipment",), "test")
    return router, ticket, binding, at


def _proposal(ticket_id: str, binding: RunBinding, actions, *, confidence: float = 0.9) -> ExpertProposal:
    return ExpertProposal(expert="equipment", ticket_id=ticket_id, actions=tuple(actions),
                          cost=ProposalCost(cny=0.0), confidence=confidence, diagnosis="测试诊断", binding=binding,
                          timing=AgentTiming(origin_event_at=binding.trigger_at, trigger_at=binding.trigger_at,
                                             effective_at=binding.trigger_at + timedelta(minutes=1),
                                             requested_at=binding.trigger_at, returned_at=binding.trigger_at,
                                             latency_ms=0))


# ---------------------------------------------------------------- 方案 1（#204-1）


def test_work_order_reject_writes_back_task_ticket_and_feedback(plant):
    """拒绝工单：工单立刻 rejected、事件单 blocked、进纠错队列，B32 收到一次纠错反馈。"""
    category = _event_category(plant)
    pending = _new_task(plant, "work_order")

    response = _decide(plant, pending.action_id, "reject", pending.revision, "wo-reject-204",
                       edits={"system_error": False})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    task = plant.service.store.task(pending.payload["work_order_id"])
    assert task.status == "rejected" and "值班人员在统一端点拒绝" in task.basis
    router = plant.runner.dispatcher.router
    assert router.store.ticket(plant.ticket_id).status == "blocked"
    assert "correction" in plant.runner.tickets.tickets[plant.ticket_id].pending
    assert [item for item in plant.service.store.queue("correction") if item.ticket_id == plant.ticket_id]
    rows = _b32_store(plant).feedback_rows(category, 10)
    assert rows and rows[0]["system_error"] is False
    assert _b32_store(plant).autonomy_pending_candidate(category) is None


def test_human_decision_reject_blocks_ticket_with_reason_note(plant):
    """人工待办被否决：任务 rejected、不执行、事件单 blocked，并留“人工否决”note。"""
    pending = _new_task(plant, "human_decision")

    response = _decide(plant, pending.action_id, "reject", pending.revision, "hd-reject-204")

    assert response.status_code == 200, response.text
    task = plant.service.store.task(pending.payload["work_order_id"])
    assert task.status == "rejected" and "人工否决" in task.basis
    assert plant.runner.dispatcher.router.store.ticket(plant.ticket_id).status == "blocked"
    notes = [note for note in plant.runner.trace.notes if note.get("kind") == "b47_human_rejected"]
    assert notes and notes[0]["reason"] == "人工否决"
    assert not [item for item in plant.runner.trace.actions
                if item.get("source", {}).get("work_order_id") == task.work_order_id]


# ---------------------------------------------------------------- 方案 2（#204-4）


def test_confirm_infeasible_reports_outcome_and_keeps_business_status(plant):
    """确认后复核不可行：business_status 仍是 done，data 里带 outcome=blocked_infeasible 与原因。"""
    pending = _new_task(plant, "work_order", actions=(_action(plant, PANEL, "repair"),))

    response = _decide(plant, pending.action_id, "confirm", pending.revision, "confirm-infeasible-204")

    assert response.status_code == 200, response.text
    result = plant.store.result(pending.action_id)
    assert result is not None and result.ok and result.business_status == "done"
    assert result.data["outcome"] == "blocked_infeasible" and result.data["basis"]
    task = plant.service.store.task(pending.payload["work_order_id"])
    assert task.status == "done" and "全部不可执行" in task.basis
    assert plant.runner.dispatcher.router.store.ticket(plant.ticket_id).status == "blocked"


def test_confirm_executed_reports_outcome_and_basis(plant):
    """正常确认：data 带 outcome=executed 与执行条数依据（business_status 不变）。"""
    pending = _new_task(plant, "work_order", actions=(_action(plant, ADMIN, "set_mode", {"mode": "precool"}),))

    response = _decide(plant, pending.action_id, "confirm", pending.revision, "confirm-executed-204")

    assert response.status_code == 200, response.text
    result = plant.store.result(pending.action_id)
    assert result is not None and result.business_status == "done"
    assert result.data["outcome"] == "executed" and "确认后执行 1 个动作" in result.data["basis"]
    assert plant.service.store.task(pending.payload["work_order_id"]).status == "done"


def test_correction_receipt_reports_all_action_indexes(plant):
    """低置信转人工纠错：回执的 action_indexes 覆盖提案里的全部动作（原来只报第一个）。"""
    ticket_id = "ticket://TK-R240-corr"
    _, _, binding, _ = _open_ticket(plant, event_id="EV-R240-corr", ticket_id=ticket_id,
                                    event_type="load_spike", entity_id=ADMIN)
    actions = (_action(plant, ADMIN, "set_mode", {"mode": "precool"}, start_minutes=120),
               _action(plant, ADMIN, "set_mode", {"mode": "off"}, start_minutes=120))
    receipt = plant.service.execute(plant.runner, _proposal(ticket_id, binding, actions, confidence=0.1))

    assert receipt.outcome == "correction"
    assert receipt.action_indexes == (0, 1)


def test_execute_direct_fallback_zero_is_stale_not_executed(plant):
    """直派器兜底：0 个动作不能再记 executed；没有对应事件单时按 stale 回执。"""
    missing = "ticket://TK-R240-missing"
    binding = RunBinding(run_id=plant.runner.run_info.run_id, knowledge_version=plant.runner.knowledge_version,
                         ticket_id=missing, round=1, state_version=plant.runner.state.state_version,
                         trigger_at=plant.runner.now)
    router = plant.runner.dispatcher.router
    proposal_id = router.sink.save(_proposal(missing, binding, (_action(plant, ADMIN, "set_mode", {"mode": "precool"}),)),
                                   binding)
    receipt = plant.service.execute(plant.runner, router.sink.get(proposal_id), None)

    assert receipt.outcome == "stale" and receipt.action_indexes == ()
    assert "没有执行动作" in receipt.reason


# ---------------------------------------------------------------- 方案 7（#234）


def test_reject_accepts_system_error_edit_and_stores_it_for_audit(plant):
    """拒绝带 edits.system_error=false：200、B32 收到 false 且不降级、决定记录留修订。"""
    category = _event_category(plant)
    plant.service.store.mark_seen(category, plant.runner.now, "L2")
    round_ = max(plant.runner.dispatcher.router.store.ticket(plant.ticket_id).round, 1) + 1
    _probe(plant, round_)
    item_id, _ = _review_item(plant, round_)
    pending = _pending_review(plant, item_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, "reject-review-234",
                       edits={"system_error": False})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert _review_item(plant, round_)[1].status == "wrong"
    rows = _b32_store(plant).feedback_rows(category, 10)
    assert rows and rows[0]["system_error"] is False
    assert _b32_store(plant).autonomy_pending_candidate(category) is None
    with plant.services.storage.writer.read_only() as con:
        row = con.execute("SELECT edits FROM action_decisions WHERE plant_id=? AND idempotency_key=?",
                          (plant.services.settings.plant_id, "reject-review-234")).fetchone()
    assert row is not None and json.loads(row[0]) == {"system_error": False}


def test_reject_without_edits_keeps_default_system_error(plant):
    """不带 edits 时行为与现在一致：B32 按系统判断有误记录（默认 true）。"""
    category = _event_category(plant)
    round_ = max(plant.runner.dispatcher.router.store.ticket(plant.ticket_id).round, 1) + 2
    _probe(plant, round_)
    item_id, _ = _review_item(plant, round_)
    pending = _pending_review(plant, item_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, "reject-review-default")

    assert response.status_code == 200, response.text
    rows = _b32_store(plant).feedback_rows(category, 10)
    assert rows and rows[0]["system_error"] is True


def test_reject_edit_with_other_key_is_400(plant):
    """拒绝只允许 system_error 一个修订键，其他键返回 400 并写明允许项。"""
    round_ = max(plant.runner.dispatcher.router.store.ticket(plant.ticket_id).round, 1) + 3
    _probe(plant, round_)
    item_id, _ = _review_item(plant, round_)
    pending = _pending_review(plant, item_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, "reject-review-badkey",
                       edits={"note": "现场说明"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "PLANTKG_E_INVALID_REQUEST"
    assert "system_error" in error["message"]
    assert plant.store.get(pending.action_id).status == "proposed"


def test_reject_edit_not_open_for_human_decision_is_400(plant):
    """human_decision 的拒绝不接受修订；报错写明开放的动作类型。"""
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    task = WorkOrderTask(work_order_id=plant.service.store.next_id("HD"), ticket_id=ticket.ticket_id,
                         run_id=plant.runner.run_info.run_id, round=ticket.round, created_at=plant.runner.now,
                         reason="人工待办", kind="human_decision", actions=())
    task = plant.service.store.save_task(task)
    plant.service._propose_pending(task, PendingActionKind.human_decision)
    pending = next(a for a in plant.store.list(kind="human_decision")[0] if a.target_ref == task.work_order_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, "reject-human-edits",
                       edits={"system_error": False})

    assert response.status_code == 400
    error = response.json()["error"]
    assert "只允许" in error["message"] and "review" in error["message"]
    assert plant.store.get(pending.action_id).status == "proposed"


def test_reject_with_empty_edits_is_treated_as_absent(plant):
    """通用前端对拒绝一律带空 edits 对象时，人工待办仍按未带修订处理（#240 复审）。"""
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    task = WorkOrderTask(work_order_id=plant.service.store.next_id("HD"), ticket_id=ticket.ticket_id,
                         run_id=plant.runner.run_info.run_id, round=ticket.round, created_at=plant.runner.now,
                         reason="人工待办", kind="human_decision", actions=())
    task = plant.service.store.save_task(task)
    plant.service._propose_pending(task, PendingActionKind.human_decision)
    pending = next(a for a in plant.store.list(kind="human_decision")[0] if a.target_ref == task.work_order_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, "reject-human-empty-edits", edits={})

    assert response.status_code == 200, response.text
    assert plant.service.store.task(task.work_order_id).status == "rejected"


# ---------------------------------------------------------------- 复审 P1：未绑定运行时的回写


def test_reject_without_bound_router_blocks_ticket_in_store(plant):
    """#240 复审 P1：runner/router 未绑定时，拒绝仍按任务 run 作用域把事件单 blocked 落库后才返回成功。"""
    ticket_id = plant.ticket_id
    pending = _new_task(plant, "work_order")
    plant.store.set_action_clock(lambda kind, run_id: plant.runner.now)  # 运行断开后 B04 仍按场景时钟判过期
    plant.service._bindings.clear()  # 模拟重启后运行绑定尚未恢复

    response = _decide(plant, pending.action_id, "reject", pending.revision, "wo-reject-unbound")

    assert response.status_code == 200, response.text
    assert plant.service.store.task(pending.payload["work_order_id"]).status == "rejected"
    assert plant.runner.dispatcher.router.store.ticket(ticket_id).status == "blocked"


def test_reject_writeback_failure_keeps_task_proposed_and_retryable(plant, monkeypatch):
    """#240 复审 P1：兜底回写失败返回 503、任务与待确认动作保持 proposed，恢复后可再次拒绝成功。"""
    ticket_id = plant.ticket_id
    pending = _new_task(plant, "work_order")
    plant.store.set_action_clock(lambda kind, run_id: plant.runner.now)
    plant.service._bindings.clear()
    registry = plant.services.registry
    original = registry.hooks_for

    def broken(name, *args, **kwargs):
        return () if name == TICKET_WRITEBACK_HOOK else original(name, *args, **kwargs)

    monkeypatch.setattr(registry, "hooks_for", broken)
    response = _decide(plant, pending.action_id, "reject", pending.revision, "wo-reject-unbound-fail")

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "PLANTKG_E_DEPENDENCY_UNAVAILABLE"
    assert plant.service.store.task(pending.payload["work_order_id"]).status == "proposed"
    assert plant.store.get(pending.action_id).status == "proposed"

    monkeypatch.undo()
    response = _decide(plant, pending.action_id, "reject", pending.revision, "wo-reject-unbound-retry")

    assert response.status_code == 200, response.text
    assert plant.service.store.task(pending.payload["work_order_id"]).status == "rejected"
    assert plant.runner.dispatcher.router.store.ticket(ticket_id).status == "blocked"
