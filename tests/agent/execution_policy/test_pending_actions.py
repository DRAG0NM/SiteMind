"""#88/#103 待确认动作经默认装配（build_services + 真实 Sqlite store + HTTP 确认端点）的冒烟。

一次装配跑完第一幕：D55 操作工单登记 work_order 待确认项；L1 试探复核登记 review 待确认项，
确认=复核通过、拒绝=回滚并降一级；工单进入终态后待确认项不能再“确认成功但不执行”。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from plantkg.agent.execution_policy import demo
from plantkg.agent.execution_policy.records import WorkOrderTask
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Interval, PendingActionKind, ProbeProposal, ProposedAction, RunBinding
from plantkg.core.ports import PendingActionStore
from plantkg.core.versions import read
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
ACTOR = "u-reviewer"
HEADERS = {"x-plantkg-operator-token": "", "x-plantkg-actor": ACTOR}


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    runtime = tmp_path_factory.mktemp("b47-pending")
    services = build_services(load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(runtime / "catalog.db"),
        "storage.graph": str(runtime / "graph"), "storage.series": str(runtime / "series"),
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


def _decide(plant, action_id: str, decision: str, revision: int, key: str):
    return plant.client.post(f"/api/v1/actions/{action_id}/{decision}", json={
        "reason": "冒烟复核", "expected_revision": revision, "idempotency_key": key}, headers=_headers(plant))


def _probe(plant, round_: int, entity: str, command: str, params: dict, minutes: int = 2) -> None:
    runner, router = plant.runner, plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    binding = RunBinding(run_id=runner.run_info.run_id, knowledge_version=runner.knowledge_version,
                         ticket_id=ticket.ticket_id, round=ticket.round,
                         state_version=runner.state.state_version, trigger_at=runner.now)
    proposal = ProbeProposal(ticket_id=ticket.ticket_id, round=round_, actions=(ProposedAction(
        entity_id=entity, command=command, params=params,
        interval=Interval(start=runner.now, end=runner.now + timedelta(minutes=minutes))),),
        window_min=minutes, hypothesis="冒烟探针", expectations=("读数有变化",), created_at=runner.now)
    plant.runner.registry.hooks_for("router.probe")[-1](ticket, proposal, binding)


def _review_item(plant, round_: int):
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    item_id = f"review-{ticket.ticket_id.rsplit('/', 1)[-1]}-{round_}-manual-0"
    return item_id, next(item for item in plant.service.store.queue("review") if item.item_id == item_id)


def _event_category(plant) -> str:
    router = plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    event = router.store.event(ticket.event_ids[-1])["event"]
    for hook in reversed(plant.runner.registry.hooks_for("router.entity_type")):
        entity_type = hook(event.entity_id)
        if entity_type:
            return plant.service.store.category(event.event_type, entity_type)
    return plant.service.store.category(event.event_type, None)


def test_work_order_pending_action_visible_and_confirmable_through_http(plant):
    """#88：真实 store 下工单登记带 target_ref，/actions 可见，统一确认端点能执行。"""
    order = next(task for task in plant.service.store.tasks(plant.ticket_id) if task.kind == "work_order")
    assert order.status == "proposed" and order.pending_action_id  # 修前：pending_action_id 永远为空

    action = plant.store.get(order.pending_action_id)
    assert action.kind == "work_order" and action.target_ref == order.work_order_id
    assert action.payload["target_ref"] == order.work_order_id

    page = plant.client.get("/api/v1/actions", params={"kind": "work_order", "status": "proposed"}).json()
    assert any(item["action_id"] == order.pending_action_id for item in page["items"])  # 修前：/actions 看不到

    response = _decide(plant, order.pending_action_id, "confirm", action.revision, "wo-confirm")
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
    assert plant.service.store.task(order.work_order_id).status == "done"
    assert any(item.get("source", {}).get("work_order_id") == order.work_order_id
               for item in plant.runner.trace.actions)


def test_review_queue_confirm_marks_correct(plant):
    """#103：L1 先做后审登记 review 待确认项，确认端点确认=复核通过，队列回写 correct。"""
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    round_ = max(ticket.round, 1) + 1
    entity = plant.runner.world.index.eid("zone:admin")
    _probe(plant, round_, entity, "set_mode", {"mode": "precool"})
    item_id, item = _review_item(plant, round_)
    assert item.status == "pending"

    pending = next(a for a in plant.store.list(kind="review")[0] if a.payload.get("item_id") == item_id)
    assert pending.target_ref == item_id and pending.kind == "review"

    response = _decide(plant, pending.action_id, "confirm", pending.revision, f"review-ok-{round_}")
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
    assert _review_item(plant, round_)[1].status == "correct"


def test_review_reject_rolls_back_and_downgrades(plant):
    """#103：拒绝复核项=复核不通过：回滚动作并把该类事件降一级。"""
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    round_ = max(ticket.round, 1) + 2
    category = _event_category(plant)
    plant.service.store.mark_seen(category, plant.runner.now, "L2")
    assert plant.service.store.level(category)[0] == "L2"

    entity = plant.runner.world.index.eid("zone:admin")
    _probe(plant, round_, entity, "set_mode", {"mode": "off"})
    item_id, _ = _review_item(plant, round_)
    pending = next(a for a in plant.store.list(kind="review")[0] if a.payload.get("item_id") == item_id)

    response = _decide(plant, pending.action_id, "reject", pending.revision, f"review-bad-{round_}")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert _review_item(plant, round_)[1].status == "wrong"  # 修前：review_outcome 无入口，队列永远 pending
    assert plant.service.store.level(category)[0] == "L1"
    assert any(item.get("source", {}).get("item_id") == item_id and item["source"].get("b47") == "rollback"
               for item in plant.runner.trace.actions)


def test_terminal_task_cancels_pending_action(plant):
    """#169：工单过期/拒绝后待确认项同步关闭，确认端点返回冲突而不是“确认成功但不执行”。"""
    runner = plant.runner
    ticket = runner.dispatcher.router.store.ticket(plant.ticket_id)
    at = runner.now
    expiring = WorkOrderTask(work_order_id=plant.service.store.next_id("HD"), ticket_id=ticket.ticket_id,
                             run_id=runner.run_info.run_id, round=ticket.round,
                             created_at=at - timedelta(minutes=60), reason="超时待办", kind="human_decision")
    plant.service.store.save_task(expiring)
    plant.service._propose_pending(expiring, PendingActionKind.human_decision)
    pending_id = plant.service.store.task(expiring.work_order_id).pending_action_id
    assert pending_id

    plant.service.step(runner.run_info.run_id, at)  # human_deadline_min=30
    assert plant.service.store.task(expiring.work_order_id).status == "expired"
    assert plant.store.get(pending_id).status == "expired"
    assert "到期未处理" in plant.store.get(pending_id).payload["cancel_reason"]
    assert _decide(plant, pending_id, "confirm", 0, "expired-confirm").status_code == 409

    rejected = WorkOrderTask(work_order_id=plant.service.store.next_id("HD"), ticket_id=ticket.ticket_id,
                             run_id=runner.run_info.run_id, round=ticket.round, created_at=at,
                             reason="现场拒绝", kind="human_decision")
    plant.service.store.save_task(rejected)
    plant.service._propose_pending(rejected, PendingActionKind.human_decision)
    rejected = plant.service.store.task(rejected.work_order_id)
    assert plant.service.reject(rejected.work_order_id, at, "现场不同意")
    assert plant.store.get(rejected.pending_action_id).status == "expired"
    assert _decide(plant, rejected.pending_action_id, "confirm", 0, "rejected-confirm").status_code == 409
    correction = next(a for a in plant.store.list(kind="correction")[0] if a.target_ref and a.target_ref.startswith("correction-"))
    assert correction.target_ref == correction.payload["item_id"]  # #88：correction 也带 target_ref、/actions 可见


def _human_decision(plant, reason: str):
    """登记一条人工待办（human_decision）并返回任务与它的待确认动作。"""
    runner = plant.runner
    ticket = runner.dispatcher.router.store.ticket(plant.ticket_id)
    task = WorkOrderTask(work_order_id=plant.service.store.next_id("HD"), ticket_id=ticket.ticket_id,
                         run_id=runner.run_info.run_id, round=ticket.round, created_at=runner.now,
                         reason=reason, kind="human_decision")
    plant.service.store.save_task(task)
    plant.service._propose_pending(task, PendingActionKind.human_decision)
    task = plant.service.store.task(task.work_order_id)
    return task, plant.store.get(task.pending_action_id)


def test_human_decision_confirm_and_reject_through_http(plant):
    """B35 批准卡：human_decision 待办经统一确认端点批准=任务完成，驳回=任务退回，/actions 同步状态。"""
    approved, approved_action = _human_decision(plant, "人工确认：3 号空压机停机检修")
    rejected, rejected_action = _human_decision(plant, "人工确认：夜间巡检是否增派")

    response = _decide(plant, approved_action.action_id, "confirm", approved_action.revision, "hd-confirm")
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
    assert plant.service.store.task(approved.work_order_id).status == "done"

    response = _decide(plant, rejected_action.action_id, "reject", rejected_action.revision, "hd-reject")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert plant.service.store.task(rejected.work_order_id).status == "rejected"

    page = plant.client.get("/api/v1/actions", params={"kind": "human_decision", "page_size": 100}).json()
    statuses = {item["action_id"]: item["status"] for item in page["items"]}
    assert statuses[approved_action.action_id] == "confirmed"
    assert statuses[rejected_action.action_id] == "rejected"


def test_correction_reason_reaches_evolution_feedback(plant):
    """#247：纠错确认把值班填的纠正原文作为 note 交给 B32，时间线里有对应记录。"""
    runner = plant.runner
    ticket = runner.dispatcher.router.store.ticket(plant.ticket_id)
    item_id = f"correction-{ticket.ticket_id.rsplit('/', 1)[-1]}-{max(ticket.round, 1)}-manual-99"
    plant.service._queue("correction", None, max(ticket.round, 1), None, None, 99,
                         reason="低置信：照明与空压同时异常", ticket_id=ticket.ticket_id,
                         run_id=runner.run_info.run_id)
    pending = next(a for a in plant.store.list(kind="correction")[0] if a.payload.get("item_id") == item_id)

    text = "先查配电回路，照明与空压同时异常多半是同一路电源"
    response = plant.client.post(f"/api/v1/actions/{pending.action_id}/confirm", json={
        "reason": text, "expected_revision": pending.revision, "idempotency_key": "correction-note"}, headers=_headers(plant))
    assert response.status_code == 200, response.text

    row = read(plant.services.storage.writer, lambda con: con.execute(
        "SELECT event_type, outcome, note, ticket_id FROM b32_feedback WHERE plant_id=? ORDER BY rowid DESC LIMIT 1",
        (plant.services.settings.plant_id,)).fetchone())
    assert row is not None and row[2] == text and row[3] == ticket.ticket_id

    timeline = plant.client.get("/api/v1/evolution/timeline", params={"page_size": 50}).json()["items"]
    assert any(entry["event"] == "feedback_recorded" and entry["details"].get("ticket_id") == ticket.ticket_id
               for entry in timeline)
