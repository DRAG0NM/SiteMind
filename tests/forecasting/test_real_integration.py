from datetime import datetime, timedelta, timezone
import json
import math
import shutil

import pytest

from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.adapters.baseload import selected_device_baseload
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Interval, PlantkgError
from plantkg.core.ports import StateProvider
from plantkg.decision.forecasting import ForecastService
from plantkg.decision.planner import SharedPlanner
from plantkg.packs.pv import PvConfig, PvPack
from plantkg.testing.fakes import fixture
from plantkg.world.replay.input_assembly import prepare_online_inputs
from tests.adapters.test_adapters import hipe_csv, hipe_map, pv_file, pv_map
from tests.versions.conftest import REVIEWER, context

UTC = timezone.utc
CUTOFF = datetime(2026, 7, 1, 1, tzinfo=UTC)
HORIZON = Interval(start=datetime(2026, 7, 1, 16, tzinfo=UTC), end=datetime(2026, 7, 1, 17, tzinfo=UTC))


def meters(path):
    from zoneinfo import ZoneInfo
    start = datetime(2017, 10, 23, tzinfo=ZoneInfo('Europe/Berlin'))
    def bundle(name, value):
        rows = [[(start + timedelta(seconds=5*i)).isoformat(), name, value] for i in range(721)]
        return HipeAdapter().normalize(hipe_csv(path, rows), hipe_map(), {})
    return bundle('main', 2), bundle('process', 0.4)


def settings(path):
    return load_settings(profile='dev', overrides={'storage.enabled':True,
        'storage.sqlite':str(path/'catalog.db'), 'storage.graph':str(path/'graph'), 'storage.series':str(path/'series')})


def test_b05_b03_b04_b07_b14_persistent_store_and_real_b09(tmp_path):
    cfg = settings(tmp_path)
    services = build_services(cfg)
    owner = services.version_run
    art = owner.integration_artifacts
    ctx = context(plant_id=cfg.plant_id, as_of=CUTOFF)
    pv = PvgisAdapter().normalize(pv_file(tmp_path), pv_map(), {})
    total, device = meters(tmp_path)
    source_map, inputs, baseline = prepare_online_inputs(services.storage.series, art, total,
        [device], ['process'], pv, selected_device_baseload,
        approved_baseline=lambda b:b.records[:4], approved_pv=lambda b:b.records[:4], as_of=CUTOFF, basis='approved fixture')
    param = art.prepare_parameters(pv_capacity_kw=500, model_version='persistence-1', parameter_version='v1', configuration_ref='config-v1')
    manifest = owner.versions.prepare({'data_sources':source_map, 'parameters':param}, 'first')
    owner.versions.check_integrity(manifest.version_manifest_id)
    owner.versions.activate(None, manifest.version_manifest_id, REVIEWER, 'first')
    clock = Interval(start=CUTOFF,end=CUTOFF+timedelta(days=2))
    run = owner.runs.create_run(ctx, 'replay_day', clock, inputs=inputs)
    ctx = ctx.model_copy(update={'run_id':run.run_id, 'version_manifest_id':manifest.version_manifest_id})
    service = ForecastService.from_services(services)
    key, result = service.predict_and_save(ctx, HORIZON)
    load, solar = result.forecasts
    assert load.values == pytest.approx((40,)*4) and solar.values == pytest.approx((24.105,)*4)
    assert load.input_slice.source_ids == ('hipe',) and solar.input_slice.source_ids == (pv.source_id,)
    assert all(r.value == pytest.approx(40) for r in baseline.records[:4])
    assert solar.params['parameter_version'] == 'v1' and result.uncertainty_summary['interval_calibrated'] == 0
    # Fixed configured band: finite, ordered and the same length as values.
    assert all(f.params['interval_status'] == 'uncalibrated' and f.params['interval_band_nature'] == 'configured'
               and f.params['interval_band_ratio'] == 0.10 for f in result.forecasts)
    assert all(len(f.lower) == len(f.values) == len(f.upper) for f in result.forecasts)
    assert all(lower <= value <= upper and math.isfinite(lower) and math.isfinite(upper)
               for f in result.forecasts for value, lower, upper in zip(f.values, f.lower, f.upper))
    assert service.store.put(result) == key
    mismatched = tuple(f.model_copy(update={'params': f.params | {'parameter_ref':'other-parameter'}}) for f in result.forecasts)
    wrong_key = service.store.put(result.model_copy(update={'forecasts':mismatched}))
    with pytest.raises(ValueError, match='parameters do not match'):
        service.load(ctx, wrong_key)
    assert service.for_planner(ctx,key)([f.forecast_id for f in result.forecasts]) == result
    with pytest.raises(ValueError): service.for_planner(ctx,key)(['wrong'])
    with pytest.raises(ValueError): service.load(ctx.model_copy(update={'as_of':CUTOFF-timedelta(seconds=1)}),key)
    with pytest.raises(PlantkgError): service.load(ctx.model_copy(update={'plant_id':'other'}),key)
    snap = services.registry.resolve(StateProvider).at(ctx, CUTOFF)
    pack = PvPack(PvConfig(entity_id=f'entity://{cfg.plant_id}/pv/PV1',kwp=500,config_ref='config://test#pv'))
    req = fixture('planning_request').model_copy(update={'context':ctx, 'snapshot_id':snap.snapshot_id,
        'forecast_ids':tuple(f.forecast_id for f in result.forecasts), 'rule_ids':(), 'horizon':HORIZON, 'packs':('pv',)})
    planner = SharedPlanner({'pv':pack}, lambda _:snap, service.for_planner(ctx,key), lambda _:())
    planned = planner.solve(req)
    assert planned.solver_status == 'optimal', planned.validation_errors
    early = ctx.model_copy(update={'as_of':CUTOFF-timedelta(seconds=1)})
    early_snapshot = snap.model_copy(update={'as_of':early.as_of})
    early_planner = SharedPlanner({'pv':pack}, lambda _:early_snapshot, service.for_planner(early,key), lambda _:())
    with pytest.raises(ValueError,match='not been issued'):
        early_planner.solve(req.model_copy(update={'context':early}))
    with pytest.raises(ValueError):
        planner.solve(req.model_copy(update={'forecast_ids':req.forecast_ids[:1]}))
    with pytest.raises(PlantkgError):
        planner.solve(req.model_copy(update={'horizon':Interval(start=HORIZON.start+timedelta(minutes=15), end=HORIZON.end+timedelta(minutes=15))}))
    partial = Interval(start=HORIZON.start, end=HORIZON.end+timedelta(hours=1))
    partial_key, partial_result = service.predict_and_save(ctx, partial)
    assert partial_result.forecasts[0].coverage == 0.5
    partial_forecast = partial_result.forecasts[0]
    assert len(partial_forecast.lower) == len(partial_forecast.values) == len(partial_forecast.upper)
    assert all((lower is None) == (value is None) for value, lower in zip(partial_forecast.values, partial_forecast.lower))
    partial_req = req.model_copy(update={'horizon':partial, 'forecast_ids':tuple(f.forecast_id for f in partial_result.forecasts)})
    partial_planner = SharedPlanner({'pv':pack}, lambda _:snap, service.for_planner(ctx,partial_key), lambda _:())
    with pytest.raises(PlantkgError): partial_planner.solve(partial_req)
    new_param = art.prepare_parameters(pv_capacity_kw=500, model_version='persistence-1', parameter_version='v2', configuration_ref='config-v2', pv_correction_k=1.5)
    new_manifest = owner.versions.prepare({'data_sources':source_map, 'parameters':new_param}, 'second')
    owner.versions.check_integrity(new_manifest.version_manifest_id)
    owner.versions.activate(manifest.version_manifest_id, new_manifest.version_manifest_id, REVIEWER, 'second')
    run2 = owner.runs.create_run(ctx.model_copy(update={'version_manifest_id':new_manifest.version_manifest_id}), 'replay_day', clock, inputs=inputs)
    ctx2 = ctx.model_copy(update={'run_id':run2.run_id,'version_manifest_id':new_manifest.version_manifest_id})
    key2, result2 = service.predict_and_save(ctx2,HORIZON)
    assert result2.forecasts[1].values == pytest.approx((36.1575,)*4)
    assert key2 != key and result2.forecasts[1].forecast_id != solar.forecast_id
    snap2 = services.registry.resolve(StateProvider).at(ctx2, CUTOFF)
    req2 = req.model_copy(update={'context':ctx2, 'snapshot_id':snap2.snapshot_id,
        'forecast_ids':tuple(f.forecast_id for f in result2.forecasts)})
    plan2 = SharedPlanner({'pv':pack}, lambda _:snap2, service.for_planner(ctx2,key2), lambda _:()).solve(req2)
    assert plan2.solver_status == 'optimal', plan2.validation_errors
    pv_used = lambda plan: sum(a.value for a in plan.actions if a.quantity == 'pv_use')
    assert pv_used(plan2.plan) > pv_used(planned.plan)
    assert service.predict_and_save(ctx,HORIZON) == (key,result)
    with pytest.raises(ValueError): service.load(ctx2,key)
    services.close()
    # Copy only after the SQLite writer and Oxigraph have closed; the relocated
    # directory includes the catalog, Parquet, maps, parameters and forecasts.
    restored_path = tmp_path.parent / (tmp_path.name + '-restored')
    shutil.copytree(tmp_path, restored_path)
    restored = build_services(settings(restored_path))
    try:
        restored_service = ForecastService.from_services(restored)
        assert restored_service.load(ctx, key) == result
        assert restored_service.predict_and_save(ctx, HORIZON) == (key, result)
        assert restored_service.load(ctx2, key2) == result2
    finally:
        restored.close()
    services = build_services(cfg)
    try:
        service = ForecastService.from_services(services)
        assert service.load(ctx,key) == result
        assert service.predict_and_save(ctx,HORIZON) == (key,result)
        parameter_path = services.version_run.integration_artifacts.path / ('parameters-' + param.ref.split(':')[-1] + '.json')
        parameter_bytes = parameter_path.read_bytes()
        parameter_path.write_bytes(b'corrupt')
        with pytest.raises(PlantkgError): service.load(ctx,key)
        parameter_path.write_bytes(parameter_bytes)
        path = service.store.directory/(key+'.json')
        contents = json.loads(path.read_text(encoding='utf-8'))
        contents['bundle']['forecasts'][0]['values'][0] = 9
        path.write_text(json.dumps(contents),encoding='utf-8')
        with pytest.raises(ValueError,match='checksum'): service.load(ctx,key)
        path.unlink()
        with pytest.raises(FileNotFoundError): service.load(ctx,key)
    finally:
        services.close()


def test_mock_assembly_and_disabled_storage():
    services = build_services(load_settings(profile='mock', overrides={'storage.enabled':True}))
    try:
        service = ForecastService.from_services(services)
        assert service.store.directory.is_dir()
    finally:
        services.close()
    services = build_services(load_settings(profile='mock'))
    try:
        with pytest.raises(ValueError): ForecastService.from_services(services)
    finally:
        services.close()


def test_full_day_real_adapter_history_and_b09(tmp_path):
    from zoneinfo import ZoneInfo
    cfg = settings(tmp_path)
    services = build_services(cfg)
    try:
        start = datetime(2017, 10, 23, tzinfo=ZoneInfo('Europe/Berlin'))
        def meter_source(name, value):
            rows = [[(start + timedelta(minutes=i)).isoformat(), name, value] for i in range(3*24*60+1)]
            return HipeAdapter().normalize(hipe_csv(tmp_path,rows), hipe_map(plant_start='2026-06-30',days=3), {})
        main, machine = meter_source('main',2), meter_source('machine',0.4)
        path = pv_file(tmp_path)
        response = json.loads(path.read_text(encoding='utf-8'))
        pv_start = datetime(2017,6,29,16,30,tzinfo=UTC)
        response['outputs']['hourly'] = [{'time':(pv_start+timedelta(hours=i)).strftime('%Y%m%d:%H%M'), 'P':48.21} for i in range(72)]
        path.write_text(json.dumps(response),encoding='utf-8')
        pv = PvgisAdapter().normalize(path,pv_map(source_start='2017-06-30',plant_start='2026-06-30',days=3),{})
        cutoff = datetime(2026,7,1,18,tzinfo=UTC)
        horizon = Interval(start=datetime(2026,7,2,tzinfo=UTC),end=datetime(2026,7,3,tzinfo=UTC))
        owner = services.version_run
        art = owner.integration_artifacts
        approved = lambda b: [r for r in b.records if r.available_at <= cutoff and r.interval.end <= cutoff]
        source, inputs, _ = prepare_online_inputs(services.storage.series,art,main,[machine],['machine'],pv,
            selected_device_baseload,approved_baseline=approved,approved_pv=approved,as_of=cutoff,basis='day fixture')
        params = art.prepare_parameters(pv_capacity_kw=500,model_version='persistence-1',parameter_version='v1',configuration_ref='cfg')
        manifest = owner.versions.prepare({'data_sources':source,'parameters':params},'day')
        owner.versions.check_integrity(manifest.version_manifest_id)
        ctx = context(plant_id=cfg.plant_id,as_of=cutoff,version_manifest_id=manifest.version_manifest_id)
        run = owner.runs.create_run(ctx,'replay_day',Interval(start=cutoff,end=horizon.end),inputs=inputs)
        ctx = ctx.model_copy(update={'run_id':run.run_id})
        service = ForecastService.from_services(services)
        key, forecast = service.predict_and_save(ctx,horizon)
        assert all(f.coverage == 1 and len(f.values) == 96 for f in forecast.forecasts)
        assert forecast.uncertainty_summary['pv_available_fallback_slots'] == 24
        snap = services.registry.resolve(StateProvider).at(ctx,cutoff)
        pack = PvPack(PvConfig(entity_id=f'entity://{cfg.plant_id}/pv/PV1',kwp=500,config_ref='config://test#pv'))
        req = fixture('planning_request').model_copy(update={'context':ctx,'snapshot_id':snap.snapshot_id,
            'forecast_ids':tuple(f.forecast_id for f in forecast.forecasts),'rule_ids':(),'horizon':horizon,'packs':('pv',)})
        from tests.planner.fake_packs import FakeTariff
        tariff = FakeTariff([0.6] * 96)
        req = req.model_copy(update={'packs':('pv','tariff')})
        planner = SharedPlanner({'pv':pack,'tariff':tariff},lambda _:snap,service.for_planner(ctx,key),lambda _:())
        result = planner.solve(req)
        assert result.solver_status == 'optimal', result.validation_errors
        assert result.plan.forecast_ids == req.forecast_ids and result.objective_value > 0
    finally:
        services.close()
