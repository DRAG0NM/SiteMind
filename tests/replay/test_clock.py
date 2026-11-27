from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.replay.clock import REPLAY_ADVANCED_HOOK, ReplayClock
from tests.versions.conftest import AS_OF, PLANT, context
from tests.versions.test_versions import publish

@pytest.fixture
def replay(writer, versions):
    manifest = publish(versions, "t:rules@v1", None)
    runs = SqliteRunStore(writer, PLANT, versions)
    run = runs.create_run(context(), "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)), seed=7, inputs={"slice": "v1", "online_rule_ids": ["rule-1"]})
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    clock = ReplayClock(writer, runs, Registry())
    clock.initialize(ctx)
    return clock, ctx, runs


def test_cas_conflict_read_only_and_restart(replay, tmp_path):
    clock, ctx, runs = replay
    observed = []
    clock.registry.register_hook(REPLAY_ADVANCED_HOOK, lambda context, instant: observed.append((context.run_id, instant)))
    later = AS_OF + timedelta(minutes=15)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def advance():
            try:
                return clock.advance(ctx, 0, later).event_seq
            except PlantkgError as exc:
                return exc.code
        results = list(pool.map(lambda _: advance(), range(2)))
    assert sorted(map(str, results)) == sorted(map(str, [1, ErrorCode.STATE_CONFLICT]))
    assert clock.at(ctx, AS_OF).event_seq == 0
    assert len(runs.events(ctx.run_id)) == 1
    assert ReplayClock(clock.writer, runs).current(ctx)[1:] == (1, later)
    assert observed == [(ctx.run_id, later)]
    with pytest.raises(PlantkgError):
        clock.advance(ctx, 1, AS_OF)
    with pytest.raises(PlantkgError):
        clock.at(ctx, later + timedelta(minutes=15))


def test_fork_atomic_and_independent(replay, monkeypatch):
    clock, ctx, runs = replay
    clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))
    child = clock.fork_run_at(ctx, AS_OF)
    child_ctx = ctx.model_copy(update={"run_id": child.run_id})
    assert child.forked_from == ctx.run_id
    assert runs.run_inputs(child.run_id)["seed"] == 7
    assert runs.run_inputs(child.run_id)["inputs"] == {"slice": "v1", "online_rule_ids": ["rule-1"]}
    clock.advance(child_ctx, 0, AS_OF + timedelta(minutes=15))
    assert clock.current(ctx)[1] == 1
    assert clock.current(child_ctx)[1] == 1
    before = len(runs.list_runs()[0])
    original = runs.create_run
    def fail(*args, **kwargs):
        saved = kwargs["transaction_hook"]
        def hook(con, child):
            saved(con, child)
            raise RuntimeError("failure")
        kwargs["transaction_hook"] = hook
        return original(*args, **kwargs)
    monkeypatch.setattr(runs, "create_run", fail)
    with pytest.raises(RuntimeError):
        clock.fork_run_at(ctx, AS_OF)
    assert len(runs.list_runs()[0]) == before


def test_invalid_context_and_ended_run_are_rejected(replay):
    clock, ctx, runs = replay
    for bad in (ctx.model_copy(update={"version_manifest_id": "wrong"}),
                ctx.model_copy(update={"as_of": AS_OF + timedelta(minutes=15)}),
                ctx.model_copy(update={"plant_id": "other"})):
        with pytest.raises(PlantkgError):
            clock.at(bad, AS_OF)
    with pytest.raises(PlantkgError):
        clock.advance(ctx, 0, AS_OF + timedelta(minutes=1))
    runs.append_event(ctx.run_id, "failed", ctx.actor, to_status="failed")
    with pytest.raises(PlantkgError) as exc:
        clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))
    assert exc.value.code == ErrorCode.STATE_CONFLICT


def test_visibility_scope_rejects_untrusted_cutoff_and_filters_rules(replay):
    from plantkg.world.replay.scope import VisibilityContext
    from tests.replay.test_visibility import make_rule
    clock, ctx, _ = replay
    scope = VisibilityContext(clock, ctx)
    rule = make_rule(available_at=AS_OF, effective_from=AS_OF + timedelta(hours=1), effective_to=AS_OF + timedelta(hours=2))
    assert scope.filter([rule], AS_OF) == ()
    assert scope.filter_rules([rule], AS_OF) == ()
    assert scope.filter_rules([rule], AS_OF + timedelta(hours=1)) == (rule,)
    assert scope.filter_rules([rule], AS_OF + timedelta(hours=2)) == ()
    with pytest.raises(PlantkgError):
        VisibilityContext(clock, ctx.model_copy(update={"as_of": AS_OF + timedelta(minutes=15)}))


def test_documents_require_allowlist_and_knowledge_cutoff(replay):
    from plantkg.testing.fakes import fixture
    from plantkg.world.replay.documents import visible_document_paths
    clock, ctx, runs = replay
    asset = fixture("asset_version_policy")
    run = runs.create_run(ctx, "replay_day", runs.get_run(ctx.run_id).clock,
                          inputs={"online_document_ids": [asset.asset_version_id]})
    child_ctx = ctx.model_copy(update={"run_id": run.run_id})
    clock.initialize(child_ctx)
    path = {asset.asset_version_id: "indexed/online.pdf"}
    known = asset.model_copy(update={"available_at": AS_OF})
    future = asset.model_copy(update={"available_at": AS_OF + timedelta(seconds=1)})
    assert visible_document_paths(clock, runs, child_ctx, [known], path) == ("indexed/online.pdf",)
    assert visible_document_paths(clock, runs, child_ctx, [future], path) == ()
    with pytest.raises(PlantkgError):
        visible_document_paths(clock, runs, ctx, [known], path)


@pytest.mark.parametrize("days", [1, 7])
def test_every_quarter_hour_can_advance_in_mapped_utc(writer, versions, days):
    manifest = publish(versions, "t:rules@v1", None)
    runs = SqliteRunStore(writer, PLANT, versions)
    run = runs.create_run(context(), "replay_day" if days == 1 else "replay_week",
                          Interval(start=AS_OF, end=AS_OF + timedelta(days=days)))
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    clock = ReplayClock(writer, runs)
    clock.initialize(ctx)
    for seq in range(days * 96):
        snapshot = clock.advance(ctx, seq, AS_OF + timedelta(minutes=15 * (seq + 1)))
        assert snapshot.event_seq == seq + 1
    assert clock.current(ctx)[1:] == (days * 96, run.clock.end)


def test_committed_clock_survives_writer_reopen(tmp_path):
    from plantkg.core.versions import SqliteVersionStore
    path = tmp_path / "restart.db"
    writer = SQLiteWriter(path)
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, PLANT)
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        runs = SqliteRunStore(writer, PLANT, versions)
        run = runs.create_run(context(), "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
        ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
        clock = ReplayClock(writer, runs)
        clock.initialize(ctx)
        clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))
    finally:
        writer.close()
    writer = SQLiteWriter(path)
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, PLANT)
        clock = ReplayClock(writer, SqliteRunStore(writer, PLANT, versions))
        assert clock.current(ctx)[1:] == (1, AS_OF + timedelta(minutes=15))
    finally:
        writer.close()
