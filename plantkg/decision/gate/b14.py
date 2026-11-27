"""Adapter for loading the actual B14 forecast artifact for a fixed plan/run."""
from __future__ import annotations

from plantkg.core.contracts import Context, Forecast, Plan
from plantkg.decision.forecasting.service import ForecastService


def load_b14_forecasts(service: ForecastService, context: Context, plan: Plan,
                       artifact_key: str) -> tuple[Forecast, ...]:
    """Load and bind exact plan forecasts; this does not assert calibration.

    ``context`` and ``artifact_key`` must come from the server's persisted run
    and planning record. ForecastService.load checks the fixed run, manifest,
    parameter snapshot, plant and publication time against the persisted B14
    artifact. The policy then evaluates slot coverage and any independently
    supplied calibration evidence.
    """
    if not context.can_write or context.run_id != plan.run_id or context.version_manifest_id != plan.version_manifest_id:
        raise ValueError("plan and authenticated fixed run do not match")
    bundle = service.load(context, artifact_key)
    by_id = {forecast.forecast_id: forecast for forecast in bundle.forecasts}
    if (len(by_id) != len(bundle.forecasts) or len(set(plan.forecast_ids)) != len(plan.forecast_ids)
            or set(by_id) != set(plan.forecast_ids)):
        raise ValueError("plan forecast IDs do not match the fixed B14 artifact")
    return tuple(by_id[fid] for fid in plan.forecast_ids)
