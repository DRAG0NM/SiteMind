"""Generated B05 adapter samples, not formal factory measurements."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.core.contracts import ErrorCode, Interval, PlantkgError
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.record.scenario import ScenarioInputs
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.adapters.test_adapters import hipe_csv, hipe_map, pv_file, pv_map
from tests.versions.conftest import PLANT, REVIEWER, context


def test_b05_scenario_has_private_frozen_provenance(tmp_path, writer, versions):
    source_start = datetime(2017, 10, 23, tzinfo=ZoneInfo("Europe/Berlin"))
    rows = [[(source_start + timedelta(seconds=5*i)).isoformat(), "main", 2] for i in range(181)]
    hipe = HipeAdapter().normalize(hipe_csv(tmp_path, rows), hipe_map(), {})
    pv = PvgisAdapter().normalize(pv_file(tmp_path), pv_map(), {})
    load = next(r for r in hipe.records if r.nature == "configured")
    solar = pv.records[0]
    assert load.value == pytest.approx(50) and solar.value == pytest.approx(24.105)
    assert solar.quality.flag == "estimated" and solar.available_at == solar.interval.start + timedelta(hours=1)
    refs = ManifestReferences()
    refs.bind(versions)
    series = ParquetSeriesStore(tmp_path / "series", writer, PLANT, refs.series)
    artifacts = IntegrationArtifacts(writer, series, PLANT, tmp_path / "artifacts")
    refs.bind_source_maps(artifacts)
    entries = []
    for quantity, bundle in (("active_power", hipe), ("pv_ac_power", pv)):
        ref = series.write_partition(bundle)
        entries.append(dict(quantity=quantity, ref=ref, source_id=bundle.source_id,
                            source_version=bundle.source_version, basis="synthetic adapter test"))
    source_map = artifacts.prepare_series_map(entries)
    versions.register_verifier("series-map:v1:", artifacts.verify_map)
    manifest = versions.prepare({"data_sources": source_map}, "synthetic")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    runs = SqliteRunStore(writer, PLANT, versions)
    clock, public = install(Registry(), writer, runs, series)
    start = load.interval.start
    assert solar.interval == load.interval
    inputs = {"online_source_ids": [hipe.source_id, pv.source_id], "online_record_hashes": [],
              "execution_truth_source_ids": [hipe.source_id, pv.source_id],
              "execution_truth_record_hashes": [online_record_hash(load), online_record_hash(solar)],
              "execution_truth_as_of": solar.available_at.isoformat()}
    run = runs.create_run(context(), "replay_day", Interval(start=start, end=start + timedelta(days=1)), inputs=inputs)
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id,
                  channel="offline", agent_run_id=None)
    scenario = ScenarioInputs(clock, series, runs)
    with pytest.raises(PlantkgError) as not_offline:
        scenario.candidate(ctx.model_copy(update={"channel": "gateway", "agent_run_id": "agent-test"}),
                           "active_power", load.interval, 0)
    assert not_offline.value.code == ErrorCode.FORBIDDEN
    assert scenario.candidate(ctx, "active_power", load.interval, 0).records == (load,)
    result = scenario.candidate(ctx, "pv_ac_power", solar.interval, 0).records[0]
    assert result == solar and result.available_at > result.interval.end
    assert result.transform_steps[0].params["capacity_ratio"] == 500
    assert public.query(ctx, "pv_ac_power", solar.interval).records == ()
    with pytest.raises(PlantkgError) as wrong_seq:
        scenario.candidate(ctx, "pv_ac_power", solar.interval, 1)
    assert wrong_seq.value.code == ErrorCode.STATE_CONFLICT
    other = runs.create_run(ctx, "replay_day", run.clock, inputs=inputs | {"online_record_hashes": [online_record_hash(solar)]})
    with pytest.raises(PlantkgError) as overlap:
        scenario.candidate(ctx.model_copy(update={"run_id": other.run_id}), "pv_ac_power", solar.interval, 0)
    assert overlap.value.code == ErrorCode.PRECONDITION
    clock.advance(ctx, 0, solar.available_at)
    later = ctx.model_copy(update={"as_of": solar.available_at})
    assert public.query(later, "pv_ac_power", solar.interval).records == ()
