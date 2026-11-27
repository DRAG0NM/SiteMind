"""Synthetic test-only observations against real SQLite and Parquet."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences, atomic
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, context


@pytest.fixture
def bridge(tmp_path, writer, versions):
    refs = ManifestReferences()
    refs.bind(versions)
    series = ParquetSeriesStore(tmp_path / "series", writer, PLANT, refs.series)
    records = tuple(SourceRecord(source_id="online", source_version="v1", content_hash="sha256:" + "a" * 64,
        locator={"line_start": i + 1}, nature="measured", event_time=AS_OF + timedelta(minutes=15*i),
        interval=Interval(start=AS_OF + timedelta(minutes=15*i), end=AS_OF + timedelta(minutes=15*(i+1))),
        available_at=AS_OF + timedelta(minutes=15*(i+1)), source_tz="UTC", source_unit="kW",
        quantity="power", unit="kW", value=float(i)) for i in range(96))
    late = records[0].model_copy(update={"quantity": "late", "available_at": AS_OF + timedelta(minutes=16)})
    missing = records[0].model_copy(update={"quantity": "missing", "value": None,
                                            "quality": records[0].quality.model_copy(update={"flag": "missing"})})
    secret = records[0].model_copy(update={"quantity": "truth", "value": 999.0})
    all_records = records + (late, missing, secret)
    ref = series.write_partition(SourceBundle(bundle_id="online", source_id="online", source_version="v1", mapping_version="m1", records=all_records))
    with writer.read_only() as con:
        checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
    versions.register_verifier("series:v1:", lambda _: "complete")
    manifest = versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum}}, "synthetic")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    runs = SqliteRunStore(writer, PLANT, versions)
    clock, query = install(Registry(), writer, runs, series)
    run = runs.create_run(context(), "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)),
        inputs={"online_source_ids": ["online"], "online_record_hashes": [online_record_hash(r) for r in all_records[:-1]]})
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    return clock, query, ctx, writer, records


def test_candidate_day_and_public_visibility(bridge):
    clock, query, ctx, writer, records = bridge
    for seq, record in enumerate(records):
        window = record.interval
        assert query.query(ctx, "power", window).records == ()
        assert query.candidate_observed(ctx, "power", window, seq).records == (record,)
        atomic(writer, lambda con: clock.advance_in_transaction(con, ctx, seq, window.end))
    assert clock.current(ctx)[1:] == (96, AS_OF + timedelta(days=1))


def test_candidate_fails_closed(bridge):
    clock, query, ctx, _, records = bridge
    window = records[0].interval
    for quantity in ("late", "missing", "truth", "absent"):
        with pytest.raises(PlantkgError) as exc:
            query.candidate_observed(ctx, quantity, window, 0)
        assert exc.value.code == ErrorCode.NOT_VISIBLE
    for bad_context in (ctx.model_copy(update={"plant_id": "other"}),
                        ctx.model_copy(update={"version_manifest_id": "other"}),
                        ctx.model_copy(update={"run_id": "other"})):
        with pytest.raises(PlantkgError):
            query.candidate_observed(bad_context, "power", window, 0)
    with pytest.raises(PlantkgError):
        query.candidate_observed(ctx, "power", window, 1)
    with pytest.raises(PlantkgError):
        query.candidate_observed(ctx, "power", Interval(start=window.start, end=window.end + timedelta(minutes=15)), 0)
    with pytest.raises(PlantkgError):
        query.query(ctx.model_copy(update={"as_of": window.end}), "power", window)
    run = clock.runs.get_run(ctx.run_id)
    other = clock.runs.create_run(ctx, "replay_day", run.clock, inputs={"online_source_ids": ["other"],
                                  "online_record_hashes": [online_record_hash(records[0])]})
    with pytest.raises(PlantkgError) as wrong_source:
        query.candidate_observed(ctx.model_copy(update={"run_id": other.run_id}), "power", window, 0)
    assert wrong_source.value.code == ErrorCode.NOT_VISIBLE
    assert clock.current(ctx)[1] == 0


def test_atomic_rollback_cas_and_competition(bridge):
    clock, _, ctx, writer, records = bridge
    writer.submit(lambda con: con.execute("CREATE TABLE simulated_execution (seq INTEGER PRIMARY KEY)"))
    end = records[0].interval.end
    def attempt(fail_at=None):
        def write(con):
            con.execute("INSERT INTO simulated_execution VALUES (1)")
            if fail_at == "before":
                raise RuntimeError("before clock")
            clock.advance_in_transaction(con, ctx, 0, end)
            if fail_at == "after":
                raise RuntimeError("after clock")
        return atomic(writer, write)
    for point in ("before", "after"):
        with pytest.raises(RuntimeError):
            attempt(point)
        with writer.read_only() as con:
            assert con.execute("SELECT count(*) FROM simulated_execution").fetchone()[0] == 0
        assert clock.current(ctx)[1] == 0
    def race():
        try:
            def write(con):
                clock.advance_in_transaction(con, ctx, 0, end)
                con.execute("INSERT INTO simulated_execution VALUES (1)")
            atomic(writer, write)
            return "ok"
        except PlantkgError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(lambda _: race(), range(2)))) == sorted(map(str, ["ok", ErrorCode.STATE_CONFLICT]))
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM simulated_execution").fetchone()[0] == 1
    assert clock.current(ctx)[1] == 1
    from plantkg.storage.sqlite import SQLiteWriter
    from plantkg.core.versions import SqliteVersionStore
    from plantkg.world.replay.clock import ReplayClock
    path = writer.path
    writer.close()
    reopened = SQLiteWriter(path)
    try:
        restored = ReplayClock(reopened, SqliteRunStore(reopened, PLANT, SqliteVersionStore(reopened, PLANT)))
        assert restored.current(ctx)[1:] == (1, end)
        with reopened.read_only() as con:
            assert con.execute("SELECT count(*) FROM simulated_execution").fetchone()[0] == 1
    finally:
        reopened.close()
