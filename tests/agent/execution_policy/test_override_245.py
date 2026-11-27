"""#245 人工干预：D 块拒绝落库重试、A 块暂停自动执行、B 块取消与撤销。

全部走默认装配（build_services + 真实 SQLite + HTTP 端点），断言只读执行表、事件单、运行状态与 B32 反馈。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from plantkg.agent.execution_policy import demo
from plantkg.agent.execution_policy.records import StoredTempRule, WorkOrderTask
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    AgentTiming, DayEvent, ExpertProposal, Interval, PendingActionKind, ProposalCost, ProposedAction, RunBinding,
    SimAction, TempRule, Ticket, TicketSubscription,
)
from plantkg.core.ports import EvidenceSink, PendingActionStore
from plantkg.evolution.store import EvolutionStore
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.dispatch import TicketState
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, active_actions, default_registry

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
HEADERS = {"x-plantkg-operator-token": "", "x-plantkg-actor": "u-reviewer"}
ADMIN = "entity://demo-electronics/zone/admin"


SETTINGS = {"storage.enabled": True, "actions.reviewers": "[u-reviewer]"}


def _settings(tmp_path, profile: str = "mock"):
    return load_settings(profile=profile, overrides=SETTINGS | {
        "storage.sqlite": str(tmp_path / "catalog.db"), "storage.graph": str(tmp_path / "graph"),
        "storage.series": str(tmp_path / "series")})


def _build(tmp_path, *, pause_first: bool = False, profile: str = "mock"):
    services = build_services(_settings(tmp_path, profile))
    scene = demo._scenario(DEMO)
    policy = dict(scene.policy)
    policy["autonomy_levels"] = {**(policy.get("autonomy_levels") or {}), "equipment_fault": "L2", "load_spike": "L2"}
    scene = scene.model_copy(update={"policy": policy})
    world = build_world(scene)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(services.storage.writer, world.site.plant_id),
                            registry=default_registry(services.registry), mode="evaluation", group="coordinated")
    runner.start()
    service = services.registry.hooks_for("router.urgent")[0].__self__
    client = TestClient(create_app(services))
    plant = SimpleNamespace(services=services, scene=scene, runner=runner, service=service, client=client,
                            store=services.registry.resolve(PendingActionStore), tmp_path=tmp_path)
    if pause_first:  # 第一幕事件到来之前就暂停：看暂停期间的保护动作与提议
        _pause(plant, "演练暂停")
    runner.run_until(scene.instant("08:31"))
    plant.ticket_id = runner.dispatcher.router.store.event_ticket("act1-spike")
    return plant


@pytest.fixture()
def plant(tmp_path):
    built = _build(tmp_path)
    try:
        yield built
    finally:
        built.client.close()
        built.services.close()


@pytest.fixture()
def paused_plant(tmp_path):
    built = _build(tmp_path, pause_first=True)
    try:
        yield built
    finally:
        built.client.close()
        built.services.close()


def _headers(plant) -> dict[str, str]:
    return HEADERS | {"x-plantkg-operator-token": plant.services.version_run.operator.token}


def _pause(plant, reason: str, run_id: str | None = None):
    body = {"reason": reason} | ({"run_id": run_id} if run_id else {})
    response = plant.client.post("/api/v1/autonomy/pause", json=body, headers=_headers(plant))
    assert response.status_code == 200, response.text
    return response.json()


def _status(plant, run_id: str | None = None) -> dict:
    response = plant.client.get("/api/v1/autonomy/status", params={"run_id": run_id} if run_id else None)
    assert response.status_code == 200, response.text
    return response.json()


def _post(plant, path: str, body: dict):
    return plant.client.post(f"/api/v1/{path}", json=body, headers=_headers(plant))


def _applied(plant) -> dict[str, dict]:
    response = plant.client.get("/api/v1/autonomy/actions")
    assert response.status_code == 200, response.text
    return {item["action_id"]: item for item in response.json()["items"]}


def _evidence(plant, event_type: str) -> list:
    context = plant.services.guard.anonymous_read_context().model_copy(
        update={"as_of": datetime(2100, 1, 1, tzinfo=timezone.utc)})
    page = plant.services.registry.resolve(EvidenceSink).list(None, None, context, page_size=200)
    return [event for event in page.items if event.event_type == event_type]


def _category(plant, ticket_id: str) -> str:
    router = plant.runner.dispatcher.router
    event = router.store.event(router.store.ticket(ticket_id).event_ids[-1])["event"]
    return plant.service.store.category(event.event_type, plant.service._entity_type(router, event.entity_id))


def _feedback(plant, ticket_id: str) -> list[dict]:
    store = EvolutionStore(plant.services.storage.writer, plant.services.settings.plant_id)
    return [row for row in store.feedback_rows(_category(plant, ticket_id), 50) if row["source"] == "revoke"]


def _decide(plant, action_id: str, decision: str, revision: int, key: str, *, edits=None):
    body = {"reason": "冒烟复核", "expected_revision": revision, "idempotency_key": key}
    if edits is not None:
        body["edits"] = edits
    return plant.client.post(f"/api/v1/actions/{action_id}/{decision}", json=body, headers=_headers(plant))


def _new_task(plant, kind: str):
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    task = WorkOrderTask(work_order_id=plant.service.store.next_id("HD" if kind == "human_decision" else "WO"),
                         ticket_id=ticket.ticket_id, run_id=plant.runner.run_info.run_id, round=ticket.round,
                         created_at=plant.runner.now, reason="拒绝重试冒烟", kind=kind)
    task = plant.service.store.save_task(task)
    plant.service._propose_pending(task, PendingActionKind(kind))
    return next(action for action in plant.store.list(kind=kind)[0] if action.target_ref == task.work_order_id)


# ---------------------------------------------------------------- D：拒绝决定落库失败后可重试（#258）


@pytest.mark.parametrize("kind", ["work_order", "human_decision"])
def test_reject_retries_after_decision_commit_failure_writes_blocked_once(plant, monkeypatch, kind):
    """决定提交失败后重试：任务已 rejected 时 reject_from_pending 幂等成功，动作进入 rejected，事件单只写一次 blocked。"""
    pending = _new_task(plant, kind)
    work_order_id = pending.payload["work_order_id"]
    blocked_writes: list[str] = []
    original = plant.service._persist_ticket_blocked

    def counting(task):
        blocked_writes.append(task.work_order_id)
        return original(task)

    monkeypatch.setattr(plant.service, "_persist_ticket_blocked", counting)
    commit = plant.store._commit_decision
    failures = {"left": 1}

    def flaky(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("模拟决定落库失败")
        return commit(*args, **kwargs)

    monkeypatch.setattr(plant.store, "_commit_decision", flaky)
    first = _decide(plant, pending.action_id, "reject", pending.revision, f"{kind}-reject-a")
    assert first.status_code == 500, first.text
    assert plant.service.store.task(work_order_id).status == "rejected"  # 拒绝已落到任务，决定还没提交
    assert plant.store.get(pending.action_id).status == "proposed"

    second = _decide(plant, pending.action_id, "reject", pending.revision, f"{kind}-reject-b")
    assert second.status_code == 200, second.text
    assert plant.store.get(pending.action_id).status == "rejected"
    assert plant.service.store.task(work_order_id).status == "rejected"
    assert blocked_writes == [work_order_id]
    assert plant.runner.dispatcher.router.store.ticket(plant.ticket_id).status == "blocked"


# ---------------------------------------------------------------- A：暂停自动执行


def _open_ticket(plant, *, event_id: str, ticket_id: str, entity_id: str = ADMIN, event_type: str = "equipment_fault"):
    """另开一张非 D55 事件单：走执行策略矩阵（场景策略里 equipment_fault 为 L2）。"""
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
    runner.tickets.tickets[ticket_id] = TicketState(ticket=ticket, event=event.model_dump(mode="json"), origin_event_at=at,
                                                    trigger_at=at, entity_id=entity_id)
    return binding, at


def _execute(plant, ticket_id: str, binding, commands: tuple[str, ...] = ("precool",)):
    at = plant.runner.now
    actions = tuple(ProposedAction(entity_id=ADMIN, command="set_mode", params={"mode": mode},
                                   interval=Interval(start=at, end=at + timedelta(minutes=5))) for mode in commands)
    proposal = ExpertProposal(
        expert="equipment", ticket_id=ticket_id, actions=actions, cost=ProposalCost(cny=0.0), confidence=0.9,
        diagnosis="测试诊断", binding=binding,
        timing=AgentTiming(origin_event_at=at, trigger_at=at, effective_at=at + timedelta(minutes=1),
                           requested_at=at, returned_at=at, latency_ms=0))
    router = plant.runner.dispatcher.router
    saved = router.sink.get(router.sink.save(proposal, binding))
    return plant.service.execute(plant.runner, saved)


def test_pause_resume_http_status_auth_and_evidence(plant):
    """pause/resume/status：无令牌被拒；暂停记谁、何时、为什么；重复暂停幂等；各写一条证据事件。"""
    denied = plant.client.post("/api/v1/autonomy/pause", json={"reason": "无令牌"})
    assert denied.status_code == 403
    assert _status(plant)["paused"] is False

    first = _pause(plant, "巡检：先看一眼")
    assert first["paused"] is True and first["by"] == "u-reviewer" and first["reason"] == "巡检：先看一眼"
    assert first["scope"] == "plant" and first["backlog"] == 0 and first["since"]
    second = _pause(plant, "再暂停一次")  # 已在暂停：幂等，保留最初的暂停人、时间与原因
    assert second["since"] == first["since"] and second["reason"] == first["reason"]

    resumed = _post(plant, "autonomy/resume", {})
    assert resumed.status_code == 200, resumed.text
    status = resumed.json()
    assert status["paused"] is False and status["resumed_by"] == "u-reviewer" and status["resumed_at"]
    assert status["by"] == "u-reviewer" and status["reason"] == "巡检：先看一眼"
    assert _post(plant, "autonomy/resume", {}).json()["resumed_at"] == status["resumed_at"]  # 重复恢复幂等

    assert len(_evidence(plant, "autonomy_paused")) == 1
    assert len(_evidence(plant, "autonomy_resumed")) == 1


def test_pause_only_proposes_and_resume_goes_back_to_auto(plant):
    """暂停后新事件的动作只提议（不执行）、登记待批准；恢复后同类新事件回到自动执行。"""
    _pause(plant, "只提议冒烟")
    binding, _ = _open_ticket(plant, event_id="EV-PAUSE-1", ticket_id="ticket://TK-PAUSE-1")
    receipt = _execute(plant, "ticket://TK-PAUSE-1", binding)
    assert receipt.outcome == "queued_human" and receipt.action_indexes == (0,)
    assert not [a for a in plant.runner.trace.actions if (a.get("source") or {}).get("ticket_id") == "ticket://TK-PAUSE-1"]
    backlog = [t for t in plant.service.store.tasks("ticket://TK-PAUSE-1", "proposed") if t.autonomy_paused]
    assert len(backlog) == 1 and backlog[0].kind == "human_decision" and backlog[0].pending_action_id
    assert _status(plant)["backlog"] == 1

    _post(plant, "autonomy/resume", {})
    binding, _ = _open_ticket(plant, event_id="EV-PAUSE-2", ticket_id="ticket://TK-PAUSE-2")
    receipt = _execute(plant, "ticket://TK-PAUSE-2", binding)
    assert receipt.outcome == "executed"
    assert [a for a in plant.runner.trace.actions if (a.get("source") or {}).get("ticket_id") == "ticket://TK-PAUSE-2"]
    assert _status(plant)["backlog"] == 1  # 恢复不放行积压：仍待逐条批准


def test_pause_keeps_protection_running_and_backlog_is_approved_one_by_one(paused_plant):
    """第一幕到来前就暂停：保护动作照常执行并留 note，专家提案只进待批准；恢复后积压逐条批准，没有“全部放行”。"""
    plant = paused_plant
    trace = plant.runner.trace
    protection = [a for a in trace.actions if (a.get("source") or {}).get("b47") == "protection"]
    assert {a["action"]["command"] for a in protection} >= {"discharge", "set_mode"}
    assert [n for n in trace.notes if n.get("kind") == "autonomy_protection_continues"]
    assert not [a for a in trace.actions if (a.get("source") or {}).get("proposal_id")]  # 提案动作没有自动下发

    backlog = [t for t in plant.service.store.tasks(status="proposed") if t.autonomy_paused]
    assert len(backlog) >= 2 and all(len(t.actions) == 1 for t in backlog)  # 一条一张
    assert _status(plant)["backlog"] == len(backlog)

    resumed = _post(plant, "autonomy/resume", {}).json()
    assert resumed["paused"] is False and resumed["backlog"] == len(backlog)  # 恢复不放行积压
    assert plant.client.post("/api/v1/autonomy/approve-all", json={}, headers=_headers(plant)).status_code in (404, 405)

    pending = {a.target_ref: a for a in plant.store.list(kind="human_decision")[0]}
    first = pending[backlog[0].work_order_id]
    assert _decide(plant, first.action_id, "confirm", first.revision, "backlog-1").status_code == 200
    executed = [a for a in plant.runner.trace.actions
                if (a.get("source") or {}).get("work_order_id") == backlog[0].work_order_id]
    assert len(executed) == 1  # 只执行这一条
    assert _status(plant)["backlog"] == len(backlog) - 1
    assert not [a for a in plant.runner.trace.actions
                if (a.get("source") or {}).get("work_order_id") == backlog[1].work_order_id]  # 其余仍待批准


def test_run_pause_is_inherited_by_forked_child(plant):
    """运行级暂停不是整厂暂停；分叉时交给子运行，子运行里的新事件仍只提议。"""
    parent = plant.runner.run_info.run_id
    _pause(plant, "跨分叉", run_id=parent)
    assert _status(plant)["paused"] is False
    child = plant.runner.fork(plant.runner.now).run_id
    status = _status(plant, child)
    assert status["paused"] is True and status["scope"] == "run" and status["reason"] == "跨分叉"
    binding, _ = _open_ticket(plant, event_id="EV-FORK", ticket_id="ticket://TK-FORK")
    assert _execute(plant, "ticket://TK-FORK", binding).outcome == "queued_human"
    resumed = _post(plant, "autonomy/resume", {"run_id": child})
    assert resumed.status_code == 200 and resumed.json()["paused"] is False


def test_pause_survives_restart(tmp_path):
    """暂停状态存在库里：整厂与运行级暂停重启后（重新装配同一个 SQLite）仍然保持。"""
    services = build_services(_settings(tmp_path, "dev"))
    scene = demo._scenario(DEMO)
    world = build_world(scene)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(services.storage.writer, world.site.plant_id),
                            registry=default_registry(services.registry), mode="evaluation", group="coordinated")
    run_id = runner.start().run_id
    client = TestClient(create_app(services))
    plant = SimpleNamespace(services=services, client=client)
    try:
        _pause(plant, "整厂暂停")
        _pause(plant, "运行暂停", run_id=run_id)
    finally:
        client.close()
        services.close()
    reopened = build_services(_settings(tmp_path, "dev"))
    client = TestClient(create_app(reopened))
    plant = SimpleNamespace(services=reopened, client=client)
    try:
        assert _status(plant)["paused"] is True and _status(plant)["reason"] == "整厂暂停"
        run = _status(plant, run_id)
        assert run["paused"] is True and run["scope"] == "run" and run["reason"] == "运行暂停"
    finally:
        client.close()
        reopened.close()


# ---------------------------------------------------------------- B：取消与撤销


def _battery_mode(plant) -> str:
    return plant.runner.state.devices[plant.runner.world.index.battery_id]["mode"]


def test_cancel_scheduled_action_never_takes_effect(plant):
    """取消还没生效的动作：终态 cancelled，动作不生效；重复取消幂等；已生效的动作不能走取消。"""
    runner = plant.runner
    start = runner.now + timedelta(minutes=10)
    runner.apply(SimAction(entity_id=ADMIN, command="set_mode", params={"mode": "precool"},
                           interval=Interval(start=start, end=start + timedelta(minutes=5))),
                 source={"ticket_id": plant.ticket_id, "proposal_id": "proposal://P-CANCEL"})
    action_id = next(a["action_id"] for a in reversed(runner.trace.actions) if a["source"].get("proposal_id") == "proposal://P-CANCEL")
    assert _applied(plant)[action_id]["stage"] == "scheduled"

    assert _post(plant, f"actions/{action_id}/cancel", {"reason": ""}).status_code == 400  # 必须填原因
    response = _post(plant, f"actions/{action_id}/cancel", {"reason": "现场已处理"})
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["kind"] == "cancelled" and record["operator"] == "u-reviewer" and record["note"] == "现场已处理"
    assert _applied(plant)[action_id]["stage"] == "cancelled"
    assert not any(item.get("action_id") == action_id for item in runner.state.sims["actions"])
    assert _post(plant, f"actions/{action_id}/cancel", {"reason": "再取消一次"}).json() == record  # 幂等：同一条记录
    assert len(_evidence(plant, "action_cancelled")) == 1

    runner.run_until(start + timedelta(minutes=6))  # 走过原本的生效窗口：动作从未生效
    assert not [a for a in active_actions(runner.state, start) if a.params.get("mode") == "precool" and a.interval.start == start]
    assert _post(plant, "actions/A-00001/cancel", {"reason": "已生效的不能取消"}).status_code == 409


def test_revoke_active_action_recovers_next_minute_and_is_idempotent(plant):
    """撤销已生效动作：结束时刻提前到当前分钟，下一分钟设备回到没有这条动作的状态；重复撤销返回同一条记录。"""
    runner = plant.runner
    assert _applied(plant)["A-00003"]["stage"] == "active"  # 预案保护放电，剩 5 分钟
    runner.run_until(runner.now + timedelta(minutes=1))
    assert _battery_mode(plant) == "discharge"

    body = {"reason_kind": "other", "note": "现场要手动调度储能", "confirm_protection": True}
    response = _post(plant, "actions/A-00003/revoke", body)
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["kind"] == "revoked" and record["reason_kind"] == "other" and record["protection"] is True
    assert record["effective_end"] == record["at"]
    runner.run_until(runner.now + timedelta(minutes=1))
    assert _battery_mode(plant) != "discharge"  # 下一分钟恢复
    assert _applied(plant)["A-00003"]["stage"] == "revoked"

    again = _post(plant, "actions/A-00003/revoke", {"reason_kind": "system_wrong", "note": "改口也不重复记账",
                                                    "confirm_protection": True})
    assert again.status_code == 200 and again.json() == record
    assert len(_evidence(plant, "action_revoked")) == 1
    assert not _feedback(plant, plant.ticket_id)  # 重复撤销不会补记 system_wrong


def test_revoke_reason_kind_decides_whether_b32_counts_it(plant):
    """system_wrong 进入 B32 反馈（system_error=真，来源 revoke）；other 只留撤销记录、不进 B32。"""
    other = _post(plant, "actions/A-00001/revoke", {"reason_kind": "other", "note": "现场要求"})
    assert other.status_code == 200 and other.json()["reason_kind"] == "other"
    assert not _feedback(plant, "ticket://TK-0004")

    wrong = _post(plant, "actions/A-00002/revoke", {"reason_kind": "system_wrong", "note": "判断错了"})
    assert wrong.status_code == 200 and wrong.json()["reason_kind"] == "system_wrong"
    rows = _feedback(plant, "ticket://TK-0004")
    assert len(rows) == 1 and rows[0]["system_error"] is True and rows[0]["outcome"] == "rejected"
    assert rows[0]["note"] == "判断错了" and rows[0]["actor"] == "u-reviewer"


def test_revoke_protection_action_needs_note_and_confirmation(plant):
    """保护动作允许撤销，但必须填说明并带 confirm_protection；证据标“撤销保护动作”。"""
    path = "actions/A-00003/revoke"
    assert _post(plant, path, {"reason_kind": "other", "note": "有说明但没确认"}).status_code == 400
    assert _post(plant, path, {"reason_kind": "other", "note": "  ", "confirm_protection": True}).status_code == 400
    assert _applied(plant)["A-00003"]["stage"] == "active"  # 被拒绝后动作没动
    ok = _post(plant, path, {"reason_kind": "other", "note": "确认撤销", "confirm_protection": True})
    assert ok.status_code == 200 and ok.json()["protection"] is True
    assert any("撤销保护动作" in (event.summary or "") for event in _evidence(plant, "action_revoked"))
    assert plant.client.get("/api/v1/actions/A-00003/revoke-preview").status_code == 409  # 已撤销没有剩余窗口


def test_revoke_preview_compares_revoke_and_keep_over_remaining_window(plant):
    """revoke-preview：剩余窗口内撤销/保持两种情况的费用、越界分钟与 T1 峰值之差；不改变任何状态。"""
    before = plant.runner.state.state_version
    response = plant.client.get("/api/v1/actions/A-00001/revoke-preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["protection"] is False and preview["note"] == ""
    assert preview["window"]["start"].startswith("2026-07-01T00:31") and preview["window"]["end"].startswith("2026-07-01T00:51")
    revoke, keep = preview["revoke"], preview["keep"]
    assert preview["cost_delta_cny"] == pytest.approx(revoke["cost_cny"] - keep["cost_cny"], abs=1e-3)
    assert preview["t1_peak_delta_kw"] == pytest.approx(revoke["t1_peak_kw"] - keep["t1_peak_kw"], abs=1e-2)
    assert preview["violation_minutes_delta"] == revoke["violation_minutes"] - keep["violation_minutes"]
    assert preview["cost_delta_cny"] != 0 and preview["t1_peak_delta_kw"] != 0  # 撤销与保持确实不同
    assert plant.runner.state.state_version == before and _applied(plant)["A-00001"]["stage"] == "active"
    protection = plant.client.get("/api/v1/actions/A-00003/revoke-preview").json()
    assert protection["protection"] is True and "二次确认" in protection["note"]
    assert plant.client.get("/api/v1/actions/A-99999/revoke-preview").status_code == 404


def test_revoke_in_the_expiry_minute_loses_to_expiry(plant):
    """撤销和到期在同一分钟：到期先到的动作已是 ended，撤销按冲突拒绝，不改记录。"""
    runner = plant.runner
    runner.run_until(runner.now + timedelta(minutes=5))  # A-00003 原结束时刻 00:36，此刻 00:36
    assert _applied(plant)["A-00003"]["stage"] == "ended"
    response = _post(plant, "actions/A-00003/revoke", {"reason_kind": "other", "note": "晚了一步", "confirm_protection": True})
    assert response.status_code == 409
    assert next(a for a in runner.trace.actions if a["action_id"] == "A-00003").get("intervention") is None


def test_revoke_and_fork_follow_lock_order(plant):
    """撤销与分叉同时发生：先撤销再分叉，子运行继承已撤销的动作；分叉后再用父运行编号撤销按冲突拒绝。"""
    runner = plant.runner
    parent = runner.run_info.run_id
    assert _post(plant, "actions/A-00001/revoke", {"reason_kind": "other", "note": "分叉前撤销"}).status_code == 200
    child = runner.fork(runner.now).run_id
    inherited = next(item for item in runner.state.sims["actions"] if item.get("action_id") == "A-00001")
    assert inherited["action"]["interval"]["end"] == runner.now.isoformat()
    assert _applied(plant)["A-00001"]["run_id"] == child

    late = _post(plant, "actions/A-00002/revoke", {"reason_kind": "other", "note": "用父运行编号", "run_id": parent})
    assert late.status_code == 409 and "分叉" in late.json()["error"]["message"]
    fresh = _post(plant, "actions/A-00002/revoke", {"reason_kind": "other", "note": "在子运行里撤销", "run_id": child})
    assert fresh.status_code == 200 and fresh.json()["run_id"] == child


def test_concurrent_revokes_write_one_record_and_one_feedback(plant):
    """同一动作并发撤销：运行编辑锁串行化，只有一条记录、一次 B32 反馈，所有调用返回同一条记录。"""
    results: list[dict] = []
    barrier = threading.Barrier(4)

    def revoke() -> None:
        barrier.wait()
        response = _post(plant, "actions/A-00001/revoke", {"reason_kind": "system_wrong", "note": "并发撤销"})
        assert response.status_code == 200, response.text
        results.append(response.json())

    threads = [threading.Thread(target=revoke) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and all(item == results[0] for item in results)
    assert len(_evidence(plant, "action_revoked")) == 1
    assert len(_feedback(plant, "ticket://TK-0004")) == 1


def test_revoke_temp_rule_ends_its_actions(plant):
    """临时规则同样处理：生效中的规则按撤销提前结束，规则置 cancelled；重复撤销幂等。"""
    runner = plant.runner
    window = Interval(start=runner.now + timedelta(minutes=1), end=runner.now + timedelta(minutes=19))
    rule = TempRule(rule_id="TR-9001", text="行政楼设到 26 度", effective=window, entity_ids=(ADMIN,),
                    created_at=runner.now, source_event_id="EV-RULE")
    plant.service.store.save_temp_rule(StoredTempRule(rule=rule, run_id=runner.run_info.run_id))
    runner.temp_rules.add(rule)  # 与 on_instruction 的运行内路径一致：登记规则并下发它的设定动作
    runner.apply(SimAction(entity_id=ADMIN, command="set_setpoint", params={"setpoint_c": 26.0}, interval=window),
                 source={"temp_rule": rule.rule_id})
    runner.run_until(runner.now + timedelta(minutes=2))  # 规则生效中
    assert [a for a in active_actions(runner.state, runner.now) if a.params.get("setpoint_c") == 26.0]

    response = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "值班撤销规则"})
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["kind"] == "revoked" and record["action_id"] == "TR-9001"
    runner.run_until(runner.now + timedelta(minutes=1))
    assert not [a for a in active_actions(runner.state, runner.now) if a.params.get("setpoint_c") == 26.0]
    stored = next(r for r in plant.service.store.temp_rules() if r.rule.rule_id == "TR-9001")
    assert stored.status == "cancelled" and stored.rule.effective.end == runner.now - timedelta(minutes=1)
    assert _post(plant, "actions/TR-9001/revoke", {"reason_kind": "system_wrong", "note": "重复"}).json() == record


def _rule_actions(runner):
    return [a for a in active_actions(runner.state, runner.now) if a.params.get("setpoint_c") == 26.0]


def _add_rule(plant, start_min: int = 1, end_min: int = 19):
    runner = plant.runner
    window = Interval(start=runner.now + timedelta(minutes=start_min), end=runner.now + timedelta(minutes=end_min))
    rule = TempRule(rule_id="TR-9001", text="行政楼设到 26 度", effective=window, entity_ids=(ADMIN,),
                    created_at=runner.now, source_event_id="EV-RULE")
    plant.service.store.save_temp_rule(StoredTempRule(rule=rule, run_id=runner.run_info.run_id))
    runner.temp_rules.add(rule)
    runner.trace.temp_rules["TR-9001"] = rule
    runner.db.store.write_records(runner.run_info.run_id, [("temp_rule", "TR-9001", runner.now, rule.model_dump(mode="json"))])
    runner.apply(SimAction(entity_id=ADMIN, command="set_setpoint", params={"setpoint_c": 26.0}, interval=window),
                 source={"temp_rule": "TR-9001"})
    return window


def _exported_rules(plant):
    from plantkg.world.scenario.export import export_day

    day = export_day(plant.runner.db, plant.runner.run_info.run_id, "2026-07-01")
    return [r for r in day.temp_rules if r.source_event_id == "EV-RULE"]


def test_temp_rule_keeps_its_id_across_forks_and_export_has_one_rule(plant):
    """分叉不换规则编号：两级分叉后导出同一指令只有一条规则、动作来源同编号；撤销后导出结束时刻截短；运行链校验通过。"""
    runner = plant.runner
    _add_rule(plant)
    runner.fork(runner.now)
    runner.run_until(runner.now + timedelta(minutes=2))
    child = runner.fork(runner.now).run_id
    stored = [r for r in plant.service.store.temp_rules() if r.rule.source_event_id == "EV-RULE"]
    assert [(r.rule.rule_id, r.run_id) for r in stored] == [("TR-9001", child)]  # 一行，改归最新子运行
    assert {a["source"].get("temp_rule") for a in runner.trace.actions if a["source"].get("temp_rule")} == {"TR-9001"}
    assert {i["source"].get("temp_rule") for i in runner.state.sims["actions"] if i["source"].get("temp_rule")} == {"TR-9001"}
    exported = _exported_rules(plant)
    assert [r.rule_id for r in exported] == ["TR-9001"]
    original_end = exported[0].effective.end
    assert _rule_actions(runner)

    response = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "控制台看到的编号"})
    assert response.status_code == 200, response.text
    assert response.json()["action_id"] == "TR-9001" and response.json()["kind"] == "revoked"
    runner.run_until(runner.now + timedelta(minutes=1))
    assert not _rule_actions(runner)
    after = _exported_rules(plant)
    assert [r.rule_id for r in after] == ["TR-9001"] and after[0].effective.end < original_end
    assert after[0].effective.end.isoformat().startswith("2026-07-01T00:33")
    assert _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "重复"}).json() == response.json()

    # 与 `python -m plantkg.contracts.validate --chain` 同一条校验路径（服务进程占着库文件锁，子进程打不开，这里进程内调用）
    from plantkg.world.scenario.__main__ import day_exports
    from plantkg.world.scenario.export import validate_chain

    assert validate_chain(runner.db, runner.run_info.run_id,
                          evidence_for_day=lambda runs, as_of: day_exports(runner.db, runs, as_of)) == []


def test_rule_cancelled_before_start_is_skipped_by_export(plant):
    """还没开始就被取消的规则：不再出现在导出的 temp_rules 里（TempRule 契约不变）。"""
    runner = plant.runner
    _add_rule(plant, start_min=10, end_min=30)
    assert [r.rule_id for r in _exported_rules(plant)] == ["TR-9001"]
    response = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "还没开始就不要了"})
    assert response.status_code == 200 and response.json()["kind"] == "cancelled"
    assert not _rule_actions(runner)
    assert _exported_rules(plant) == []


def test_revoke_rule_then_fork_then_revoke_again_returns_original_record(plant):
    """撤销后分叉（已撤销的规则留在旧运行里），再撤销同一规则：幂等返回原记录，不是 409。"""
    runner = plant.runner
    _add_rule(plant)
    runner.run_until(runner.now + timedelta(minutes=2))
    first = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "先撤销"})
    assert first.status_code == 200, first.text
    runner.fork(runner.now)
    again = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "分叉后再撤销"})
    assert again.status_code == 200 and again.json() == first.json()


def test_revoke_rule_rereads_when_fork_moves_the_row_midway(plant, monkeypatch):
    """撤销与分叉交错：第一次读到的是分叉前的旧行（运行已不在线），重读后找到子运行，撤销成功而不是误报 409。"""
    runner = plant.runner
    _add_rule(plant)
    runner.run_until(runner.now + timedelta(minutes=2))
    parent = runner.run_info.run_id
    stale = next(r for r in plant.service.store.temp_rules() if r.rule.rule_id == "TR-9001")
    runner.fork(runner.now)  # 分叉后规则行改归子运行
    original = plant.service.override._find_rule
    calls = {"n": 0}

    def first_stale(rule_id):
        calls["n"] += 1
        return stale.model_copy(update={"run_id": parent}) if calls["n"] == 1 else original(rule_id)

    monkeypatch.setattr(plant.service.override, "_find_rule", first_stale)
    response = _post(plant, "actions/TR-9001/revoke", {"reason_kind": "other", "note": "交错"})
    assert response.status_code == 200, response.text
    assert calls["n"] >= 2


def test_revoke_temp_rule_of_finished_run_explains_why(plant):
    """规则所在运行已不在线：409，提示写清原因与运行编号。"""
    other = TempRule(rule_id="TR-9100", text="孤立规则", effective=Interval(
        start=plant.runner.now, end=plant.runner.now + timedelta(minutes=10)), entity_ids=(ADMIN,),
        created_at=plant.runner.now, source_event_id="EV-ORPHAN")
    plant.service.store.save_temp_rule(StoredTempRule(rule=other, run_id="run-gone"))
    response = _post(plant, "actions/TR-9100/revoke", {"reason_kind": "other", "note": "x"})
    assert response.status_code == 409 and "run-gone" in response.json()["error"]["message"]


def test_template_protection_requires_matching_params(plant):
    """暂停时：与预案保护动作同对象同命令、参数一致 → 照常执行；参数不同 → 压成只提议（路径可区分）。"""
    runner, service = plant.runner, plant.service
    battery = runner.world.ids()["battery"]
    compressor = "entity://demo-electronics/air_compressor/AC-2"
    _pause(plant, "保护动作判定")

    def run(ticket: str, power: float):
        binding, at = _open_ticket(plant, event_id=f"EV-{ticket}", ticket_id=f"ticket://{ticket}",
                                   entity_id=compressor, event_type="load_spike")
        action = ProposedAction(entity_id=battery, command="discharge", params={"power_kw": power},
                                interval=Interval(start=at, end=at + timedelta(minutes=5)))
        proposal = ExpertProposal(
            expert="equipment", ticket_id=f"ticket://{ticket}", actions=(action,), cost=ProposalCost(cny=0.0),
            confidence=0.9, diagnosis="保护动作判定", binding=binding,
            timing=AgentTiming(origin_event_at=at, trigger_at=at, effective_at=at + timedelta(minutes=1),
                               requested_at=at, returned_at=at, latency_ms=0))
        router = runner.dispatcher.router
        return service.execute(runner, router.sink.get(router.sink.save(proposal, binding)))

    assert run("TK-PROT-SAME", 400.0).outcome == "executed"  # 与预案模板参数一致：照常执行
    assert [n for n in runner.trace.notes if n.get("kind") == "autonomy_protection_continues"
            and n.get("entity_id") == battery]
    runner.run_until(runner.now + timedelta(minutes=2))  # 动作生效后再撤销
    protected = next(item for item in _applied(plant).values() if item["ticket_id"] == "ticket://TK-PROT-SAME")
    assert protected["protection"] is True  # 模板匹配的执行动作必须按保护动作落账
    denied = _post(plant, f"actions/{protected['action_id']}/revoke", {"reason_kind": "other", "note": "保护动作"})
    assert denied.status_code == 400  # 没有 confirm_protection，撤销保护动作必须拒绝
    assert run("TK-PROT-DIFF", 123.0).outcome == "queued_human"  # 参数不同：压成只提议
    assert _status(plant)["backlog"] == 1


def test_backlog_is_not_doubled_by_fork_copies(paused_plant):
    """分叉后父运行里还留着同一批积压的副本：整厂状态只数在线运行，不翻倍。"""
    plant = paused_plant
    before = _status(plant)["backlog"]
    assert before >= 2
    child = plant.runner.fork(plant.runner.now).run_id
    assert _status(plant)["backlog"] == before
    assert _status(plant, child)["backlog"] == before


def test_backlog_ignores_tasks_without_online_runner(tmp_path):
    """重启后没有绑定运行（live 控制台）：历史暂停任务不再算进整厂 backlog，避免显示无法批准的积压。"""
    plant = _build(tmp_path, pause_first=True, profile="dev")
    try:
        assert _status(plant)["backlog"] >= 2
    finally:
        plant.client.close()
        plant.services.close()
    reopened = build_services(_settings(tmp_path, "dev"))
    client = TestClient(create_app(reopened))
    try:
        status = client.get("/api/v1/autonomy/status")
        assert status.status_code == 200, status.text
        assert status.json()["paused"] is True and status.json()["backlog"] == 0
    finally:
        client.close()
        reopened.close()


def test_pause_and_resume_in_the_same_minute_each_write_evidence(plant):
    """暂停/恢复/再暂停落在同一分钟（场景时钟不动）：证据事件各一条，不因编号重复被吞。"""
    _pause(plant, "第一次")
    _post(plant, "autonomy/resume", {})
    _pause(plant, "第二次")
    assert len(_evidence(plant, "autonomy_paused")) == 2
    assert len(_evidence(plant, "autonomy_resumed")) == 1
