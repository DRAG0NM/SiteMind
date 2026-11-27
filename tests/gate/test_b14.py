from types import SimpleNamespace
import json

import pytest

from plantkg.core.contracts import ForecastBundle
from plantkg.decision.forecasting.service import ForecastService
from plantkg.decision.forecasting.store import ForecastStore
from plantkg.decision.forecasting.history import digest
from plantkg.decision.gate.b14 import load_b14_forecasts
from plantkg.decision.gate.policy import evaluate_policy
from tests.gate.test_policy import complete_materials
from dataclasses import replace


class Clock:
    def current(self, context):
        return context.as_of


class Parameters:
    def get_for_context(self, context):
        return SimpleNamespace(parameter_ref="params-v1", parameter_version="v1",
            configuration_ref="config-v1", pv_correction_k=1.0, pv_capacity_kw=500,
            model_version="persistence-1")


def put_fixture(store, bundle):
    # WinError 5 prevents hard-link creation in the sandbox. Construct the
    # documented B14 envelope; the real store.get still validates both hashes.
    key = store._key(bundle)
    payload = bundle.model_dump(mode="json")
    envelope = {"format_version": 1, "content_hash": digest(payload), "bundle": payload}
    (store.directory / (key + ".json")).write_text(json.dumps(envelope), encoding="utf-8")
    return key


def test_real_b14_artifact_loading_checks_fixed_parameters_and_never_invents_calibration(tmp_path):
    plan, materials = complete_materials()
    params = {"parameter_ref": "params-v1", "parameter_version": "v1", "configuration_ref": "config-v1",
              "pv_correction_k": 1.0, "pv_capacity_kw": 500}
    forecasts = tuple(f.model_copy(update={"lower": None, "upper": None, "params": f.params | params})
                      for f in materials.forecasts)
    store = ForecastStore(tmp_path)
    key = put_fixture(store, ForecastBundle(forecasts=forecasts, uncertainty_summary={"interval_calibrated": 0.0}))
    # ForecastService and ForecastStore are real; clock and parameter source are
    # test doubles. The full B14 integration covers their production wiring.
    service = ForecastService(None, Parameters(), Clock(), store)
    loaded = load_b14_forecasts(service, materials.request.context, plan, key)
    assert loaded == forecasts
    decision = evaluate_policy(plan, replace(materials, forecasts=loaded, calibrations=()))
    assert decision.outcome == "review_required"
    assert any("校准" in reason for reason in decision.reasons)
    with pytest.raises(ValueError, match="fixed run"):
        load_b14_forecasts(service, materials.request.context.model_copy(update={"run_id": "other"}), plan, key)
    with pytest.raises(ValueError, match="forecast IDs"):
        load_b14_forecasts(service, materials.request.context, plan.model_copy(update={"forecast_ids": ("other",)}), key)
    wrong = tuple(f.model_copy(update={"params": f.params | {"parameter_ref": "wrong"}}) for f in forecasts)
    wrong_key = put_fixture(store, ForecastBundle(forecasts=wrong))
    with pytest.raises(ValueError, match="fixed manifest"):
        load_b14_forecasts(service, materials.request.context, plan, wrong_key)


def test_uncalibrated_interval_has_its_own_review_reason():
    plan, materials = complete_materials()
    forecasts = tuple(f.model_copy(update={"params": f.params | {"interval_status": "uncalibrated"}})
                      for f in materials.forecasts)
    result = evaluate_policy(plan, replace(materials, forecasts=forecasts, calibrations=()))
    assert result.outcome == "review_required"
    assert any("误差区间未校准" in reason for reason in result.reasons)
    assert not result.confirmation_blockers


def test_default_provider_loads_real_b14_but_never_fabricates_approval_material(tmp_path):
    from plantkg.core.contracts import ErrorCode, PlantkgError
    from plantkg.decision.gate.materials import B14MaterialsProvider

    plan, materials = complete_materials()
    params = {"parameter_ref": "params-v1", "parameter_version": "v1", "configuration_ref": "config-v1",
              "pv_correction_k": 1.0, "pv_capacity_kw": 500, "interval_status": "uncalibrated"}
    forecasts = tuple(f.model_copy(update={"lower": None, "upper": None, "params": f.params | params})
                      for f in materials.forecasts)
    store = ForecastStore(tmp_path)
    key = put_fixture(store, ForecastBundle(forecasts=forecasts))
    provider = B14MaterialsProvider(ForecastService(None, Parameters(), Clock(), store))
    supplied = provider(plan, {"planning_request": materials.request.model_dump(mode="json"), "forecast_key": key})
    assert supplied.forecasts == forecasts
    result = evaluate_policy(plan, supplied)
    assert result.outcome == "review_required"
    assert any("校准" in reason for reason in result.reasons)
    assert any("依赖" in reason for reason in result.confirmation_blockers)
    assert any("CNY" in reason for reason in result.confirmation_blockers)
    assert evaluate_policy(plan, provider(plan, {})).confirmation_blockers
    assert evaluate_policy(plan, provider(plan, {"planning_request": materials.request.model_dump(mode="json")})).confirmation_blockers
    with pytest.raises(PlantkgError) as malformed:
        provider(plan, {"planning_request": {"incorrect": True}, "forecast_key": key})
    assert malformed.value.code == ErrorCode.PRECONDITION
    with pytest.raises(PlantkgError) as mismatch:
        provider(plan.model_copy(update={"forecast_ids": ("wrong",)}),
                 {"planning_request": materials.request.model_dump(mode="json"), "forecast_key": key})
    assert mismatch.value.code == ErrorCode.PRECONDITION
