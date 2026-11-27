from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from plantkg.core.contracts import Approval, ErrorCode, PlantkgError
from plantkg.decision.gate.repository import PlanRepository
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.testing.fakes import fixture


@pytest.fixture
def repository(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(writer).apply()
    try:
        yield PlanRepository(writer, "demo-electronics")
    finally:
        writer.close()


def test_immutable_history_cas_and_atomic_facts(repository):
    plan = fixture("plan").model_copy(update={"revision": 0, "status": "draft"}, deep=True)
    metadata = {"context": {"run_id": plan.run_id}, "source": {"ids": ["one"]}}
    repository.register(plan, metadata)
    metadata["source"]["ids"].append("mutated")
    plan.objective_terms["tariff.energy_cost"] = 0
    stored = repository.load(plan.plan_id)
    assert stored.objective_terms["tariff.energy_cost"] == 6235.4
    assert repository.metadata(plan.plan_id)["source"]["ids"] == ["one"]
    decision = fixture("gate_decision").model_copy(deep=True)
    next_plan = repository.transition(stored.plan_id, 0, "review_required", decision=decision,
                                      policy_snapshot={"version": "gate-policy-v1"}, action_id="act-0001")
    assert next_plan.revision == 1
    assert repository.action_for(plan.plan_id, 1) == "act-0001"
    assert repository.decision(plan.plan_id, 1) == decision
    assert repository.policy_snapshot(plan.plan_id, 1) == {"version": "gate-policy-v1"}
    with pytest.raises(PlantkgError) as exc:
        repository.transition(plan.plan_id, 0, "superseded")
    assert exc.value.code == ErrorCode.REVISION_CONFLICT
    with pytest.raises(PlantkgError):
        repository.transition(plan.plan_id, 1, "confirmed")
    assert repository.load(plan.plan_id).revision == 1
    approval = Approval(plan_id=plan.plan_id, revision=2, mode="manual", actor="reviewer", action_id="act-0001",
                        approved_at=datetime(2026, 10, 31, tzinfo=timezone.utc), reason="reviewed")
    assert repository.transition(plan.plan_id, 1, "confirmed", approval=approval).revision == 2
    assert repository.approval(plan.plan_id, 2) == approval
    assert repository.load(plan.plan_id, 0).status == "draft"


def test_recoverable_intent_and_idempotency(repository):
    plan = fixture("plan").model_copy(update={"revision": 0, "status": "draft"}, deep=True)
    repository.register(plan, {"run_id": plan.run_id})
    intent = {"action_key": "stable", "payload": {"target_ref": f"{plan.plan_id}@1"}}
    assert repository.reserve_intent(plan.plan_id, 0, intent) == intent
    assert repository.reserve_intent(plan.plan_id, 0, intent) == intent
    with pytest.raises(PlantkgError) as exc:
        repository.reserve_intent(plan.plan_id, 0, {"action_key": "changed"})
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    assert repository.intent(plan.plan_id, 0) == intent
    assert repository.result("handler", "act-1", {"reviewer": "u"}) is None
    repository.save_result("handler", "act-1", {"reviewer": "u"}, {"approved": True})
    assert repository.result("handler", "act-1", {"reviewer": "u"}) == {"approved": True}
    with pytest.raises(PlantkgError) as exc:
        repository.result("handler", "act-1", {"reviewer": "other"})
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH


def test_failed_fact_write_rolls_back_revision_and_survives_restart(tmp_path):
    path = tmp_path / "catalog.db"
    writer = SQLiteWriter(path)
    MigrationRunner(writer).apply()
    repository = PlanRepository(writer, "demo-electronics")
    plan = fixture("plan").model_copy(update={"revision": 0, "status": "draft"}, deep=True)
    repository.register(plan, {"run_id": plan.run_id})
    decision = fixture("gate_decision").model_copy(deep=True)
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_decision BEFORE INSERT ON b15_decisions BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        repository.transition(plan.plan_id, 0, "review_required", decision=decision, action_id="act-0001")
    assert repository.load(plan.plan_id).revision == 0
    assert repository.decision(plan.plan_id) is None
    writer.submit(lambda con: con.execute("DROP TRIGGER fail_decision"))
    repository.transition(plan.plan_id, 0, "review_required", decision=decision, action_id="act-0001")
    writer.close()
    writer = SQLiteWriter(path)
    try:
        MigrationRunner(writer).apply()
        repository = PlanRepository(writer, "demo-electronics")
        assert repository.load(plan.plan_id).revision == 1
        assert repository.action_for(plan.plan_id, 1) == "act-0001"
        assert repository.decision(plan.plan_id) == decision
    finally:
        writer.close()


def test_concurrent_plan_transition_has_one_winner(repository):
    plan = fixture("plan").model_copy(update={"revision": 0, "status": "draft"}, deep=True)
    repository.register(plan, {"run_id": plan.run_id})

    def attempt():
        try:
            return repository.transition(plan.plan_id, 0, "superseded").revision
        except PlantkgError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert outcomes.count(1) == 1
    assert outcomes.count(ErrorCode.REVISION_CONFLICT) == 1
    assert repository.load(plan.plan_id).revision == 1
