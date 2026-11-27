from dataclasses import replace

import pytest

from plantkg.decision.gate.policy import (
    BaselineEvidence, CalibrationEvidence, CostEvidence, PolicyMaterials,
    PolicyParameters, evaluate_policy,
)
from plantkg.testing.fakes import fixture


def complete_materials():
    """Constructed evidence; this is not a calibrated production forecast."""
    req = fixture("planning_request").model_copy(update={"rule_ids": ()})
    plan = fixture("plan").model_copy(update={"status": "draft", "actions": ()})
    source = fixture("forecast_pv")
    params = {"plant_id": req.context.plant_id, "run_id": plan.run_id,
              "version_manifest_id": plan.version_manifest_id}
    def make(fid, quantity):
        return source.model_copy(update={"forecast_id": fid, "quantity": quantity,
            "target": req.horizon, "values": (10.0,) * 96, "lower": (9.0,) * 96,
            "upper": (11.0,) * 96, "params": params})
    forecasts = (make(plan.forecast_ids[0], "pv_available"), make(plan.forecast_ids[1], "base_load"))
    calibrations = tuple(CalibrationEvidence(f.forecast_id, f.model_version, .95,
        "realized value falls inside the interval", "cal-v1", "held-out same-plant days",
        "test-provider", "test://constructed-validation") for f in forecasts)
    cost = CostEvidence(100, "test://cny-electricity", "test://same-inputs")
    baseline = BaselineEvidence(plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}),
                                req.context.plant_id, CostEvidence(100, "test://baseline-cny", cost.input_identity))
    return plan, PolicyMaterials(req, forecasts, (), True, (), frozenset(), cost, baseline, calibrations)


def test_complete_constructed_materials_auto_confirm_and_fixed_snapshot():
    plan, materials = complete_materials()
    result = evaluate_policy(plan, materials)
    assert result.outcome == "auto_confirmed"
    assert not result.confirmation_blockers
    assert result.thresholds == {"min_reliability": .95, "min_coverage": 1.0,
        "min_baseline_cny": .01, "max_relative_cost_delta": .10}
    with pytest.raises(ValueError):
        PolicyParameters(min_reliability=.9)


def test_uncalibrated_is_reviewable_but_missing_forecast_is_not():
    plan, materials = complete_materials()
    forecasts = tuple(f.model_copy(update={"lower": None, "upper": None}) for f in materials.forecasts)
    result = evaluate_policy(plan, replace(materials, forecasts=forecasts, calibrations=()))
    assert result.outcome == "review_required"
    assert not result.confirmation_blockers
    broken = forecasts[0].model_copy(update={"values": (None,) + forecasts[0].values[1:]})
    result = evaluate_policy(plan, replace(materials, forecasts=(broken, forecasts[1])))
    assert result.outcome == "review_required"
    assert any("缺值" in reason for reason in result.confirmation_blockers)


def test_identity_mismatch_rejects_and_other_risks_need_review():
    plan, materials = complete_materials()
    f = materials.forecasts[0]
    wrong = f.model_copy(update={"params": f.params | {"run_id": "wrong"}})
    assert evaluate_policy(plan, replace(materials, forecasts=(wrong, materials.forecasts[1]))).outcome == "rejected"
    low = replace(materials.calibrations[0], reliability=.94)
    result = evaluate_policy(plan.model_copy(update={"solver_status": "feasible"}),
        replace(materials, calibrations=(low, materials.calibrations[1]),
                plan_cost=CostEvidence(111, "test://cost", "test://same-inputs")))
    assert result.outcome == "review_required"
    assert any("求解" in r for r in result.reasons)
    assert any("可靠度" in r for r in result.reasons)
    assert any("成本相对差" in r for r in result.reasons)
    assert not result.confirmation_blockers


def test_missing_dependencies_and_rules_cannot_be_approved():
    plan, materials = complete_materials()
    result = evaluate_policy(plan, replace(materials, dependencies_verified=False, rule_conflicts=None, plan_cost=None))
    assert result.outcome == "review_required"
    assert len(result.confirmation_blockers) == 3
    req = materials.request.model_copy(update={"rule_ids": ("missing-rule",)})
    result = evaluate_policy(plan, replace(materials, request=req, rule_conflicts=("tariff conflict",)))
    assert any("missing-rule" in r for r in result.confirmation_blockers)
    assert any("规则冲突" in r for r in result.confirmation_blockers)


def test_baseline_thresholds_and_calibration_validation():
    plan, materials = complete_materials()
    baseline = materials.baseline
    for amount in (0, .01):
        result = evaluate_policy(plan, replace(materials, baseline=replace(baseline, cost=CostEvidence(amount, "test://cost", "test://same-inputs"))))
        assert result.outcome == "review_required"
        assert not result.confirmation_blockers
    with pytest.raises(ValueError):
        replace(materials.calibrations[0], reliability=float("nan"))
    with pytest.raises(ValueError):
        replace(materials.calibrations[0], event="")


def test_rule_dependency_requires_coverage_and_uncalibrated_status_cannot_be_overridden():
    from datetime import timedelta
    from plantkg.core.contracts import Interval
    from plantkg.decision.gate.policy import RuleRequirement

    plan, materials = complete_materials()
    rule = fixture("rule_trusted_config")
    req = materials.request.model_copy(update={"rule_ids": (rule.rule_id,)})
    end = plan.valid_interval.start + timedelta(hours=1)
    short_rule = rule.model_copy(update={"effective_to": end})
    whole_day = RuleRequirement(rule.rule_id, plan.valid_interval)
    m = replace(materials, request=req, rules=(short_rule,), rule_requirements=(whole_day,),
                verified_refs=frozenset({("config", rule.config_ref)}))
    result = evaluate_policy(plan, m)
    assert any("未覆盖实际依赖时段" in reason for reason in result.confirmation_blockers)
    first_hour = RuleRequirement(rule.rule_id, Interval(start=plan.valid_interval.start, end=end))
    assert evaluate_policy(plan, replace(m, rule_requirements=(first_hour,))).outcome == "auto_confirmed"
    assert any("缺少实际依赖时段" in reason for reason in
               evaluate_policy(plan, replace(m, rule_requirements=())).confirmation_blockers)
    forecasts = list(materials.forecasts)
    forecasts[0] = forecasts[0].model_copy(update={"params": forecasts[0].params | {"interval_status": "uncalibrated"}})
    result = evaluate_policy(plan, replace(materials, forecasts=tuple(forecasts)))
    assert result.outcome == "review_required"
    assert any("校准" in reason for reason in result.reasons)
