"""Reproduce the B07 visibility/atomic commit prerequisite with real storage."""
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, context


def test_slot_measurement_is_unavailable_until_clock_already_committed(tmp_path, writer, versions):
    end = AS_OF + timedelta(minutes=15)
    interval = Interval(start=AS_OF, end=end)
    record = SourceRecord(source_id="online", source_version="v1", content_hash="sha256:" + "a" * 64,
                          locator={"line_start": 1}, nature="measured", event_time=AS_OF,
                          interval=interval, available_at=end, source_tz="UTC", source_unit="kW",
                          quantity="power", unit="kW", value=10)
    refs = ManifestReferences()
    refs.bind(versions)
    series = ParquetSeriesStore(tmp_path / "series", writer, PLANT, refs.series)
    ref = series.write_partition(SourceBundle(bundle_id="online", source_id="online", source_version="v1",
                                               mapping_version="m1", records=(record,)))
    with writer.read_only() as con:
        checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
    versions.register_verifier("series:v1:", lambda component: "complete")
    manifest = versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum}}, "test")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    runs = SqliteRunStore(writer, PLANT, versions)
    run = runs.create_run(context(), "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)),
                          inputs={"online_source_ids": ["online"], "online_record_hashes": [online_record_hash(record)]})
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    clock, query = install(Registry(), writer, runs, series)
    clock.initialize(ctx)
    assert query.query(ctx, "power", interval).records == ()
    future = ctx.model_copy(update={"as_of": end})
    with pytest.raises(PlantkgError) as error:
        query.query(future, "power", interval)
    assert error.value.code == ErrorCode.NOT_VISIBLE
    assert clock.current(ctx)[1:] == (0, AS_OF)
    clock.advance(ctx, 0, end)
    assert query.query(future, "power", interval).records == (record,)
