"""Constructed policy materials with real B04 stores and real SQLite transactions."""
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import SqliteVersionStore
from plantkg.core.versions import atomic
from plantkg.decision.gate.service import GateService
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from tests.gate.test_policy import complete_materials
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, VIEWER, component, context


@pytest.fixture
def env(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(writer).apply()
    now = [AS_OF]
    versions = SqliteVersionStore(writer, PLANT, clock=lambda: now[0])
    versions.register_verifier("t:", lambda _: "complete")
    manifest = versions.prepare({"parameters": component("t:constructed")}, "constructed test")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    runs = SqliteRunStore(writer, PLANT, versions, clock=lambda: now[0])
    original, materials = complete_materials()
    run = runs.create_run(context(version_manifest_id=manifest.version_manifest_id), "replay_day", original.valid_interval)
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    plan = original.model_copy(update={"revision": 0, "run_id": run.run_id,
                                      "version_manifest_id": manifest.version_manifest_id}, deep=True)
    request = materials.request.model_copy(update={"context": ctx})
    forecasts = tuple(f.model_copy(update={"params": f.params | {"run_id": run.run_id,
                         "version_manifest_id": manifest.version_manifest_id}, "lower": None, "upper": None})
                      for f in materials.forecasts)
    baseline = replace(materials.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}))
    constructed = [replace(materials, request=request, forecasts=forecasts, baseline=baseline, calibrations=())]
    handlers = {}
    actions = SqlitePendingActionStore(writer, PLANT, handlers, ttl_s=600, clock=lambda: now[0])
    service = GateService(writer, PLANT, actions, runs, versions, materials_provider=lambda _p, _m: constructed[0], clock=lambda: now[0])
    handlers["plan_confirmation"] = service
    service.register(plan, ctx, {"fixture": "constructed policy materials"})
    try:
        yield service, actions, ctx, plan, now, writer, constructed
    finally:
        writer.close()


def test_real_b04_confirmation_revisions_and_idempotency(env):
    service, actions, ctx, plan, _, _, _ = env
    decision = service.assess(plan.plan_id, 0, ctx)
    assert decision.outcome == "review_required" and decision.revision == 1
    action = actions.get(decision.pending_action_id)
    assert action.revision == 0 and action.target_revision == 1
    assert service.repository.approval(plan.plan_id) is None
    assert service.propose(plan.plan_id, 1, "门控需要人工确认", ctx).action_id == action.action_id
    with pytest.raises(PlantkgError) as forbidden:
        actions.confirm(action.action_id, VIEWER, "accept", 0, "viewer")
    assert forbidden.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(PlantkgError) as stale:
        actions.confirm(action.action_id, REVIEWER, "accept", 1, "wrong-action-rev")
    assert stale.value.code == ErrorCode.REVISION_CONFLICT
    decided = actions.confirm(action.action_id, REVIEWER, "accept risks", 0, "review-1")
    assert actions.confirm(action.action_id, REVIEWER, "accept risks", 0, "review-1") == decided
    with pytest.raises(PlantkgError) as changed:
        actions.confirm(action.action_id, REVIEWER, "different", 0, "review-1")
    assert changed.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(PlantkgError) as again:
        actions.confirm(action.action_id, REVIEWER, "accept risks", 1, "review-2")
    assert again.value.code == ErrorCode.STATE_CONFLICT
    executable = service.executable(plan.plan_id)
    assert executable.status == "confirmed" and executable.revision == 2
    approval = service.repository.approval(plan.plan_id, 2)
    assert approval.mode == "manual" and approval.action_id == action.action_id and approval.actor == REVIEWER.actor_id
    with pytest.raises(PlantkgError) as old:
        service.executable(plan.plan_id, 1)
    assert old.value.code == ErrorCode.REVISION_CONFLICT


def test_propose_persists_policy_rejection_and_retries(env):
    service, actions, ctx, plan, _, _, supplied = env
    forecast = supplied[0].forecasts[0]
    wrong = forecast.model_copy(update={"params": forecast.params | {"run_id": "wrong"}})
    supplied[0] = replace(supplied[0], forecasts=(wrong, supplied[0].forecasts[1]))
    ctx = ctx.model_copy(update={"idempotency_key": "reject-proposal"})
    result = service.proposal_tool(ctx, plan.plan_id, 0, "request")
    assert result.ok and result.data["status"] == "rejected" and result.data["revision"] == 1
    assert result.data["reasons"]
    assert service.load(plan.plan_id).status == "rejected"
    decision = service.repository.decision(plan.plan_id, 1)
    assert decision.outcome == "rejected"
    assert service.repository.policy_snapshot(plan.plan_id, 1) == decision.thresholds
    assert service.proposal_tool(ctx, plan.plan_id, 0, "request") == result
    assert service.proposal_tool(ctx.model_copy(update={"idempotency_key": "other"}), plan.plan_id, 1, "request").data == result.data
    assert not actions.list(kind="plan_confirmation")[0]
    with pytest.raises(PlantkgError) as changed:
        service.proposal_tool(ctx, plan.plan_id, 0, "different")
    assert changed.value.code == ErrorCode.IDEMPOTENCY_MISMATCH


def test_review_proposal_requires_original_rationale(env):
    service, actions, ctx, plan, _, _, _ = env
    ctx = ctx.model_copy(update={"idempotency_key": "first"})
    action = service.propose(plan.plan_id, 0, "original", ctx)
    assert service.propose(plan.plan_id, 0, "original", ctx).action_id == action.action_id
    other = ctx.model_copy(update={"idempotency_key": "other"})
    assert service.propose(plan.plan_id, 1, "original", other).action_id == action.action_id
    with pytest.raises(PlantkgError) as changed:
        service.propose(plan.plan_id, 1, "different", ctx.model_copy(update={"idempotency_key": "third"}))
    assert changed.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    assert len(actions.list(kind="plan_confirmation")[0]) == 1


def test_failure_after_handler_blocks_retry_and_reject(env):
    """#123：handler 已执行、终态决定落库失败 → 动作停在 proposed，不能重试确认，也不能拒绝。"""
    service, actions, ctx, plan, _, writer, _ = env
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_b04 BEFORE UPDATE ON pending_actions WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(PlantkgError) as failed:
        actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    assert failed.value.code == ErrorCode.INTERNAL
    assert actions.get(action.action_id).status == "proposed"
    assert service.repository.load(plan.plan_id).status == "review_required"
    assert service.repository.approval(plan.plan_id) is None
    with pytest.raises(PlantkgError):
        service.executable(plan.plan_id)
    with pytest.raises(PlantkgError) as rejected:  # 修前：reject 成功，出现“动作 rejected、副作用仍在”
        actions.reject(action.action_id, REVIEWER, "reject", 0, "reject")
    assert rejected.value.code == ErrorCode.STATE_CONFLICT
    writer.submit(lambda con: con.execute("DROP TRIGGER fail_b04"))
    with pytest.raises(PlantkgError) as blocked:  # 执行意图在，重启前不再重放 handler
        actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    assert blocked.value.code == ErrorCode.STATE_CONFLICT


def test_reject_expiry_and_supersede_block_confirmation(env):
    service, actions, ctx, plan, now, _, _ = env
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    actions.reject(action.action_id, REVIEWER, "reject", 0, "reject")
    assert service.load(plan.plan_id).status == "rejected"
    with pytest.raises(PlantkgError):
        service.executable(plan.plan_id)
    assert service.repository.approval(plan.plan_id) is None


def test_expired_action_never_confirms(env):
    service, actions, ctx, plan, now, _, _ = env
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    now[0] += timedelta(seconds=601)
    with pytest.raises(PlantkgError) as exc:
        actions.confirm(action.action_id, REVIEWER, "late", 0, "late")
    assert exc.value.code == ErrorCode.ACTION_EXPIRED
    assert service.load(plan.plan_id).status == "rejected"
    assert service.repository.approval(plan.plan_id) is None


def test_superseded_plan_cannot_confirm(env):
    service, actions, ctx, plan, _, _, _ = env
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    service.repository.transition(plan.plan_id, 1, "superseded")
    with pytest.raises(PlantkgError):
        actions.confirm(action.action_id, REVIEWER, "stale", 0, "stale")
    assert actions.get(action.action_id).status == "proposed"
    assert service.repository.approval(plan.plan_id) is None


def test_failed_commit_then_superseded_cannot_retry(env):
    service, actions, ctx, plan, _, writer, _ = env
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_b04 BEFORE UPDATE ON pending_actions WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(PlantkgError) as failed:
        actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    assert failed.value.code == ErrorCode.INTERNAL
    writer.submit(lambda con: con.execute("DROP TRIGGER fail_b04"))
    service.repository.transition(plan.plan_id, 1, "superseded")
    with pytest.raises(PlantkgError) as blocked:
        actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    assert blocked.value.code == ErrorCode.STATE_CONFLICT
    assert actions.get(action.action_id).status == "proposed"
    assert service.repository.approval(plan.plan_id) is None


def test_proposals_require_matching_authenticated_run(env):
    service, _, ctx, plan, _, _, _ = env
    anonymous = ctx.model_copy(update={"actor": None, "channel": "none", "agent_run_id": None})
    for supplied, expected in ((anonymous, ErrorCode.NO_CTX),
                               (ctx.model_copy(update={"plant_id": "other"}), ErrorCode.CROSS_PLANT),
                               (ctx.model_copy(update={"run_id": "other"}), ErrorCode.PRECONDITION)):
        with pytest.raises(PlantkgError) as exc:
            service.propose(plan.plan_id, 0, "human request", supplied)
        assert exc.value.code == expected
    assert service.repository.approval(plan.plan_id) is None


def test_concurrent_identical_proposals_reuse_action(env, monkeypatch):
    service, actions, ctx, plan, _, _, _ = env
    ctx = ctx.model_copy(update={"idempotency_key": "same-proposal"})
    barrier = threading.Barrier(2)
    original = service.repository.intent

    def concurrent_read(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(service.repository, "intent", concurrent_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.propose, plan.plan_id, 0, "same request", ctx) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0].action_id == results[1].action_id
    assert len(actions.list(kind="plan_confirmation")[0]) == 1


def test_concurrent_rejected_action_synchronization(env, monkeypatch):
    service, actions, ctx, plan, _, _, _ = env
    action = service.propose(plan.plan_id, 0, "request", ctx)
    actions.reject(action.action_id, REVIEWER, "reject", 0, "reject")
    barrier = threading.Barrier(2)
    original = actions.get

    def concurrent_read(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(actions, "get", concurrent_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.load, plan.plan_id) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert [result.status for result in results] == ["rejected", "rejected"]
    assert service.repository.load(plan.plan_id).revision == 2


def test_agent_proposal_records_policy_outcome_instead_of_auto_approval_wording(env):
    service, _, ctx, plan, _, _, supplied = env
    _, complete = complete_materials()
    forecasts = tuple(f.model_copy(update={"lower": (9.0,) * len(f.values), "upper": (11.0,) * len(f.values)})
                      for f in supplied[0].forecasts)
    supplied[0] = replace(supplied[0], forecasts=forecasts, calibrations=complete.calibrations)
    # The same materials pass policy automatically...
    auto = service.assess(plan.plan_id, 0, ctx)
    assert auto.outcome == "auto_confirmed" and auto.policy_outcome == "auto_confirmed"
    # ...but an Agent proposal still needs a human and records the raw policy outcome.
    proposal_plan = plan.model_copy(update={"plan_id": "agent-proposal-plan"})
    service.register(proposal_plan, ctx)
    action = service.propose(proposal_plan.plan_id, 0, "Agent 建议执行", ctx)
    decision = service.repository.decision(proposal_plan.plan_id, 1)
    assert decision.outcome == "review_required" and decision.policy_outcome == "auto_confirmed"
    assert decision.pending_action_id == action.action_id
    assert decision.reasons == ("Agent 提议，等待人工确认",)
    assert service.repository.approval(proposal_plan.plan_id) is None


def test_constructed_calibration_auto_approval_and_missing_approval_fails_closed(env):
    service, _, ctx, plan, _, writer, supplied = env
    # These bounds and calibration records are test constructions, not B14 output.
    _, complete = complete_materials()
    forecasts = tuple(f.model_copy(update={"lower": (9.0,) * len(f.values), "upper": (11.0,) * len(f.values)})
                      for f in supplied[0].forecasts)
    supplied[0] = replace(supplied[0], forecasts=forecasts, calibrations=complete.calibrations)
    decision = service.assess(plan.plan_id, 0, ctx)
    assert decision.outcome == "auto_confirmed" and decision.pending_action_id is None
    assert decision.revision == 1
    assert service.repository.policy_snapshot(plan.plan_id, 1) == decision.thresholds
    approval = service.repository.approval(plan.plan_id, 1)
    assert approval.mode == "auto" and approval.actor is None and approval.action_id is None
    assert service.executable(plan.plan_id, 1).status == "auto_confirmed"
    # Simulate a damaged or incomplete database: status alone cannot grant execution.
    writer.submit(lambda con: con.execute("DELETE FROM b15_approvals WHERE plant_id=? AND plan_id=?",
                                          (PLANT, plan.plan_id)))
    with pytest.raises(PlantkgError) as exc:
        service.executable(plan.plan_id, 1)
    assert exc.value.code == ErrorCode.PRECONDITION


def test_forged_confirmed_plan_without_approval_cannot_execute(env):
    service, _, _, plan, _, writer, _ = env
    forged = plan.model_copy(update={"status": "confirmed"})
    writer.submit(lambda con: con.execute("UPDATE b15_plan_revisions SET record=? WHERE plant_id=? AND plan_id=? AND revision=?",
                                          (forged.model_dump_json(), PLANT, plan.plan_id, plan.revision)))
    assert service.repository.load(plan.plan_id).status == "confirmed"
    with pytest.raises(PlantkgError) as exc:
        service.executable(plan.plan_id)
    assert exc.value.code == ErrorCode.PRECONDITION


def test_transaction_rechecks_manual_approval_and_clock(env):
    from plantkg.world.replay.clock import ReplayClock

    service, actions, ctx, plan, _, writer, _ = env
    clock = ReplayClock(writer, service.runs)
    clock.initialize(ctx)
    def check(revision, run_id=None, manifest_id=None, instant=None):
        return atomic(writer, lambda con: service.require_executable_in_transaction(
            con, plan.plan_id, revision, run_id or ctx.run_id, manifest_id or ctx.version_manifest_id, instant or plan.valid_interval.start))
    with pytest.raises(PlantkgError):
        check(0)
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    with pytest.raises(PlantkgError):
        check(1)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    # Public API performs the existing lazy finalization before the transaction.
    approved = service.executable(plan.plan_id)
    assert check(approved.revision) == approved
    for args in ((2, "other", None, None), (2, None, "other", None),
                 (2, None, None, plan.valid_interval.start + timedelta(minutes=15))):
        with pytest.raises(PlantkgError):
            check(*args)
    service.repository.transition(plan.plan_id, approved.revision, "superseded")
    with pytest.raises(PlantkgError) as stale:
        check(approved.revision)
    assert stale.value.code == ErrorCode.REVISION_CONFLICT


def test_transaction_rejects_missing_approval_and_unconfirmed_action(env):
    from plantkg.world.replay.clock import ReplayClock

    service, actions, ctx, plan, _, writer, _ = env
    ReplayClock(writer, service.runs).initialize(ctx)
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    approved = service.executable(plan.plan_id)
    def check():
        return atomic(writer, lambda con: service.require_executable_in_transaction(
            con, plan.plan_id, approved.revision, ctx.run_id, ctx.version_manifest_id, plan.valid_interval.start))
    assert check() == approved
    writer.submit(lambda con: con.execute("UPDATE pending_actions SET status='proposed' WHERE plant_id=? AND action_id=?", (PLANT, action.action_id)))
    with pytest.raises(PlantkgError):
        check()
    writer.submit(lambda con: con.execute("DELETE FROM b15_approvals WHERE plant_id=? AND plan_id=?", (PLANT, plan.plan_id)))
    with pytest.raises(PlantkgError):
        check()


def test_gate_clock_and_result_share_one_transaction(env):
    from plantkg.world.replay.clock import ReplayClock

    service, actions, ctx, plan, _, writer, _ = env
    clock = ReplayClock(writer, service.runs)
    _, start = clock.initialize(ctx)
    action = service.propose(plan.plan_id, 0, "human request", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "confirm")
    approved = service.executable(plan.plan_id)
    writer.submit(lambda con: con.execute("CREATE TABLE simulated_execution (seq INTEGER PRIMARY KEY)"))
    def commit(fail=False):
        def write(con):
            service.require_executable_in_transaction(con, plan.plan_id, approved.revision,
                                                      ctx.run_id, ctx.version_manifest_id, start)
            con.execute("INSERT INTO simulated_execution VALUES (1)")
            clock.advance_in_transaction(con, ctx, 0, start + timedelta(minutes=15))
            if fail:
                raise RuntimeError("injected after clock")
        atomic(writer, write)
    with pytest.raises(RuntimeError):
        commit(True)
    assert clock.current(ctx)[1] == 0
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM simulated_execution").fetchone()[0] == 0
    commit()
    assert clock.current(ctx)[1] == 1
    with pytest.raises(PlantkgError):
        commit()
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM simulated_execution").fetchone()[0] == 1


def test_transaction_auto_approval_and_expired_cursor(env):
    from plantkg.world.replay.clock import ReplayClock

    service, _, ctx, plan, _, writer, supplied = env
    _, complete = complete_materials()
    forecasts = tuple(f.model_copy(update={"lower": (9.0,) * len(f.values), "upper": (11.0,) * len(f.values)})
                      for f in supplied[0].forecasts)
    supplied[0] = replace(supplied[0], forecasts=forecasts, calibrations=complete.calibrations)
    clock = ReplayClock(writer, service.runs)
    clock.initialize(ctx)
    service.assess(plan.plan_id, 0, ctx)
    approved = service.executable(plan.plan_id)
    def check(instant):
        return atomic(writer, lambda con: service.require_executable_in_transaction(
            con, plan.plan_id, approved.revision, ctx.run_id, ctx.version_manifest_id, instant))
    assert check(plan.valid_interval.start) == approved
    clock.advance(ctx, 0, plan.valid_interval.end)
    with pytest.raises(PlantkgError) as exc:
        check(plan.valid_interval.end)
    assert exc.value.code == ErrorCode.PRECONDITION


def test_real_candidate_manual_gate_and_atomic_commit(env, tmp_path):
    """Synthetic materials only: Parquet, B07, B15, B04 and SQLite together."""
    from plantkg.core.contracts import Interval, SourceBundle, SourceRecord
    from plantkg.core.registry import Registry
    from plantkg.core.versions import ManifestReferences
    from plantkg.storage.series import ParquetSeriesStore
    from plantkg.world.replay.providers import install
    from plantkg.world.replay.series import online_record_hash

    service, actions, old_ctx, original, _, writer, supplied = env
    start = original.valid_interval.start
    slot = Interval(start=start, end=start + timedelta(minutes=15))
    record = SourceRecord(source_id="observed", source_version="v1", content_hash="sha256:" + "a" * 64,
        locator={"line_start": 1}, nature="measured", event_time=start, interval=slot, available_at=slot.end,
        source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=10)
    refs = ManifestReferences()
    refs.bind(service.versions)
    series = ParquetSeriesStore(tmp_path / "series", writer, PLANT, refs.series)
    ref = series.write_partition(SourceBundle(bundle_id="observed", source_id="observed", source_version="v1",
                                               mapping_version="m1", records=(record,)))
    with writer.read_only() as con:
        checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
    service.versions.register_verifier("series:v1:", lambda _: "complete")
    manifest = service.versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum}}, "synthetic")
    service.versions.check_integrity(manifest.version_manifest_id)
    service.versions.activate(old_ctx.version_manifest_id, manifest.version_manifest_id, REVIEWER, "test")
    clock, query = install(Registry(), writer, service.runs, series)
    run = service.runs.create_run(old_ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id}),
        "replay_day", original.valid_interval, inputs={"online_source_ids": ["observed"],
        "online_record_hashes": [online_record_hash(record)]})
    ctx = old_ctx.model_copy(update={"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id})
    plan = original.model_copy(update={"plan_id": "candidate-test-plan", "run_id": run.run_id,
                                     "version_manifest_id": manifest.version_manifest_id}, deep=True)
    materials = supplied[0]
    request = materials.request.model_copy(update={"context": ctx})
    forecasts = tuple(f.model_copy(update={"params": f.params | {"run_id": run.run_id,
                       "version_manifest_id": manifest.version_manifest_id}}) for f in materials.forecasts)
    baseline = replace(materials.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}))
    supplied[0] = replace(materials, request=request, forecasts=forecasts, baseline=baseline)
    service.register(plan, ctx)
    action = service.propose(plan.plan_id, 0, "synthetic review", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "synthetic-confirm")
    approved = service.executable(plan.plan_id)
    assert query.candidate_observed(ctx, "power", slot, 0).records == (record,)
    writer.submit(lambda con: con.execute("CREATE TABLE simulated_result (value REAL)"))
    def commit(con):
        service.require_executable_in_transaction(con, plan.plan_id, approved.revision, run.run_id,
                                                  manifest.version_manifest_id, start)
        con.execute("INSERT INTO simulated_result VALUES (?)", (record.value,))
        return clock.advance_in_transaction(con, ctx, 0, slot.end)
    assert atomic(writer, commit) == (1, slot.end)
    with writer.read_only() as con:
        assert con.execute("SELECT value FROM simulated_result").fetchone() == (10.0,)
