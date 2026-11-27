"""P1-3/P2-1：B47 复核与纠错结果闭环到 B32（actor 透传、滑窗收紧、候选占位释放、system_error 透传）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from plantkg.agent.execution_policy import demo
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Interval, ProbeProposal, ProposedAction, Role, RunBinding
from plantkg.core.ports import PendingActionStore, VersionStore
from plantkg.evolution.service import EvolutionService
from plantkg.evolution.store import EvolutionStore
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
REVIEWER = Actor(actor_id="u-reviewer", roles=(Role.reviewer,))
HEADERS = {"x-plantkg-operator-token": "", "x-plantkg-actor": "u-reviewer"}


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
    client = TestClient(create_app(services))
    try:
        yield SimpleNamespace(services=services, runner=runner, service=service, client=client,
                              store=services.registry.resolve(PendingActionStore),
                              ticket_id=runner.dispatcher.router.store.event_ticket("act1-spike"))
    finally:
        client.close()
        services.close()


def _headers(plant) -> dict[str, str]:
    return HEADERS | {"x-plantkg-operator-token": plant.services.version_run.operator.token}


def _probe(plant, round_: int) -> None:
    runner, router = plant.runner, plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    binding = RunBinding(run_id=runner.run_info.run_id, knowledge_version=runner.knowledge_version,
                         ticket_id=ticket.ticket_id, round=ticket.round,
                         state_version=runner.state.state_version, trigger_at=runner.now)
    proposal = ProbeProposal(ticket_id=ticket.ticket_id, round=round_, actions=(ProposedAction(
        entity_id=runner.world.index.eid("zone:admin"), command="set_mode", params={"mode": "precool"},
        interval=Interval(start=runner.now, end=runner.now + timedelta(minutes=2))),),
        window_min=2, hypothesis="复核闭环探针", expectations=("读数有变化",), created_at=runner.now)
    runner.registry.hooks_for("router.probe")[-1](ticket, proposal, binding)


def _review_item(plant, round_: int):
    ticket = plant.runner.dispatcher.router.store.ticket(plant.ticket_id)
    item_id = f"review-{ticket.ticket_id.rsplit('/', 1)[-1]}-{round_}-manual-0"
    return item_id, next(item for item in plant.service.store.queue("review") if item.item_id == item_id)


def _category(plant) -> str:
    router = plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    event = router.store.event(ticket.event_ids[-1])["event"]
    entity_type = None
    for hook in reversed(plant.runner.registry.hooks_for("router.entity_type")):
        entity_type = hook(event.entity_id)
        if entity_type:
            break
    return plant.service.store.category(event.event_type, entity_type)


def test_review_approvals_carry_actor_and_release_pending_on_reject(plant):
    """P1-3：复核结论带 actor 进 B32；累计到阈值生成候选；候选被回测拒绝后释放占位。"""
    category = _category(plant)
    plant.service.store.mark_seen(category, plant.runner.now, "L1")
    for round_ in (1, 2, 3):
        _probe(plant, round_)
        item_id, item = _review_item(plant, round_)
        assert item.status == "pending"
        plant.service.review_outcome(item_id, True, actor=REVIEWER)
        assert _review_item(plant, round_)[1].status == "correct"

    store_b32 = EvolutionStore(plant.services.storage.writer, plant.services.settings.plant_id)
    successes, pending = store_b32.autonomy_state(category)
    assert (successes, pending) == (0, "L3")  # L2（默认 policy）→ L3
    candidate_id = store_b32.autonomy_pending_candidate(category)
    assert candidate_id, "连续 3 次批准应生成升一级候选并记下占位"
    candidate = store_b32.candidate(candidate_id)
    assert candidate.trigger.startswith("反馈累计")
    events = [row for row in store_b32.timeline() if row.get("event") == "feedback_recorded"]
    assert any(row.get("actor") == REVIEWER.actor_id for row in events)  # actor 透传
    feedback_rows = store_b32.feedback_rows(category, 10)
    assert feedback_rows and feedback_rows[0]["actor"] == REVIEWER.actor_id  # P2-7：actor 进 b32_feedback 表

    versions = plant.services.registry.resolve(VersionStore)
    context = Context(plant_id=plant.services.settings.plant_id, industry_pack="manufacturing", actor=REVIEWER,
                      channel="offline", trace_id="b32-tighten-reject", as_of=datetime.now(timezone.utc))
    service = EvolutionService(
        store_b32, versions, plant.services.registry.resolve(PendingActionStore), None,
        plant.services.registry, context,
        evaluator=lambda *_: {"baseline_score": 1.0, "candidate_score": 0.0, "executed": True, "ledger_verified": True})
    report = service.backtest(candidate_id, [{"scenario_id": "tighten-fail", "seed": 1}])
    assert report.verdict == "fail"
    assert store_b32.autonomy_state(category)[1] is None
    assert store_b32.autonomy_pending_candidate(category) is None


def test_non_reviewer_feedback_does_not_advance_pending(plant):
    """P1-3：非复核员上下文生成不了候选时，不清零累计、不占 pending。"""
    category = "load_spike|air_compressor"
    versions = plant.services.registry.resolve(VersionStore)
    store_b32 = EvolutionStore(plant.services.storage.writer, plant.services.settings.plant_id)
    operator = Actor(actor_id="operator", roles=(Role.operator,))
    context = Context(plant_id=plant.services.settings.plant_id, industry_pack="manufacturing", actor=operator,
                      channel="offline", trace_id="b32-operator-feedback", as_of=datetime.now(timezone.utc))
    service = EvolutionService(store_b32, versions, plant.services.registry.resolve(PendingActionStore), None,
                               plant.services.registry, context)
    results = [service.record_feedback(f"ticket-op-{index}",
                                       {"category": category, "outcome": "approved"}) for index in range(3)]
    assert all(result["candidate_id"] is None for result in results)
    successes, pending = store_b32.autonomy_state(category)
    assert pending is None and successes == 2  # 到阈值但生成不了候选：保留累计，不清零
    assert not [row for row in store_b32.timeline() if row.get("event") == "autonomy_candidate"]


def test_reject_correction_from_pending_carries_system_error(plant):
    """P2-1：拒绝从队列 payload、确认从 edits 透传 system_error=False 到 B32（#193）。"""
    router = plant.runner.dispatcher.router
    ticket = router.store.ticket(plant.ticket_id)
    index = {"value": 0}

    def enqueue(system_error: bool):
        index["value"] += 1
        plant.service._queue("correction", ticket, ticket.round + index["value"], None,
                             ProposedAction(entity_id=plant.runner.world.index.eid("zone:admin"),
                                            command="set_mode", params={"mode": "auto"},
                                            interval=Interval(start=plant.runner.now,
                                                              end=plant.runner.now + timedelta(minutes=2))),
                             0, reason="测试纠错队列", ticket_id=ticket.ticket_id,
                             run_id=plant.runner.run_info.run_id, at=plant.runner.now,
                             system_error=system_error)
        return next(action for action in plant.store.list(kind="correction")[0]
                    if action.payload.get("item_id") and action.status == "proposed")

    rejected = enqueue(False)
    response = plant.client.post(f"/api/v1/actions/{rejected.action_id}/reject", json={
        "reason": "不是系统判断有误", "expected_revision": rejected.revision,
        "idempotency_key": "reject-correction-193"}, headers=_headers(plant))
    assert response.status_code == 200, response.text
    confirmed = enqueue(False)
    response = plant.client.post(f"/api/v1/actions/{confirmed.action_id}/confirm", json={
        "reason": "确认纠错", "expected_revision": confirmed.revision,
        "idempotency_key": "confirm-correction-193"}, headers=_headers(plant))
    assert response.status_code == 200, response.text
    category = _category(plant)
    store_b32 = EvolutionStore(plant.services.storage.writer, plant.services.settings.plant_id)
    rows = store_b32.feedback_rows(category, 10)
    assert len(rows) >= 2 and rows[0]["system_error"] is False and rows[1]["system_error"] is False
    assert store_b32.autonomy_pending_candidate(category) is None
