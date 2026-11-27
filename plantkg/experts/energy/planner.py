"""B19 共享规划调用（B09）+ B11 期末目标 + B08 计划账本。

一次日前/滚动规划要做的事：
1. 由 B18 装配装出包（storage / pv / tariff，有演示工序时加 production）与已审核的规则；
2. 期末电量**显式**取 `end_target_kwh / capacity` 传给 B11（`end_soc`），不用 `end_soc=None`——
   默认行为会把期末下界设为本次重解的初始电量，随实际 SOC 漂移；
3. `solve_with_receipt` 拿到求解回执（供门控的执行依据核验）；
4. 按 B08 `compute_ledger` 复算计划账本，不平就拒绝继续。

本模块不改 B09/B11/B13/B14 的内部行为；包、规则、快照、预测都从外面传进来。
"""
from __future__ import annotations

import dataclasses
import yaml
from pathlib import Path
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from plantkg.core.contracts import (
    Context,
    ErrorCode,
    ForecastBundle,
    Interval,
    Ledger,
    Plan,
    PlanningRequest,
    PlanResult,
    PlantkgError,
    Rule,
    SolverBudget,
    StateSnapshot,
)
from plantkg.core.ledger import LedgerMeta, compute_ledger, make_slot_input
from plantkg.decision.planner import SharedPlanner
from plantkg.decision.planner.receipt import SolveReceipt
from plantkg.knowledge.rules.normalise import entity_keys
from plantkg.decision.planner.rules import priced_rules
from plantkg.packs.production import OperationSpec, ProductionPack, load_operations
from plantkg.packs.pv import PvConfig, PvPack, load_pv_config
from plantkg.packs.storage import StorageConfig, StoragePack, load_storage_config
from plantkg.packs.tariff import SlotPrice, TariffPack, load_tariff_config, load_tariff_rules, price_schedule

STEP_MINUTES = 15
CHARGE, DISCHARGE, CURTAIL = "charge_power", "discharge_power", "pv_curtail"
DEFAULT_SOLVER_BUDGET = SolverBudget(time_limit_s=20.0, mip_rel_gap=1.0e-3, threads=1, random_seed=0)
EXPORT_LIMIT_KW = 0.0
EXPORT_REF = "config://configs/defaults.yaml#planning.export_limit_kw"
PURCHASE_COST_TERM = "tariff.energy_cost"  # 模型目标里的购电费用项；其余成本项由账本 objective_terms 另列
OPERATIONS_CONFIG = "configs/sites/factory/operations.yaml"
OPERATION_START, OPERATION_POWER = "operation_start", "operation_power"


@dataclass(frozen=True)
class ScenarioPacks:
    """一个场景日的领域包与规则。`config_id` 用剧本里的相对路径，作为受信配置来源。"""

    storage: StorageConfig
    pv: PvConfig
    tariff: TariffPack
    rules: tuple[Rule, ...]
    operations: tuple[OperationSpec, ...] = ()  # B13 待排工序（synthetic 演示配置）；规则里带它们的 operation_window
    equipment_code: str | None = None  # 世界里承接工序负荷的设备
    reserve_ratio: float = 0.0
    export_limit_kw: float = EXPORT_LIMIT_KW
    export_ref: str = EXPORT_REF

    @property
    def config_refs(self) -> frozenset[tuple[str, str]]:
        refs = {("config", self.storage.config_ref), ("config", self.pv.config_ref),
                ("config", self.tariff.config_ref), ("config", self.export_ref)}
        refs |= {("config", r.config_ref) for r in self.rules if r.config_ref}
        refs |= {("config", ref.id) for op in self.operations for ref in op.refs if ref.kind == "config"}
        return frozenset(refs)

    def applicable_rules(self, horizon: Interval) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.effective_from < horizon.end
                     and (r.effective_to is None or r.effective_to > horizon.start))


def load_scenario_packs(scenario) -> ScenarioPacks:
    storage = load_storage_config(scenario.resolve(scenario.sources.storage), scenario.sources.storage)
    pv = load_pv_config(scenario.resolve(scenario.sources.pv), scenario.sources.pv)
    tariff, _demo = load_tariff_config(scenario.resolve(scenario.sources.tariff_config), scenario.sources.tariff_config)
    rules = tuple(load_tariff_rules(scenario.resolve(scenario.sources.tariff_rules), scenario.sources.tariff_rules))
    operations, window_rules, equipment = load_scenario_operations(scenario)
    return ScenarioPacks(storage=storage, pv=pv, tariff=tariff, rules=rules + window_rules, operations=operations,
                         equipment_code=equipment)


def with_published_tariff(packs: ScenarioPacks, published: Sequence[Rule], horizon: Interval) -> ScenarioPacks:
    """仅以当前清单中适用的已发布电价替换同一接入点和类别的配置规则。"""
    grid = set(entity_keys(packs.tariff.grid_entity))

    def same_access(rule: Rule) -> bool:
        return (rule.kind == "tariff_tou"
                and bool(set(key for subject in rule.subject_ids for key in entity_keys(subject)) & grid)
                and rule.condition.get("category") == packs.tariff.category)

    matches = [rule for rule in published if same_access(rule)]
    if not matches:
        return packs
    if len(matches) != 1:
        raise PlantkgError(ErrorCode.STATE_CONFLICT, "已发布电价规则存在重叠")
    found = matches[0]
    if found.effective_from > horizon.start or (found.effective_to and found.effective_to < horizon.end):
        raise PlantkgError(ErrorCode.PRECONDITION, "已发布电价规则未覆盖整个重算区间")
    # 剔除配置规则与选中规则用同一归一口径；再把主体重写为场景配置的接入点，
    # 让 packs 侧（B12，不 import normalise）的原始比较通过（#175）
    selected = found.model_copy(update={"subject_ids": (packs.tariff.grid_entity,)})
    retained = tuple(rule for rule in packs.rules if not same_access(rule))
    return dataclasses.replace(packs, rules=retained + (selected,))


def published_tariff_price(scenario, published: Sequence[Rule], slot: Interval) -> float | None:
    tariff, _ = load_tariff_config(scenario.resolve(scenario.sources.tariff_config), scenario.sources.tariff_config)
    grid = set(entity_keys(tariff.grid_entity))
    matches = [rule for rule in published if rule.kind == "tariff_tou"
               and set(key for subject in rule.subject_ids for key in entity_keys(subject)) & grid
               and rule.condition.get("category") == tariff.category]
    if not matches:
        return None
    if len(matches) != 1:
        raise PlantkgError(ErrorCode.STATE_CONFLICT, "已发布电价规则存在重叠")
    rule = matches[0]
    if rule.effective_from > slot.start or (rule.effective_to and rule.effective_to < slot.end):
        raise PlantkgError(ErrorCode.PRECONDITION, "已发布电价规则未覆盖计价时段")
    return price_schedule([rule], tariff.base_prices, [slot])[0].price_cny_per_kwh


def load_scenario_operations(scenario) -> tuple[tuple[OperationSpec, ...], tuple[Rule, ...], str | None]:
    """读演示工序配置：开始时刻按剧本首日平移，每道工序配一条 operation_window 规则（trusted_config）。"""
    path = Path(scenario.resolve(OPERATIONS_CONFIG))
    if not path.exists():
        return (), (), None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    lo, hi = data["window_local"]
    operations, rules = [], []
    for spec in load_operations(path, OPERATIONS_CONFIG):
        local = spec.original_start.astimezone(scenario.tz)
        offset = local - local.replace(hour=0, minute=0, second=0, microsecond=0)
        spec = dataclasses.replace(spec, original_start=scenario.start + offset)
        operations.append(spec)
        rules.append(Rule(rule_id=f"rule-{spec.operation_id.lower()}-window", kind="operation_window", subject_ids=(spec.entity_id,),
                          condition={"local_window": [f"{lo:02d}:00", f"{hi:02d}:00"]}, quantity=OPERATION_START, unit="h",
                          bounds={"lower": lo, "upper": hi}, effective_from=scenario.start - timedelta(days=1),
                          available_at=scenario.start - timedelta(days=1),
                          config_ref=f"config://{OPERATIONS_CONFIG}#operations.{spec.operation_id}.window",
                          nature="synthetic", review_status="trusted_config"))
    return tuple(operations), tuple(rules), data.get("equipment_code")


@dataclass
class DayPlan:
    """一次规划的结果：计划、回执、B08 计划账本、逐时段价格与目标线。"""

    request: PlanningRequest
    plan: Plan
    result: PlanResult
    receipt: SolveReceipt
    ledger: Ledger
    prices: tuple[SlotPrice, ...]
    end_target_kwh: float
    end_floor_kwh: float  # 模型里 storage__energy_end 的下界（显式期末目标）
    elapsed_s: float


def time_set(horizon: Interval, step_minutes: int = STEP_MINUTES) -> list[Interval]:
    step = timedelta(minutes=step_minutes)
    count = int((horizon.end - horizon.start) / step)
    if count < 1 or horizon.start + count * step != horizon.end:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"规划区间 {horizon.start.isoformat()}～{horizon.end.isoformat()} 不是整 {step_minutes} 分钟")
    return [Interval(start=horizon.start + i * step, end=horizon.start + (i + 1) * step) for i in range(count)]


def build_packs(packs: ScenarioPacks, manifest_id: str, end_target_kwh: float,
                operations: Sequence[OperationSpec] = ()) -> dict:
    """构造包实例（有待排工序时加 production）。储能配置拷一份并写入显式 `end_soc`，原配置与 B11 行为都不改。"""
    end_soc = end_target_kwh / packs.storage.capacity_kwh
    lo, hi = packs.storage.soc_min, packs.storage.soc_max
    if not (lo <= end_soc <= hi):
        raise PlantkgError(ErrorCode.INVALID_REQUEST,
                           f"期末目标 {end_target_kwh:.1f} kWh 对应 SOC {end_soc:.3f}，不在 [{lo}, {hi}] 内")
    config = dataclasses.replace(packs.storage, end_soc=end_soc)
    params_config_id = "b19-scenario-storage-parameters"
    built = {
        "storage": StoragePack(config, {manifest_id: {"reserve_ratio": packs.reserve_ratio}},
                               params_config_id),
        "pv": PvPack(packs.pv),
        "tariff": packs.tariff,
    }
    if operations:
        built["production"] = ProductionPack(operations)
    return built


def reserve_param_ref(manifest_id: str) -> tuple[str, str]:
    """储能备用比例参数在模型里的受信配置来源（门控要核验动作引用的每个 config）。"""
    return ("config", f"config://b19-scenario-storage-parameters#{manifest_id}.reserve_ratio")


def plan_day(packs: ScenarioPacks, forecasts: ForecastBundle, snapshot: StateSnapshot, context: Context,
             horizon: Interval, *, energy_start_kwh: float, end_target_kwh: float,
             solver_budget: SolverBudget | None = None, operations: Sequence[OperationSpec] | None = None,
             fixed: frozenset[str] = frozenset()) -> DayPlan:
    """B09 规划 + B08 计划账本。求解状态不是 optimal 时拒绝（门控与执行都不接受非最优计划）。

    operations 是本次要排的工序（缺省取 packs.operations 全部）；fixed 里的工序已经开始，不再带窗口规则，
    B13 把没有窗口规则的工序固定在 original_start（重解时由调用方改成“本次区间起点 + 剩余形状”）。
    """
    operations = packs.operations if operations is None else tuple(operations)
    fixed_entities = {op.entity_id for op in operations if op.operation_id in fixed}
    rules = tuple(r for r in packs.applicable_rules(horizon) if r.subject_ids[0] not in fixed_entities)
    if not rules:
        raise PlantkgError(ErrorCode.PRECONDITION, f"规划区间 {horizon.start.isoformat()} 没有任何可用的电价规则")
    domain_packs = build_packs(packs, context.version_manifest_id, end_target_kwh, operations)
    if operations:
        snapshot = snapshot.model_copy(update={"pack_states": {**snapshot.pack_states, "production": {
            "pending_jobs": [op.operation_id for op in operations]}}})
    planner = SharedPlanner(domain_packs, lambda _sid: snapshot, lambda _ids: forecasts, lambda _ids: rules,
                            export_limit_kw=packs.export_limit_kw, export_ref=packs.export_ref)
    request = PlanningRequest(
        context=context, snapshot_id=snapshot.snapshot_id, forecast_ids=tuple(f.forecast_id for f in forecasts.forecasts),
        rule_ids=tuple(r.rule_id for r in rules), horizon=horizon, step_minutes=STEP_MINUTES,
        solver_budget=solver_budget or DEFAULT_SOLVER_BUDGET, packs=("storage", "tariff", "pv") + (("production",) if operations else ()), policy="B2")
    result, receipt = planner.solve_with_receipt(request)
    if result.solver_status != "optimal" or result.plan is None or receipt is None:
        detail = "；".join(result.validation_errors[:3]) or "；".join(result.conflict_hints[:3])
        raise PlantkgError(ErrorCode.INFEASIBLE if result.solver_status == "infeasible" else ErrorCode.PRECONDITION,
                           f"共享规划未得到最优解（{result.solver_status}）：{detail}")
    plan = result.plan
    prices = tuple(price_schedule(priced_rules(rules, context.as_of, horizon), packs.tariff.base_prices, time_set(horizon)))
    other_terms = {key: value for key, value in result.objective_terms.items() if key != PURCHASE_COST_TERM}
    ledger = plan_ledger(plan, forecasts, prices, packs.storage, energy_start_kwh,
                         objective_value_cny=result.objective_value, objective_terms=other_terms)
    floor = float(receipt.read().get("end_floor_kwh", 0.0))
    return DayPlan(request=request, plan=plan, result=result, receipt=receipt, ledger=ledger, prices=prices,
                   end_target_kwh=end_target_kwh, end_floor_kwh=floor, elapsed_s=result.elapsed_s)


def plan_ledger(plan: Plan, forecasts: ForecastBundle, prices: Sequence[SlotPrice], storage: StorageConfig,
                energy_start_kwh: float, objective_value_cny: float | None = None,
                objective_terms: Mapping[str, float] | None = None) -> Ledger:
    """按 B08 口径把 Plan 复算成计划账本（不读优化变量，不采信报出的费用）。

    统一经 make_slot_input 构造时段输入：功率截零、有限性检查、光伏可发扣除 pv_limit。"""
    slots = time_set(plan.valid_interval)
    by_quantity: dict[str, dict[datetime, float]] = {}
    for action in plan.actions:
        by_quantity.setdefault(action.quantity, {}).setdefault(action.interval.start, 0.0)
        by_quantity[action.quantity][action.interval.start] += float(action.value)
    base = _series(forecasts, "base_load", plan.valid_interval, len(slots))
    pv = _series(forecasts, "pv_available", plan.valid_interval, len(slots))
    inputs = []
    for i, slot in enumerate(slots):
        charge = by_quantity.get(CHARGE, {}).get(slot.start, 0.0)
        discharge = by_quantity.get(DISCHARGE, {}).get(slot.start, 0.0)
        curtailed = by_quantity.get(CURTAIL, {}).get(slot.start, 0.0)
        limit = by_quantity.get("pv_limit", {}).get(slot.start, 0.0)
        export = by_quantity.get("grid_export", {}).get(slot.start, 0.0)
        operation = by_quantity.get(OPERATION_POWER, {}).get(slot.start, 0.0)  # 工序负荷是 base 之外追加的
        inputs.append(make_slot_input(slot, pv_available_kw=pv[i], pv_limit_kw=limit, load_kw=base[i] + operation,
                                      charge_kw=charge, discharge_kw=discharge, grid_export_kw=export,
                                      pv_curtailed_kw=curtailed, price_cny_per_kwh=prices[i].price_cny_per_kwh))
    meta = LedgerMeta(ledger_id=f"plan:{plan.plan_id}:b08", kind="plan", run_id=plan.run_id, plan_id=plan.plan_id,
                      plan_revision=plan.revision, version_manifest_id=plan.version_manifest_id,
                      source_refs=tuple(plan.forecast_ids))
    result = compute_ledger(inputs, meta, storage.ledger_params(), energy_start_kwh,
                            objective_value_cny=objective_value_cny, objective_terms=objective_terms)
    if not result.ledger.balanced:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "计划账本不平，拒绝继续：" + "；".join(result.problems[:5]))
    return result.ledger


def compare_tariff_interpretations(packs_a: ScenarioPacks, packs_b: ScenarioPacks, forecasts: ForecastBundle,
                                   snapshot: StateSnapshot, context: Context, horizon: Interval, *,
                                   energy_start_kwh: float, end_target_kwh: float,
                                   solver_budget: SolverBudget | None = None) -> dict:
    """同一预测求解两种电价解释，再用 B 的价格及 B08 账本比较两份计划。"""
    if dataclasses.replace(packs_a, rules=()) != dataclasses.replace(packs_b, rules=()):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "比较仅允许电价规则不同，设备配置和工序须相同")
    non_tariff_a = tuple(rule for rule in packs_a.rules if rule.kind != "tariff_tou")
    non_tariff_b = tuple(rule for rule in packs_b.rules if rule.kind != "tariff_tou")
    if non_tariff_a != non_tariff_b:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "比较仅允许电价规则不同")
    if snapshot.as_of > context.as_of or any(f.issued_at > context.as_of or f.input_slice.visible_until > context.as_of
                                               for f in forecasts.forecasts):
        raise PlantkgError(ErrorCode.NOT_VISIBLE, "反事实评分不能使用截止时间后的快照或预测")
    if any(not rule.is_visible(context.as_of) for rule in (*packs_a.applicable_rules(horizon), *packs_b.applicable_rules(horizon))):
        raise PlantkgError(ErrorCode.NOT_VISIBLE, "反事实评分不能使用截止时间后可见的规则")
    a = plan_day(packs_a, forecasts, snapshot, context, horizon, energy_start_kwh=energy_start_kwh,
                 end_target_kwh=end_target_kwh, solver_budget=solver_budget)
    b = plan_day(packs_b, forecasts, snapshot, context, horizon, energy_start_kwh=energy_start_kwh,
                 end_target_kwh=end_target_kwh, solver_budget=solver_budget)
    a_under_b = plan_ledger(a.plan, forecasts, b.prices, packs_b.storage, energy_start_kwh, None)
    if a_under_b.total_cost_cny is None or b.ledger.total_cost_cny is None or b.result.objective_value is None:
        raise PlantkgError(ErrorCode.PRECONDITION, "反事实账本或目标值不完整")
    return {"status_a": a.result.solver_status, "status_b": b.result.solver_status,
            "cost_a_under_b": a_under_b.total_cost_cny, "cost_b_under_b": b.ledger.total_cost_cny,
            "objective_b": b.result.objective_value, "violates_b": False}


def _series(forecasts: ForecastBundle, quantity: str, horizon: Interval, count: int) -> list[float]:
    found = [f for f in forecasts.forecasts if f.quantity == quantity]
    if len(found) != 1:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"预测里 {quantity} 必须恰好一条，实际 {len(found)}")
    forecast = found[0]
    step = forecast.step_minutes * 60
    shift = (horizon.start - forecast.target.start).total_seconds() / step
    if shift != int(shift):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"预测 {quantity} 与规划区间错位")
    values = forecast.values[int(shift):int(shift) + count]
    if len(values) != count or any(v is None for v in values):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"预测 {quantity} 未覆盖规划区间")
    return [float(v) for v in values]


def baseline_cost(forecasts: ForecastBundle, prices: Sequence[SlotPrice], storage: StorageConfig,
                  energy_start_kwh: float, horizon: Interval, operations: Sequence[OperationSpec] = ()) -> float:
    """可比基线：同样的负荷、光伏与电价，储能不充不放、工序在原开始时刻（B0 口径）。"""
    slots = time_set(horizon)
    base = _series(forecasts, "base_load", horizon, len(slots))
    for op in operations:
        for k, kw in enumerate(op.profile_kw):
            i = int((op.original_start - horizon.start) / timedelta(minutes=STEP_MINUTES)) + k
            if 0 <= i < len(base):
                base[i] += kw
    pv = _series(forecasts, "pv_available", horizon, len(slots))
    inputs = [make_slot_input(slot, pv_available_kw=pv[i], load_kw=base[i], price_cny_per_kwh=prices[i].price_cny_per_kwh)
              for i, slot in enumerate(slots)]
    meta = LedgerMeta(ledger_id="plan:baseline", kind="plan", run_id="baseline", plan_id="baseline", plan_revision=0,
                      version_manifest_id="baseline", source_refs=("scenario-baseline",))
    result = compute_ledger(inputs, meta, storage.ledger_params(), energy_start_kwh)
    if result.ledger.total_cost_cny is None:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "基线缺电价，无法比较成本")
    return float(result.ledger.total_cost_cny)


def slot_powers(plan: Plan) -> dict[datetime, dict[str, float]]:
    """Plan 动作按槽汇总成分钟层要用的计划功率。"""
    out: dict[datetime, dict[str, float]] = {}
    for action in plan.actions:
        row = out.setdefault(action.interval.start, {"charge_kw": 0.0, "discharge_kw": 0.0, "curtail_kw": 0.0, "use_kw": 0.0,
                                                     "operation_kw": 0.0})
        key = {CHARGE: "charge_kw", DISCHARGE: "discharge_kw", CURTAIL: "curtail_kw", "pv_use": "use_kw",
               OPERATION_POWER: "operation_kw"}.get(action.quantity)
        if key:
            row[key] += float(action.value)
    return out


def slot_series(forecasts: ForecastBundle, horizon: Interval) -> Mapping[datetime, tuple[float, float]]:
    slots = time_set(horizon)
    base = _series(forecasts, "base_load", horizon, len(slots))
    pv = _series(forecasts, "pv_available", horizon, len(slots))
    return {slot.start: (base[i], pv[i]) for i, slot in enumerate(slots)}
