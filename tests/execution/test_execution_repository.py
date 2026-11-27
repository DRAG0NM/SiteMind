"""Synthetic persistence integration, not a complete B16 execution test."""
import sqlite3
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, ExecutionResult, Interval, PlantkgError
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger
from plantkg.world.record.repository import ExecutionRepository
from plantkg.world.replay.clock import ReplayClock
from tests.gate.test_gate_action_integration import env  # noqa: F401
from tests.gate.test_execution_snapshots import _setup, _snapshot
from tests.versions.conftest import REVIEWER

__all__ = ["env"]


def test_atomic_commit_idempotency_and_historical_read(request):
    env = request.getfixturevalue("env")
    service, actions, ctx, plan, writer, _ = _setup(env)
    action = service.propose(plan.plan_id, 0, "synthetic", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "repository-confirm")
    approved = service.executable(plan.plan_id)
    _, snapshot_digest = _snapshot(service, ctx, plan, approved.revision, writer)
    clock = ReplayClock(writer, service.runs)
    repository = ExecutionRepository(writer, clock, service)
    start = plan.valid_interval.start
    slot = Interval(start=start, end=start + timedelta(minutes=15))
    meta = LedgerMeta("execution-test", "execution", ctx.run_id, plan.plan_id, approved.revision,
                      ctx.version_manifest_id, ("test://synthetic",))
    ledger = compute_ledger([SlotInput(slot, pv_available_kw=0, load_kw=10, price_cny_per_kwh=1)], meta, None).ledger
    result = ExecutionResult(run_id=ctx.run_id, plan_id=plan.plan_id, plan_revision=approved.revision,
                             event_seq=1, executed=(), ledger=ledger, idempotency_key="synthetic-key")
    def commit(request_digest="a" * 64):
        return repository.commit(ctx, approved, 0, request_digest, snapshot_digest, result, {"storage": {}}, slot.end)
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_clock BEFORE INSERT ON replay_clock_history WHEN NEW.seq=1 BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        commit()
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM b16_executions").fetchone()[0] == 0
    assert clock.current(ctx)[1] == 0
    writer.submit(lambda con: con.execute("DROP TRIGGER fail_clock"))
    assert commit() == commit() == result
    assert clock.current(ctx)[1] == 1
    with pytest.raises(PlantkgError) as mismatch:
        commit("b" * 64)
    assert mismatch.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(PlantkgError) as hidden:
        repository.read(ctx, 1)
    assert hidden.value.code == ErrorCode.NOT_VISIBLE
    later = ctx.model_copy(update={"as_of": slot.end})
    assert repository.read(later, 1)[0] == result
