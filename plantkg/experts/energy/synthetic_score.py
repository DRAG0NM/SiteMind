"""离线示例评分输入：合成预测与合成校准样本，不代表真实运行数据。"""
from __future__ import annotations

import copy
import dataclasses
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plantkg.backends import FakeJudge
from plantkg.backends.calibration import TemperatureCalibrator
from plantkg.core.contracts import ErrorCode, ForecastBundle, FrozenSplit, Interval, OntologyCandidate, PlantkgError, SolverBudget, StateSnapshot
from plantkg.core.versions import digest
from plantkg.experts.energy.forecast import _forecast
from plantkg.experts.energy.planner import load_scenario_packs
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]
DEMO_DAY = "2026-07-01"


def _split(name, purpose, days):
    return FrozenSplit(split_id=name, purpose=purpose, days=days,
                       frozen_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                       content_hash="sha256:" + digest({"nature": "synthetic", "purpose": purpose, "days": days}))


def _synthetic_probability(candidate_id):
    options = ("A", "B", "C")
    definitions = {key: key for key in options}
    synthetic_distributions = ({"A": 0.25, "B": 0.65, "C": 0.10}, {"A": 0.55, "B": 0.30, "C": 0.15},
                               {"A": 0.15, "B": 0.70, "C": 0.15}, {"A": 0.45, "B": 0.40, "C": 0.15})
    labels = ("B", "A", "B", "B")
    def judge_result(index, distribution, judge):
        question = OntologyCandidate(candidate_id=f"synthetic-{index}", layer="constraint", subject="synthetic-tariff",
                                     options=options, recall_source="vector")
        state = {"content": "synthetic", "definitions": definitions, "vector_top": "B",
                 "fake_distributions": [distribution] * 3}
        return judge.decide(state, question, options)
    calibration = TemperatureCalibrator()
    examples = [judge_result(i, dist, FakeJudge()) for i, dist in enumerate(synthetic_distributions)]
    calibration.fit(examples, list(labels), _split("b30-synthetic-calibration", "calibration", ("2026-06-01", "2026-06-02")))
    result = judge_result(candidate_id, {"A": 0.30, "B": 0.55, "C": 0.15}, FakeJudge(calibrator=calibration))
    return result.prob, "synthetic:" + calibration.version


def synthetic_score_inputs(context, candidate, alternative):
    """显式 synthetic_demo 才启用；未指定时交回未校准队列。"""
    if not isinstance(alternative, dict) or alternative.get("synthetic_demo") is not True:
        return None
    if candidate.draft.get("kind") != "tariff_tou_partial" or context.plant_id != "demo-electronics":
        raise PlantkgError(ErrorCode.PRECONDITION, "合成评分只支持演示厂区的电价候选")
    peak_ratio = alternative.get("peak_ratio")
    if isinstance(peak_ratio, bool) or not isinstance(peak_ratio, (int, float)) or not math.isfinite(peak_ratio) or not -0.9 <= peak_ratio <= 3:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "合成备选峰段比例须在 -0.9 到 3 之间")
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    packs = load_scenario_packs(scenario)
    tariffs = [rule for rule in packs.rules if rule.kind == "tariff_tou"]
    if len(tariffs) != 1:
        raise PlantkgError(ErrorCode.PRECONDITION, "合成演示需要一条基础电价规则")
    source = tariffs[0]
    condition_a = copy.deepcopy(source.condition)
    condition_a.update(demo=True, source={"nature": "synthetic", "note": "B30 合成评分示例"})
    baseline = source.model_copy(update={"condition": condition_a, "nature": "synthetic", "review_status": "trusted_config",
        "reviewer": None, "source_asset_version": None, "config_ref": "config://b30-synthetic-score#tariff",
        "available_at": datetime(2026, 5, 1, tzinfo=timezone.utc)})
    condition_b = copy.deepcopy(condition_a)
    for period in condition_b["periods"]:
        if period["name"] == "peak":
            period["ratio"] = float(peak_ratio)
    proposed = baseline.model_copy(update={"rule_id": "b30-synthetic-alternative", "condition": condition_b})
    pack_a = dataclasses.replace(packs, rules=tuple(baseline if rule.rule_id == source.rule_id else rule for rule in packs.rules))
    pack_b = dataclasses.replace(packs, rules=tuple(proposed if rule.rule_id == source.rule_id else rule for rule in packs.rules))
    probability, calibration_version = _synthetic_probability(candidate.candidate_id)
    budget = SolverBudget(time_limit_s=20, mip_rel_gap=0.001, threads=1, random_seed=0)

    def day_inputs(day, as_of):
        if day != DEMO_DAY:
            raise PlantkgError(ErrorCode.PRECONDITION, "合成评分只登记一个演示日")
        start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
        horizon = Interval(start=start, end=start + timedelta(days=1))
        demo_context = context.model_copy(update={"as_of": as_of, "run_id": "b30-synthetic-score-run"})
        values = [500.0 + (120.0 if 32 <= i < 72 else 0.0) for i in range(96)]
        pv = [max(0.0, 160.0 * (1.0 - abs(i - 48) / 24)) for i in range(96)]
        forecasts = []
        for quantity, data in (("base_load", values), ("pv_available", pv)):
            forecast = _forecast(quantity, data, horizon, demo_context, "b30-synthetic-score")
            forecasts.append(forecast.model_copy(update={
                "forecast_id": f"b30-synthetic-profile:{quantity}:{day}", "model_version": "b30-synthetic-profile-1",
                "params": forecast.params | {"nature": "synthetic", "method": "configured-demo"},
                "input_slice": forecast.input_slice.model_copy(update={"note": "合成评分示例的预设负荷与光伏序列"})}))
        initial = scenario.initial.soc * packs.storage.capacity_kwh
        snapshot = StateSnapshot(run_id=demo_context.run_id, snapshot_id="b30-synthetic-score-snapshot", event_seq=0,
                                 as_of=as_of, version_manifest_id=context.version_manifest_id,
                                 pack_states={"storage": {"energy_kwh": initial}})
        return {"packs_a": pack_a, "packs_b": pack_b, "forecasts": ForecastBundle(forecasts=tuple(forecasts)),
                "snapshot": snapshot, "context": demo_context, "horizon": horizon, "energy_start_kwh": initial,
                "end_target_kwh": initial, "solver_budget": budget}

    return {"split": _split("b30-synthetic-scoring", "scoring", (DEMO_DAY,)), "probability_b": probability,
            "calibration_version": calibration_version, "mip_rel_gap": budget.mip_rel_gap,
            "nature": "synthetic", "day_inputs": day_inputs}
