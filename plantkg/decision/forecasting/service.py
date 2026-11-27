"""Production B14 wiring over B04/B07 and an isolated forecast artifact store."""
from pathlib import Path

from plantkg.core.contracts import ForecastBundle
from plantkg.core.ports import StateProvider, TimeSeriesQuery
from .forecast import DEFAULT_INTERVAL_BAND_RATIO, ParameterSnapshot, PersistenceForecaster
from .history import HistoryReader
from .store import ForecastStore


class FrozenParameters:
    """Resolve the parameters of this run, never the currently active configuration."""
    def __init__(self, artifacts, versions, runs, interval_band_ratio=DEFAULT_INTERVAL_BAND_RATIO):
        self.artifacts, self.versions, self.runs = artifacts, versions, runs
        self.interval_band_ratio = interval_band_ratio

    def get_for_context(self, context):
        p = self.artifacts.parameters_for_run(context, self.versions, self.runs)
        return ParameterSnapshot(manifest_id=p['manifest_id'], parameter_ref=p['parameter_ref'],
            model_version=p['model_version'], pv_correction_k=p['pv_correction_k'],
            pv_capacity_kw=p['pv_capacity_kw'], parameter_version=p['parameter_version'],
            configuration_ref=p['configuration_ref'], interval_band_ratio=self.interval_band_ratio)


class ForecastService:
    """Explicit assembly; keys and forecast IDs are persisted, not kept in memory."""
    def __init__(self, reader, parameters, clock, store):
        self.reader, self.clock, self.store = reader, clock, store
        self.forecaster = PersistenceForecaster(reader, parameters)

    @classmethod
    def from_services(cls, services, directory=None):
        if services.version_run is None or services.storage is None or not services.storage.enabled:
            raise ValueError('forecast service requires enabled persistent storage')
        owner = services.version_run
        path = Path(directory) if directory is not None else services.storage.writer.path.parent / 'forecast_artifacts'
        band = (services.settings.section('forecasting') or {}).get('interval_band_ratio', DEFAULT_INTERVAL_BAND_RATIO)
        return cls(HistoryReader(services.registry.resolve(TimeSeriesQuery)),
                   FrozenParameters(owner.integration_artifacts, owner.versions, owner.runs, band),
                   services.registry.resolve(StateProvider), ForecastStore(path))

    def predict_and_save(self, context, horizon):
        self.clock.current(context)
        history = self.reader.read(context, horizon)
        bundle = self.forecaster.predict(context, horizon, history)
        key = self.store.put(bundle)
        return key, self.load(context, key)

    def load(self, context, key):
        self.clock.current(context)
        bundle = self.store.get(key)
        snapshot = self.forecaster.parameters.get_for_context(context)
        for forecast in bundle.forecasts:
            p = forecast.params
            if (p.get('plant_id'), p.get('run_id'), p.get('version_manifest_id')) != (
                    context.plant_id, context.run_id, context.version_manifest_id):
                raise ValueError('forecast belongs to another plant, run or manifest')
            if forecast.issued_at > context.as_of:
                raise ValueError('forecast has not been issued at this time')
            if (p.get('parameter_ref'), p.get('parameter_version'), p.get('configuration_ref'),
                    p.get('pv_correction_k'), p.get('pv_capacity_kw'), forecast.model_version) != (
                    snapshot.parameter_ref, snapshot.parameter_version, snapshot.configuration_ref,
                    snapshot.pv_correction_k, snapshot.pv_capacity_kw, snapshot.model_version):
                raise ValueError('forecast parameters do not match the fixed manifest')
        return bundle

    def for_planner(self, context, key):
        """Create B09's forecasts_of(ids) callback bound to a persisted bundle key."""
        def resolve(ids):
            bundle = self.load(context, key)
            by_id = {f.forecast_id: f for f in bundle.forecasts}
            if len(by_id) != len(bundle.forecasts) or len(set(ids)) != len(ids) or set(ids) != set(by_id):
                raise ValueError('forecast IDs do not match the persisted bundle')
            return ForecastBundle(forecasts=tuple(by_id[i] for i in ids), uncertainty_summary=bundle.uncertainty_summary)
        return resolve
