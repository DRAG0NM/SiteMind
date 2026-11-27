from datetime import timedelta

import pytest

from plantkg.adapters.baseload import selected_device_baseload
from plantkg.core.contracts import Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences, SqliteVersionStore
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.series import ParquetSeriesStore
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.replay.input_assembly import prepare_online_inputs
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, context


def services(path):
    writer = SQLiteWriter(path / 'catalog.db')
    MigrationRunner(writer).apply()
    versions = SqliteVersionStore(writer, PLANT)
    refs = ManifestReferences()
    refs.bind(versions)
    series = ParquetSeriesStore(path / 'series', writer, PLANT, refs.series)
    artifacts = IntegrationArtifacts(writer, series, PLANT, path / 'integration_artifacts')
    refs.bind_source_maps(artifacts)
    versions.register_verifier('series-map:v1:', artifacts.verify_map)
    versions.register_verifier('parameters:v1:', artifacts.verify_parameters)
    runs = SqliteRunStore(writer, PLANT, versions)
    clock, query = install(Registry(), writer, runs, series)
    return writer, versions, series, artifacts, runs, clock, query


def meter(meter_name, value):
    records = []
    for n in range(3):
        start = AS_OF - timedelta(minutes=45 - 15*n)
        records.append(SourceRecord(source_id='hipe', source_version='hipe-v1', content_hash='sha256:'+'a'*64,
            locator={'meter': meter_name, 'line_start': n+1}, nature='configured', event_time=start,
            interval=Interval(start=start, end=start+timedelta(minutes=15)), available_at=start+timedelta(minutes=15),
            source_tz='UTC', source_unit='kW', quantity='active_power', unit='kW', value=value,
            transform_steps=({'op':'scale', 'version':'1', 'params':{'factor':25}},)))
    return SourceBundle(bundle_id=meter_name, source_id='hipe', source_version='hipe-v1', mapping_version='m1', records=tuple(records))


def pv_bundle():
    recs = []
    for n in range(4):
        start = AS_OF - timedelta(minutes=45 - 15*n)
        recs.append(SourceRecord(source_id='pvgis', source_version='pv-v1', content_hash='sha256:'+'b'*64,
            locator={'line_start':n+1}, nature='derived', event_time=start,
            interval=Interval(start=start,end=start+timedelta(minutes=15)),
            available_at=start+timedelta(minutes=15), source_tz='UTC', source_unit='W',
            quantity='pv_ac_power', unit='kW', value=4+n))
    return SourceBundle(bundle_id='pv', source_id='pvgis', source_version='pv-v1', mapping_version='m1', records=tuple(recs))


def test_real_dual_source_replay_and_frozen_parameters_restart(tmp_path):
    writer, versions, series, artifacts, runs, clock, query = services(tmp_path)
    source_map, inputs, baseline = prepare_online_inputs(series, artifacts, meter('main', 10), [meter('device', 3)],
        ['device'], pv_bundle(), selected_device_baseload, approved_baseline=lambda b: b.records[:2],
        approved_pv=lambda b: b.records[:3], as_of=AS_OF, basis='frozen selection v1')
    params = artifacts.prepare_parameters(pv_capacity_kw=100, model_version='persistence-1', parameter_version='p1', configuration_ref='config:v1')
    manifest = versions.prepare({'data_sources': source_map, 'parameters': params}, 'test')
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, 'test')
    run = runs.create_run(context(), 'replay_day', Interval(start=AS_OF, end=AS_OF+timedelta(days=1)), inputs=inputs)
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    window = Interval(start=AS_OF-timedelta(hours=1), end=AS_OF+timedelta(hours=1))
    first = query.query(ctx, 'baseline_load', window, page_size=1)
    second = query.query(ctx, 'baseline_load', window, first.next_cursor, page_size=1)
    assert [r.value for r in first.records+second.records] == [7, 7]
    assert second.next_cursor is None and first.source_id == 'hipe' and first.source_version == 'hipe-v1'
    assert first.records[0].interval.end-first.records[0].interval.start == timedelta(minutes=15)
    pv = query.query(ctx, 'pv_ac_power', window)
    assert [r.value for r in pv.records] == [4, 5, 6]
    assert pv.source_id == 'pvgis' and pv.source_version == 'pv-v1'
    assert pv.records[-1].available_at == AS_OF
    clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))
    child = clock.fork_run_at(ctx.model_copy(update={'as_of': AS_OF + timedelta(minutes=15)}), AS_OF + timedelta(minutes=15))
    assert runs.run_inputs(child.run_id)['inputs'] == inputs
    assert artifacts.parameters_for_run(ctx, versions, runs)['pv_correction_k'] == 1
    new_params = artifacts.prepare_parameters(pv_capacity_kw=100, model_version='persistence-1', parameter_version='p2', configuration_ref='config:v2', pv_correction_k=1.2)
    new_pv = pv_bundle().model_copy(update={'bundle_id':'pv-new', 'records':tuple(
        r.model_copy(update={'value': r.value + 10}) for r in pv_bundle().records)})
    new_ref = series.write_partition(new_pv)
    baseline_ref = artifacts.read_series_map(source_map)['baseline_load']
    new_map = artifacts.prepare_series_map([
        dict(quantity='baseline_load', ref=baseline_ref, source_id='hipe', source_version='hipe-v1', basis='selection v1'),
        dict(quantity='pv_ac_power', ref=new_ref, source_id='pvgis', source_version='pv-v1', basis='pv revision')])
    assert new_map.ref != source_map.ref
    new_manifest = versions.prepare({'data_sources':new_map, 'parameters':new_params}, 'new')
    versions.check_integrity(new_manifest.version_manifest_id)
    versions.activate(manifest.version_manifest_id, new_manifest.version_manifest_id, REVIEWER, 'switch')
    assert artifacts.parameters_for_run(ctx, versions, runs)['pv_correction_k'] == 1
    assert query.query(ctx, 'baseline_load', window).records == first.records+second.records
    assert query.query(ctx, 'pv_ac_power', window).records == pv.records
    with pytest.raises(PlantkgError): query.query(ctx, 'unknown', window)
    with pytest.raises(PlantkgError): query.query(ctx.model_copy(update={'plant_id':'other'}), 'baseline_load', window)
    with pytest.raises(PlantkgError): query.query(ctx.model_copy(update={'version_manifest_id':None}), 'baseline_load', window)
    future_inputs = dict(inputs, online_record_hashes=inputs['online_record_hashes'] + [online_record_hash(pv_bundle().records[-1])])
    future_run = runs.create_run(ctx, 'replay_day', run.clock, inputs=future_inputs)
    assert len(query.query(ctx.model_copy(update={'run_id':future_run.run_id}), 'pv_ac_power', window).records) == 3
    no_allow = runs.create_run(ctx, 'replay_day', run.clock)
    with pytest.raises(PlantkgError): query.query(ctx.model_copy(update={'run_id':no_allow.run_id}), 'baseline_load', window)
    writer.close()
    writer, versions, series, artifacts, runs, clock, query = services(tmp_path)
    try:
        assert artifacts.parameters_for_run(ctx, versions, runs)['pv_correction_k'] == 1
        assert query.query(ctx, 'pv_ac_power', window).records == pv.records
        map_path = artifacts.path / ('series-map-' + source_map.ref.split(':')[-1] + '.json')
        original = map_path.read_bytes()
        map_path.write_bytes(b'corrupt')
        with pytest.raises(PlantkgError): query.query(ctx, 'baseline_load', window)
        map_path.write_bytes(original)
        param_path = artifacts.path / ('parameters-' + params.ref.split(':')[-1] + '.json')
        param_path.unlink()
        with pytest.raises(PlantkgError): artifacts.parameters_for_run(ctx, versions, runs)
        ref = artifacts.read_series_map(source_map)['baseline_load']
        (series.path / (ref.split(':')[-1] + '.parquet')).write_bytes(b'corrupt')
        with pytest.raises(PlantkgError): query.query(ctx, 'baseline_load', window)
    finally:
        writer.close()


@pytest.mark.parametrize('key,value', [('pv_correction_k',True), ('pv_correction_k',-1), ('pv_correction_k',float('nan')), ('pv_correction_k',float('inf')), ('pv_capacity_kw',0), ('pv_capacity_kw',False)])
def test_invalid_frozen_parameters(tmp_path, key, value):
    writer, _, _, artifacts, _, _, _ = services(tmp_path)
    try:
        kwargs = dict(pv_capacity_kw=100, pv_correction_k=1, model_version='m', parameter_version='v', configuration_ref='c')
        kwargs[key] = value
        with pytest.raises(PlantkgError): artifacts.prepare_parameters(**kwargs)
    finally:
        writer.close()



def test_assembly_rejects_invalid_selection_and_future_approval(tmp_path):
    writer, _, series, artifacts, _, _, _ = services(tmp_path)
    try:
        def prepare(total, devices, names, pv_approval=lambda b: b.records[:3]):
            return prepare_online_inputs(series, artifacts, total, devices, names, pv_bundle(), selected_device_baseload,
                approved_baseline=lambda b: b.records[:2], approved_pv=pv_approval, as_of=AS_OF, basis='test')
        with pytest.raises(PlantkgError): prepare(meter('main', 10), [meter('device', 3)], ['device', 'device'])
        with pytest.raises(PlantkgError): prepare(meter('main', 10), [meter('device', 3)], ['other'])
        with pytest.raises(PlantkgError): prepare(meter('main', 10), [meter('device', 3)], ['device'], lambda b: b.records)
        with pytest.raises(ValueError): prepare(meter('main', 2), [meter('device', 3)], ['device'])
        missing = meter('device', 3)
        record = missing.records[0].model_copy(update={'value':None, 'quality':missing.records[0].quality.model_copy(update={'flag':'missing'})})
        missing = missing.model_copy(update={'records': (record,) + missing.records[1:]})
        _, _, baseline = prepare(meter('main', 10), [missing], ['device'])
        assert baseline.records[0].value is None and baseline.records[0].quality.flag == 'missing'
    finally:
        writer.close()


def test_artifact_registration_and_manifest_validation(tmp_path):
    writer, versions, series, artifacts, runs, _, _ = services(tmp_path)
    try:
        params = artifacts.prepare_parameters(pv_capacity_kw=100, model_version='m', parameter_version='v', configuration_ref='cfg')
        assert artifacts.prepare_parameters(pv_capacity_kw=100, model_version='m', parameter_version='v', configuration_ref='cfg') == params
        manifest = versions.prepare({'parameters':params}, 'test')
        fake_ctx = context(run_id='missing', version_manifest_id=manifest.version_manifest_id)
        with pytest.raises(PlantkgError): artifacts.parameters_for_run(fake_ctx, versions, runs)
        versions.check_integrity(manifest.version_manifest_id)
        run = runs.create_run(context(version_manifest_id=manifest.version_manifest_id), 'replay_day', Interval(start=AS_OF,end=AS_OF+timedelta(hours=1)))
        ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
        with pytest.raises(PlantkgError): artifacts.parameters_for_run(ctx.model_copy(update={'plant_id':'other'}), versions, runs)
        with writer.read_only() as con:
            assert con.execute('SELECT count(*) FROM storage_artifacts WHERE reference=?', (params.ref,)).fetchone()[0] == 1
        writer.submit(lambda con: con.execute('DELETE FROM storage_artifacts WHERE reference=?', (params.ref,)))
        with pytest.raises(PlantkgError): artifacts.parameters_for_run(ctx, versions, runs)
        with pytest.raises(PlantkgError): versions.activate(None, manifest.version_manifest_id, REVIEWER, 'test')
    finally:
        writer.close()



def test_source_map_rejects_duplicate_unregistered_and_wrong_identity(tmp_path):
    writer, _, series, artifacts, _, _, _ = services(tmp_path)
    try:
        pv = pv_bundle()
        ref = series.write_partition(pv)
        entry = dict(quantity='pv_ac_power', ref=ref, source_id=pv.source_id, source_version=pv.source_version, basis='test')
        component = artifacts.prepare_series_map([entry])
        assert artifacts.prepare_series_map([entry]) == component
        with pytest.raises(PlantkgError): artifacts.prepare_series_map([entry, entry])
        with pytest.raises(PlantkgError): artifacts.prepare_series_map([entry | {'source_id':'other'}])
        with pytest.raises(PlantkgError): artifacts.prepare_series_map([entry | {'quantity':'baseline_load'}])
        with pytest.raises(PlantkgError): artifacts.prepare_series_map([entry | {'ref':'series:v1:' + '0'*64}])
    finally:
        writer.close()


def test_manifest_record_tampering_is_rejected(tmp_path):
    import json
    writer, versions, _, artifacts, runs, _, _ = services(tmp_path)
    try:
        params = artifacts.prepare_parameters(pv_capacity_kw=100, model_version='m', parameter_version='v', configuration_ref='cfg')
        manifest = versions.prepare({'parameters':params}, 'test')
        versions.check_integrity(manifest.version_manifest_id)
        versions.activate(None, manifest.version_manifest_id, REVIEWER, 'test')
        run = runs.create_run(context(), 'replay_day', Interval(start=AS_OF,end=AS_OF+timedelta(hours=1)))
        ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
        def tamper(con):
            row = con.execute('SELECT record FROM version_manifests WHERE manifest_id=?', (manifest.version_manifest_id,)).fetchone()
            record = json.loads(row[0])
            record['components']['parameters']['ref'] = 'parameters:v1:' + '0'*64
            con.execute('UPDATE version_manifests SET record=? WHERE manifest_id=?', (json.dumps(record), manifest.version_manifest_id))
        writer.submit(tamper)
        with pytest.raises(PlantkgError): artifacts.parameters_for_run(ctx, versions, runs)
        with pytest.raises(PlantkgError): versions.active()
    finally:
        writer.close()


def test_real_b05_hipe_and_pvgis_to_b07(tmp_path):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from plantkg.adapters import HipeAdapter, PvgisAdapter
    from tests.adapters.test_adapters import hipe_csv, hipe_map, pv_file, pv_map

    start = datetime(2017, 10, 23, tzinfo=ZoneInfo('Europe/Berlin'))
    rows = lambda name, value: [[(start+timedelta(seconds=5*i)).isoformat(), name, value] for i in range(181)]
    total = HipeAdapter().normalize(hipe_csv(tmp_path, rows('MainTerminal', 2)), hipe_map(), {})
    device = HipeAdapter().normalize(hipe_csv(tmp_path, rows('VacuumPump1', 1)), hipe_map(), {})
    pv = PvgisAdapter().normalize(pv_file(tmp_path), pv_map(), {})
    cutoff = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    writer, versions, series, artifacts, runs, _, query = services(tmp_path)
    try:
        source_map, inputs, baseline = prepare_online_inputs(series, artifacts, total, [device], ['VacuumPump1'], pv,
            selected_device_baseload, approved_baseline=lambda b: b.records[:4], approved_pv=lambda b: b.records[:4],
            as_of=cutoff, basis='B05 fixture selected meter')
        manifest = versions.prepare({'data_sources':source_map}, 'B05 test')
        versions.check_integrity(manifest.version_manifest_id)
        ctx = context(as_of=cutoff, version_manifest_id=manifest.version_manifest_id)
        run = runs.create_run(ctx, 'replay_day', Interval(start=cutoff,end=cutoff+timedelta(days=1)), inputs=inputs)
        ctx = ctx.model_copy(update={'run_id':run.run_id})
        window = Interval(start=cutoff-timedelta(days=1),end=cutoff+timedelta(hours=1))
        load = query.query(ctx, 'baseline_load', window)
        solar = query.query(ctx, 'pv_ac_power', window)
        assert load.records == baseline.records[:4] and load.records[0].value == pytest.approx(25)
        assert solar.records == pv.records[:4] and all(r.value == pytest.approx(24.105) for r in solar.records)
        assert load.source_id == total.source_id and solar.source_id == pv.source_id and load.source_id != solar.source_id
        assert solar.records[0].transform_steps[0].params['capacity_ratio'] == 500
        assert all(r.available_at <= cutoff for r in load.records + solar.records)
    finally:
        writer.close()
