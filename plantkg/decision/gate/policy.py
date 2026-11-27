"""Deterministic gate-policy-v1; no I/O and no approval side effects.

Materials must be assembled by a trusted server-side provider from the immutable
planning request and the referenced artifacts, never from tool arguments alone.
The caller persists the decision and creates a real pending action before it
constructs a review_required GateDecision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from plantkg.core.contracts import Forecast, Interval, Plan, PlanningRequest, Rule


@dataclass(frozen=True)
class PolicyParameters:
    version: str = "gate-policy-v1"
    min_reliability: float = 0.95
    min_coverage: float = 1.0
    min_baseline_cny: float = 0.01
    max_relative_cost_delta: float = 0.10

    def __post_init__(self) -> None:
        # Changing any threshold is a new policy, never a silent v1 mutation.
        if (self.version, self.min_reliability, self.min_coverage,
                self.min_baseline_cny, self.max_relative_cost_delta) != (
                "gate-policy-v1", 0.95, 1.0, 0.01, 0.10):
            raise ValueError("gate-policy-v1 parameters are fixed")

    def snapshot(self) -> dict[str, float]:
        return {"min_reliability": self.min_reliability, "min_coverage": self.min_coverage,
                "min_baseline_cny": self.min_baseline_cny,
                "max_relative_cost_delta": self.max_relative_cost_delta}


@dataclass(frozen=True)
class CalibrationEvidence:
    forecast_id: str
    model_version: str
    reliability: float
    event: str
    calibration_version: str
    validation_population: str
    provider: str
    source: str

    def __post_init__(self) -> None:
        if not all((self.forecast_id, self.model_version, self.event.strip(),
                    self.calibration_version.strip(), self.validation_population.strip(),
                    self.provider.strip(), self.source.strip())):
            raise ValueError("calibration evidence must identify its semantics and source")
        if not math.isfinite(self.reliability) or not 0 <= self.reliability <= 1:
            raise ValueError("reliability must be finite and in [0, 1]")


@dataclass(frozen=True)
class CostEvidence:
    """Explicit CNY cost, attributable to a source and comparable input snapshot."""
    amount_cny: float
    source: str
    input_identity: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.amount_cny) or not self.source.strip() or not self.input_identity.strip():
            raise ValueError("cost requires a finite amount, source and input identity")


@dataclass(frozen=True)
class BaselineEvidence:
    plan: Plan
    plant_id: str
    cost: CostEvidence


@dataclass(frozen=True)
class RuleRequirement:
    """The actual interval over which a particular rule is needed."""
    rule_id: str
    interval: Interval


@dataclass(frozen=True)
class PolicyMaterials:
    """Artifacts fetched by the provider for the plan's fixed run.

    ``request`` is the original request, not a newly reconstructed request.
    ``verified_refs`` identifies configuration and asset references actually
    checked against that run's fixed manifest. An empty collection verifies none.
    ``rule_conflicts`` is the result of the applicable rule conflict check;
    None means that no such check was performed.
    ``dependencies_verified`` asserts that the trusted provider has examined
    every real rule/config/asset dependency and its applicable time slots;
    ``rule_requirements`` records those slots for every referenced rule.
    """
    request: PlanningRequest | None = None
    forecasts: tuple[Forecast, ...] = ()
    rules: tuple[Rule, ...] = ()
    dependencies_verified: bool = False
    rule_conflicts: tuple[str, ...] | None = None
    verified_refs: frozenset[tuple[str, str]] = frozenset()
    plan_cost: CostEvidence | None = None
    baseline: BaselineEvidence | None = None
    calibrations: tuple[CalibrationEvidence, ...] = ()
    rule_requirements: tuple[RuleRequirement, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    outcome: Literal["auto_confirmed", "review_required", "rejected"]
    reasons: tuple[str, ...]
    confirmation_blockers: tuple[str, ...]
    thresholds: dict[str, float] = field(default_factory=dict)
    policy_version: str = "gate-policy-v1"


def evaluate_policy(plan: Plan, materials: PolicyMaterials,
                    parameters: PolicyParameters = PolicyParameters()) -> PolicyResult:
    """Evaluate fixed policy. Re-fetch materials and re-evaluate before approval.

    Blockers prohibit *manual* confirmation as well; warnings alone can be
    explicitly accepted by a human. Identity conflicts always reject.
    """
    reasons: list[str] = []
    blockers: list[str] = []
    rejects: list[str] = []

    def warn(message: str, *, block: bool = False) -> None:
        if message not in reasons:
            reasons.append(message)
        if block and message not in blockers:
            blockers.append(message)

    def reject(message: str) -> None:
        if message not in rejects:
            rejects.append(message)

    req = materials.request
    if req is None:
        warn("缺少原始规划请求及固定运行身份", block=True)
    else:
        ctx = req.context
        if (ctx.run_id != plan.run_id or ctx.version_manifest_id != plan.version_manifest_id
                or req.snapshot_id != plan.snapshot_id or req.horizon != plan.valid_interval
                or tuple(req.forecast_ids) != tuple(plan.forecast_ids) or req.policy != plan.policy):
            reject("计划与原始规划请求的运行、清单、快照、区间或预测身份不匹配")
        for action in plan.actions:
            if not action.object.startswith(f"entity://{ctx.plant_id}/"):
                reject(f"动作 {action.action_id} 跨厂区")
        if len(set(req.forecast_ids)) != len(req.forecast_ids):
            reject("规划请求含重复预测身份")

    if plan.solver_status != "optimal":
        warn("求解结果未达到 optimal")
    if plan.status in ("executed", "superseded", "rejected"):
        reject(f"计划状态 {plan.status} 不允许新批准")

    forecasts = materials.forecasts
    by_id = {f.forecast_id: f for f in forecasts}
    if len(by_id) != len(forecasts):
        reject("预测材料含重复身份")
    if set(by_id) - set(plan.forecast_ids):
        reject("预测材料包含计划未引用的身份")
    for fid in plan.forecast_ids:
        if fid not in by_id:
            warn(f"缺少预测 {fid}", block=True)
    if req is not None:
        for f in forecasts:
            for key, expected in (("plant_id", req.context.plant_id), ("run_id", plan.run_id),
                                  ("version_manifest_id", plan.version_manifest_id)):
                actual = f.params.get(key)
                if actual is None:
                    warn(f"预测 {f.forecast_id} 缺少 {key} 身份", block=True)
                elif actual != expected:
                    reject(f"预测 {f.forecast_id} 的 {key} 身份不匹配")
            if f.issued_at > req.context.as_of:
                reject(f"预测 {f.forecast_id} 在固定运行时点尚未发布")
    for quantity in ("base_load", "pv_available"):
        matches = [f for f in forecasts if f.forecast_id in plan.forecast_ids and f.quantity == quantity]
        if len(matches) != 1:
            warn(f"必要预测 {quantity} 缺失或重复", block=True)
            continue
        f = matches[0]
        if req is None:
            continue
        if (f.step_minutes != req.step_minutes or f.target.start > plan.valid_interval.start
                or f.target.end < plan.valid_interval.end or f.unit != "kW"):
            warn(f"预测 {f.forecast_id} 的区间、步长、发布时间或单位不匹配", block=True)
            continue
        step_seconds = req.step_minutes * 60
        shift = (plan.valid_interval.start - f.target.start).total_seconds() / step_seconds
        slots = (plan.valid_interval.end - plan.valid_interval.start).total_seconds() / step_seconds
        if not shift.is_integer() or not slots.is_integer() or slots <= 0:
            warn(f"预测 {f.forecast_id} 时段网格不匹配", block=True)
            continue
        values = f.values[int(shift):int(shift + slots)]
        if (len(values) != int(slots) or any(v is None or not math.isfinite(v) or v < 0 for v in values)
                or f.coverage < parameters.min_coverage):
            warn(f"预测 {f.forecast_id} 覆盖不足或存在缺值、无效值", block=True)
        evidence = [c for c in materials.calibrations if c.forecast_id == f.forecast_id
                    and c.model_version == f.model_version]
        has_interval = f.lower is not None and f.upper is not None and not any(
            v is None or not math.isfinite(v)
            for band in (f.lower[int(shift):int(shift + slots)], f.upper[int(shift):int(shift + slots)]) for v in band)
        uncalibrated = f.params.get("interval_status") == "uncalibrated"
        if uncalibrated and has_interval:
            warn(f"预测 {f.forecast_id} 的误差区间未校准（configured 固定带宽），仍须人工确认")
        elif uncalibrated or len(evidence) != 1 or not has_interval:
            warn(f"预测 {f.forecast_id} 缺少可追溯的区间校准可靠度")
        elif any(lo > hi for lo, hi in zip(f.lower[int(shift):int(shift + slots)],
                                            f.upper[int(shift):int(shift + slots)])):
            warn(f"预测 {f.forecast_id} 的校准区间上下界颠倒")
        elif evidence[0].reliability < parameters.min_reliability:
            warn(f"预测 {f.forecast_id} 的校准可靠度低于 {parameters.min_reliability}")

    if req is not None:
        rule_ids = set(req.rule_ids)
        rules = {rule.rule_id: rule for rule in materials.rules}
        if not materials.dependencies_verified:
            warn("计划的规则和配置依赖未完成核验", block=True)
        if len(rules) != len(materials.rules) or set(rules) - rule_ids:
            reject("规则材料包含重复或未请求的规则")
        requirements = materials.rule_requirements
        if {item.rule_id for item in requirements} - rule_ids:
            reject("规则适用区间包含未登记的规则")
        for item in requirements:
            if not (plan.valid_interval.start <= item.interval.start < item.interval.end <= plan.valid_interval.end):
                reject(f"规则 {item.rule_id} 的依赖区间超出计划")
        for rid in sorted(rule_ids):
            rule = rules.get(rid)
            needed = [item.interval for item in requirements if item.rule_id == rid]
            if not needed:
                warn(f"规则 {rid} 缺少实际依赖时段证明", block=True)
            if rule is None:
                warn(f"缺少依赖规则 {rid}", block=True)
            elif (not rule.compilable or not rule.is_visible(req.context.as_of)
                  or rule.effective_from >= plan.valid_interval.end
                  or (rule.effective_to is not None and rule.effective_to <= plan.valid_interval.start)):
                warn(f"规则 {rid} 未审核、不可见或不适用", block=True)
            if rule is not None:
                if any(rule.effective_from > interval.start or
                       (rule.effective_to is not None and rule.effective_to < interval.end) for interval in needed):
                    warn(f"规则 {rid} 未覆盖实际依赖时段", block=True)
                if any(not subject.startswith(f"entity://{req.context.plant_id}/") for subject in rule.subject_ids):
                    reject(f"规则 {rid} 跨厂区")
                if rule.config_ref and ("config", rule.config_ref) not in materials.verified_refs:
                    warn(f"规则 {rid} 的配置来源未核验", block=True)
                if rule.source_asset_version and ("asset_version", rule.source_asset_version) not in materials.verified_refs:
                    warn(f"规则 {rid} 的资料来源未核验", block=True)
        for action in plan.actions:
            for ref in action.refs:
                if ref.kind == "rule" and ref.id not in rule_ids:
                    reject(f"动作引用未登记的规则 {ref.id}")
                elif ref.kind == "rule" and ref.id in rules:
                    rule = rules[ref.id]
                    if (rule.effective_from > action.interval.start or
                            (rule.effective_to is not None and rule.effective_to < action.interval.end)):
                        warn(f"规则 {ref.id} 未覆盖动作 {action.action_id} 的整个区间", block=True)
                elif ref.kind in ("config", "asset_version") and (ref.kind, ref.id) not in materials.verified_refs:
                    warn(f"动作引用 {ref.id} 缺少固定清单来源核验", block=True)
    if materials.rule_conflicts is None:
        warn("规则冲突检查结果未知", block=True)
    elif materials.rule_conflicts:
        for conflict in materials.rule_conflicts:
            warn(f"规则冲突: {conflict}", block=True)

    cost, baseline = materials.plan_cost, materials.baseline
    if cost is None:
        warn("缺少计划的明确 CNY 成本", block=True)
    if baseline is None:
        warn("缺少可比基线")
    elif cost is None:
        pass
    elif (req is None or baseline.plant_id != req.context.plant_id
          or baseline.plan.valid_interval != plan.valid_interval
          or baseline.plan.version_manifest_id != plan.version_manifest_id
          or baseline.cost.input_identity != cost.input_identity):
        warn("基线的厂区、区间、固定清单或可比输入不一致")
    elif baseline.cost.amount_cny <= parameters.min_baseline_cny:
        warn("基线成本不大于 0.01 CNY")
    elif abs(cost.amount_cny - baseline.cost.amount_cny) / baseline.cost.amount_cny > parameters.max_relative_cost_delta:
        warn("计划与基线的成本相对差超过 0.10")

    if rejects:
        return PolicyResult("rejected", tuple(rejects + reasons), tuple(rejects + blockers), parameters.snapshot())
    if reasons:
        return PolicyResult("review_required", tuple(reasons), tuple(blockers), parameters.snapshot())
    return PolicyResult("auto_confirmed", ("全部 gate-policy-v1 自动确认条件满足",), (), parameters.snapshot())
