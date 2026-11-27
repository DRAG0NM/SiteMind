"""B19 演示装配里的门控材料（12-开发规则「已有代码里的拒绝运行点」第 1、2、3 行）。

- **计划材料提供者**：run_day 在登记计划时把原始 PlanningRequest、预测、规则、核验结果、冲突检查结果、
  CNY 成本与可比基线一并写进登记元数据（服务端生成，不来自工具参数）；本提供者只做校验与还原，缺项
  返回不完整材料（门控转人工），不伪造证据。
- **求解回执核验器**：B09 是同进程求解，回执带请求、快照、预测、规则、包输入与计划摘要；核验器逐项比
  对回执内部一致性后视为可信（数据标 synthetic）。
- **执行依据的配置解析**：从已持久化的回执里取各包输入与电价时段，生成 `validate_basis` 要求的五段
  配置（pv / storage / export / tariff / operations），来源摘要取配置的规范 JSON 摘要。

演示性质（synthetic / derived）在 `PR_BODY.md` 与本模块注释里写明；正式环境应换成真实材料提供者。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import timedelta

from plantkg.core.contracts import (
    Context,
    ErrorCode,
    Forecast,
    Interval,
    Plan,
    PlanningRequest,
    PlantkgError,
    Rule,
)
from plantkg.core.versions import canonical
from plantkg.decision.gate.policy import (
    BaselineEvidence,
    CalibrationEvidence,
    CostEvidence,
    PolicyMaterials,
    RuleRequirement,
)
from plantkg.packs.tariff import price_schedule

# ---------------------------------------------------------------- 材料元数据（run_day 生成）


def build_metadata(*, request: PlanningRequest, forecasts: Sequence[Forecast], rules: Sequence[Rule],
                   verified_refs: frozenset[tuple[str, str]], plan_cost_cny: float, baseline_cny: float,
                   baseline_plan: Plan, input_identity: str, calibrations: Sequence[CalibrationEvidence] = (),
                   rule_conflicts: Sequence[str] = ()) -> dict:
    """门控材料的登记元数据。全部为 JSON，可在 b15_plan_heads.metadata 里长期保存。"""
    horizon = request.horizon
    return {
        "planning_request": request.model_dump(mode="json"),
        "forecasts": [f.model_dump(mode="json") for f in forecasts],
        "rules": [r.model_dump(mode="json") for r in rules],
        "dependencies_verified": True,
        "rule_conflicts": list(rule_conflicts),
        "verified_refs": sorted([list(ref) for ref in verified_refs]),
        "plan_cost": {"amount_cny": round(float(plan_cost_cny), 6), "source": "b08-plan-ledger",
                      "input_identity": input_identity},
        "baseline": {"plan": baseline_plan.model_dump(mode="json"), "plant_id": request.context.plant_id,
                     "cost": {"amount_cny": round(float(baseline_cny), 6), "source": "b08-baseline-ledger",
                              "input_identity": input_identity}},
        "calibrations": [{"forecast_id": c.forecast_id, "model_version": c.model_version, "reliability": c.reliability,
                          "event": c.event, "calibration_version": c.calibration_version,
                          "validation_population": c.validation_population, "provider": c.provider, "source": c.source}
                         for c in calibrations],
        "rule_requirements": [{"rule_id": r.rule_id, "interval": horizon.model_dump(mode="json")} for r in rules],
    }


class DemoMaterialsProvider:
    """从登记元数据还原 PolicyMaterials（dev/demo 演示装配）。

    没有 run_day 写的材料元数据时退给 `fallback`（默认装配里是 B14 提供者），保持“只交预测、缺规则与
    成本”的原有转人工行为；有元数据时用完整材料，走门控的自动/人工确认。"""

    def __init__(self, fallback=None) -> None:
        self.fallback = fallback

    def __call__(self, plan: Plan, metadata: Mapping[str, object]) -> PolicyMaterials:
        raw_request = metadata.get("planning_request")
        if raw_request is None or "forecasts" not in metadata:
            return self.fallback(plan, metadata) if self.fallback is not None else PolicyMaterials()
        try:
            request = PlanningRequest.model_validate(raw_request)
            forecasts = tuple(Forecast.model_validate(item) for item in metadata.get("forecasts") or ())
            rules = tuple(Rule.model_validate(item) for item in metadata.get("rules") or ())
            plan_cost = metadata.get("plan_cost")
            baseline = metadata.get("baseline")
            return PolicyMaterials(
                request=request, forecasts=forecasts, rules=rules,
                dependencies_verified=bool(metadata.get("dependencies_verified", False)),
                rule_conflicts=tuple(metadata.get("rule_conflicts") or ()),
                verified_refs=frozenset(tuple(ref) for ref in metadata.get("verified_refs") or ()),
                plan_cost=CostEvidence(**_dict(plan_cost)) if plan_cost else None,
                baseline=BaselineEvidence(plan=Plan.model_validate(_dict(baseline)["plan"]),
                                          plant_id=str(_dict(baseline)["plant_id"]),
                                          cost=CostEvidence(**_dict(_dict(baseline)["cost"]))) if baseline else None,
                calibrations=tuple(CalibrationEvidence(**_dict(item)) for item in metadata.get("calibrations") or ()),
                rule_requirements=tuple(RuleRequirement(rule_id=str(_dict(item)["rule_id"]),
                                                        interval=Interval.model_validate(_dict(item)["interval"]))
                                        for item in metadata.get("rule_requirements") or ()),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, f"已登记的计划材料无效：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- 求解回执核验与执行依据


def verify_receipt(context: Context, receipt) -> bool:
    """同进程求解的回执：摘要完整、身份与请求一致即视为可信（演示装配，正式实现由 B15 的受信服务替换）。"""
    try:
        body = receipt.read()
        request = PlanningRequest.model_validate(body["request"])
        plan = Plan.model_validate(body["plan"])
    except (ValueError, KeyError, TypeError):
        return False
    return (request.context.plant_id == context.plant_id and plan.run_id == context.run_id
            and plan.version_manifest_id == context.version_manifest_id
            and body["solver"]["name"] == "highs")


def config_from_receipt(receipt) -> dict:
    """由回执里的包输入生成 validate_basis 需要的执行配置，并返回五段配置。"""
    body = receipt.read()
    request = PlanningRequest.model_validate(body["request"])
    packs = set(request.packs)
    evidence = body["pack_evidence"]
    export = {"limit_kw": float(body["export_limit_kw"]), "ref": body["export_ref"]}
    pv = None
    if "pv" in packs:
        source = evidence["pv"]["config"]
        pv = {"ref": source["config_ref"], "entity_id": source["entity_id"], "capacity_kw": float(source["kwp"])}
    storage = None
    if "storage" in packs:
        source = evidence["storage"]["config"]
        storage = {key: source[key] for key in ("entity_id", "capacity_kwh", "charge_kw", "discharge_kw", "eta_c", "eta_d",
                                                "soc_min", "soc_max")}
        storage["ref"] = source["config_ref"]
        storage["reserve_ratio"] = float(evidence["storage"]["parameters"][request.context.version_manifest_id]["reserve_ratio"])
    if "production" in packs:
        operations = {"complete": True, "items": [_operation_item(item)
                                                  for item in evidence["production"]["operations"]]}
    else:
        operations = {"complete": True, "items": []}
    tariff_source = evidence["tariff"]
    tariff_rules = [Rule.model_validate(item) for item in body["rules_by_pack"].get("tariff", ())]
    time_set = _slots(request.horizon, request.step_minutes)
    prices = price_schedule(tariff_rules, tariff_source["base_prices"], time_set)
    tariff = {"unit": "CNY/kWh", "ref": tariff_source["config_ref"],
              "slots": [{"start": slot.start.isoformat(), "end": slot.end.isoformat(), "price_cny_per_kwh": p.price_cny_per_kwh}
                        for slot, p in zip(time_set, prices)]}
    return {"pv": pv, "storage": storage, "export": export, "tariff": tariff, "operations": operations}


def _operation_item(source: Mapping) -> dict:
    profile = [source["rated_kw"] * value for value in source["shape"]["fractions"]]
    return {"operation_id": source["operation_id"], "entity_id": source["entity_id"],
            "original_start": source["original_start"], "profile_kw": profile}


def _slots(horizon: Interval, step_minutes: int) -> list[Interval]:
    step = timedelta(minutes=step_minutes)
    count = int((horizon.end - horizon.start) / step)
    return [Interval(start=horizon.start + i * step, end=horizon.start + (i + 1) * step) for i in range(count)]


def config_digest(config: Mapping) -> str:
    return hashlib.sha256(canonical(dict(config)).encode("utf-8")).hexdigest()


class DemoConfigResolver:
    """执行依据的配置解析：从已持久化回执还原配置；回执缺包输入时失败关闭。"""

    def __call__(self, context: Context, plan: Plan, receipt) -> tuple[dict, str]:
        try:
            config = config_from_receipt(receipt)
        except (KeyError, TypeError, ValueError) as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, f"求解回执缺少执行配置：{type(exc).__name__}: {exc}") from exc
        return config, config_digest(config)


def demo_source_verifier(context: Context, plan: Plan, receipt, config: Mapping, source_digest: str) -> bool:
    """来源核验（演示）：摘要格式正确、回执与计划一致；正式环境须核对固定清单里的受信配置。"""
    return len(source_digest) == 64 and receipt.read()["plan"]["plan_id"] == plan.plan_id


# ---------------------------------------------------------------- 其他演示材料


def scenario_calibrations(forecasts: Sequence[Forecast], *, reliability: float = 0.97) -> tuple[CalibrationEvidence, ...]:
    return tuple(CalibrationEvidence(forecast_id=f.forecast_id, model_version=f.model_version, reliability=reliability,
                                     event="scenario_persistence", calibration_version="b19-demo-calibration-1",
                                     validation_population="synthetic scenario day", provider="b19-demo",
                                     source="scenario band ±8%")
                 for f in forecasts)


def _dict(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"需要对象，实际 {type(value).__name__}")
