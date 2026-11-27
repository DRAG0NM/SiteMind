"""独立电量账本（B08）：账本的唯一权威计算。

不读优化变量、不取求解器目标值：给定每时段的物理量（光伏可发、负荷、充放、外送、电价），按 03 §2.4 重算

    光伏使用 + 购电 + 放电 = 负荷 + 充电 + 外送        （交流侧，非负量）
    光伏使用 + 弃光 = 光伏可发
    E[t+1] = E[t] + η_c·P_c·Δt − P_d·Δt/η_d

输入功率一律 kW（时段平均），电量 kWh = kW × 时段小时数；SOC 为 0～1。容差用契约 LEDGER_TOLERANCE（07 Q16）。
B0/B1/B2 三种策略、计划账本与执行账本都走同一套函数。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from plantkg.core.contracts import (
    LEDGER_TOLERANCE,
    ErrorCode,
    Interval,
    Ledger,
    LedgerEntry,
    LoadCategory,
    PlantkgError,
    StorageParams,
)

CALCULATOR_VERSION = "ledger-calc-1"
# 计划账本费用与模型目标值的比对容差（B08 登记，冻结测试前不得改）
COST_TOLERANCE = {"policy_version": "ledger-cost-tol-1", "abs_cny": 0.01, "rel": 1e-6}
_ZERO_KW = LEDGER_TOLERANCE["power_zero_kw"]


@dataclass(frozen=True)
class SlotInput:
    """一个时段的物理量（kW 为时段平均功率）。reported_* 是计划或执行方报出的值，账本拿来复核，不直接采信。"""

    interval: Interval
    pv_available_kw: float
    load_kw: float  # 已做工序平移替换后的总负荷（见 replace_shifted_load）
    charge_kw: float = 0.0
    discharge_kw: float = 0.0
    grid_export_kw: float = 0.0
    pv_curtailed_kw: float | None = None  # 计划/执行指定的弃光；为空按“光伏优先”推导
    price_cny_per_kwh: float | None = None  # 缺价不当 0
    reported_grid_import_kw: float | None = None
    reported_energy_end_kwh: float | None = None  # 执行器报出的期末电量（kWh），与重算 SOC 递推核对
    load_breakdown_kw: Mapping[str, float] | None = None  # v0.8 负荷分项（LoadCategory → kW），合计应等于 load_kw

    @property
    def hours(self) -> float:
        return (self.interval.end - self.interval.start) / timedelta(hours=1)


@dataclass(frozen=True)
class LedgerMeta:
    ledger_id: str
    kind: str  # plan | execution
    run_id: str
    plan_id: str
    plan_revision: int
    version_manifest_id: str
    source_refs: tuple[str, ...]


@dataclass
class LedgerResult:
    """compute_ledger 的结果：契约账本 + 计算过程中发现、但契约字段放不下的细节。"""

    ledger: Ledger
    problems: list[str] = field(default_factory=list)
    energy_kwh: dict[str, float] = field(default_factory=dict)  # 各项总电量，便于对照与报告


def _check_finite(slot_index: int, **values: float | None) -> None:
    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"slot {slot_index} 的 {name} 不是有限数")
        if value is not None and value < -_ZERO_KW:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"slot {slot_index} 的 {name}={value} 为负；账本只收非负量")


def _clip_zero(value: float) -> float:
    return 0.0 if abs(value) <= _ZERO_KW else value


def _power(name: str, value: float) -> float:
    """单个非负功率：有限性检查后把容差内的负值截零。"""
    if not math.isfinite(value):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{name} 不是有限数：{value!r}")
    if value < -_ZERO_KW:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{name}={value} 为负；账本只收非负量")
    return _clip_zero(value)


def _finite_value(name: str, value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{name} 不是有限数：{value!r}")
    return value


def make_slot_input(
    interval: Interval, *, pv_available_kw: float, load_kw: float, pv_limit_kw: float = 0.0,
    charge_kw: float = 0.0, discharge_kw: float = 0.0, grid_export_kw: float = 0.0,
    pv_curtailed_kw: float | None = None, price_cny_per_kwh: float | None = None,
    reported_grid_import_kw: float | None = None, reported_energy_end_kwh: float | None = None,
    load_breakdown_kw: Mapping[str, float] | None = None,
) -> SlotInput:
    """计划账本等入口统一构造 SlotInput：功率逐个截零并检查有限性，光伏可发扣除限发 pv_limit。

    求解器在容差内会给出 -1e-9 这类负功率，先截零再乘时长，避免负 kWh 触发契约的 ge=0 校验；
    超过容差的负值或非有限值直接按域错误拒绝，不静默丢。"""
    available = _power("pv_available_kw", pv_available_kw) - _power("pv_limit_kw", pv_limit_kw)
    if available < -_ZERO_KW:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"限发 {pv_limit_kw} kW 超过光伏可发 {pv_available_kw} kW")
    return SlotInput(
        interval=interval, pv_available_kw=max(available, 0.0), load_kw=_power("load_kw", load_kw),
        charge_kw=_power("charge_kw", charge_kw), discharge_kw=_power("discharge_kw", discharge_kw),
        grid_export_kw=_power("grid_export_kw", grid_export_kw),
        pv_curtailed_kw=None if pv_curtailed_kw is None else _power("pv_curtailed_kw", pv_curtailed_kw),
        price_cny_per_kwh=_finite_value("price_cny_per_kwh", price_cny_per_kwh),
        reported_grid_import_kw=None if reported_grid_import_kw is None else _power("reported_grid_import_kw", reported_grid_import_kw),
        reported_energy_end_kwh=None if reported_energy_end_kwh is None else _power("reported_energy_end_kwh", reported_energy_end_kwh),
        load_breakdown_kw=load_breakdown_kw,
    )


def compute_ledger(
    slots: Sequence[SlotInput],
    meta: LedgerMeta,
    storage: StorageParams | None,
    energy_start_kwh: float = 0.0,
    objective_value_cny: float | None = None,
    objective_terms: Mapping[str, float] | None = None,
) -> LedgerResult:
    """逐时段重算账本。问题写进 violations，balanced 只在没有任何问题时为 True。

    objective_terms 是模型目标里的**非购电**成本项（如储能衰减、固定费）；计划账本按
    “购电费用 + objective_terms 合计 = 模型目标值”核对，不再拿只有购电的 total_cost 直接比全部成本项。"""
    if not slots:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "账本至少需要一个时段")
    if storage is None and energy_start_kwh:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "无储能参数时初始电量必须为 0")
    if not math.isfinite(energy_start_kwh) or energy_start_kwh < 0:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"初始电量 {energy_start_kwh} 必须是非负有限数")
    if objective_value_cny is not None and not math.isfinite(objective_value_cny):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"模型目标值 {objective_value_cny} 必须是非负有限数")
    if objective_terms is not None and any(not math.isfinite(value) for value in objective_terms.values()):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "模型成本项含非有限值，不能计入计划账本")
    if storage is not None:
        if storage.soc_min > storage.soc_max:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"SOC 下限 {storage.soc_min} 大于上限 {storage.soc_max}")
        if energy_start_kwh > storage.capacity_kwh:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"初始电量 {energy_start_kwh} 超过容量 {storage.capacity_kwh}")
    if meta.kind not in ("plan", "execution"):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"账本类型 {meta.kind!r} 只能是 plan 或 execution")
    if not meta.source_refs:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "账本必须注明来源（预测 ID 或实测来源包 ID）")
    _check_grid(slots)

    problems: list[str] = []
    if storage is not None:
        soc_start = energy_start_kwh / storage.capacity_kwh
        if not (storage.soc_min - LEDGER_TOLERANCE["soc_abs"] <= soc_start <= storage.soc_max + LEDGER_TOLERANCE["soc_abs"]):
            problems.append(f"初始 SOC {soc_start:.6f} 超出 [{storage.soc_min}, {storage.soc_max}]")  # 不让首段充放把它掩盖
    entries: list[LedgerEntry] = []
    totals = dict.fromkeys(("pv_available", "pv_used", "pv_curtailed", "grid_import", "grid_export", "charge", "discharge", "load"), 0.0)
    energy = energy_start_kwh
    total_cost = 0.0
    cost_known = True
    peak_kw = 0.0

    for i, s in enumerate(slots):
        _check_finite(i, pv_available_kw=s.pv_available_kw, load_kw=s.load_kw, charge_kw=s.charge_kw, discharge_kw=s.discharge_kw,
                      grid_export_kw=s.grid_export_kw, pv_curtailed_kw=s.pv_curtailed_kw, price_cny_per_kwh=s.price_cny_per_kwh,
                      reported_grid_import_kw=s.reported_grid_import_kw, reported_energy_end_kwh=s.reported_energy_end_kwh)
        h = s.hours
        # 求解器容差内的负功率先截零，再参与物理平衡与电量计算
        pv_available, load = _clip_zero(s.pv_available_kw), _clip_zero(s.load_kw)
        charge, discharge, export = _clip_zero(s.charge_kw), _clip_zero(s.discharge_kw), _clip_zero(s.grid_export_kw)
        curtailed = None if s.pv_curtailed_kw is None else _clip_zero(s.pv_curtailed_kw)
        reported_import = None if s.reported_grid_import_kw is None else _clip_zero(s.reported_grid_import_kw)
        if charge > 0 and discharge > 0:
            problems.append(f"slot {i} 同时充放（充 {charge} kW、放 {discharge} kW）")
        if storage is None and (charge > 0 or discharge > 0):
            problems.append(f"slot {i} 没有储能却有充放")

        demand_kw = load + charge + export
        if curtailed is None:
            pv_used_kw = min(pv_available, max(demand_kw - discharge, 0.0))  # 光伏优先
        else:
            pv_used_kw = pv_available - curtailed
            if pv_used_kw < -_ZERO_KW:
                problems.append(f"slot {i} 弃光 {curtailed} kW 超过可发 {pv_available} kW")
        pv_used_kw = max(pv_used_kw, 0.0)
        derived_import_kw = demand_kw - discharge - pv_used_kw
        if derived_import_kw < -_ZERO_KW:
            problems.append(f"slot {i} 供给超过需求 {-derived_import_kw:.6g} kW（放电或光伏无处可去，外送上限不足）")
        import_kw = reported_import if reported_import is not None else max(derived_import_kw, 0.0)
        curtailed_kw = max(pv_available - pv_used_kw, 0.0)
        if curtailed_kw * h > LEDGER_TOLERANCE["energy_abs_kwh"] and import_kw * h > LEDGER_TOLERANCE["energy_abs_kwh"]:
            # 外送上限内光伏多余才弃；一边弃光一边购电自相矛盾（弃光同时放电可由执行削减产生，合法）
            problems.append(f"slot {i} 弃光 {curtailed_kw:.6g} kW 的同时购电 {import_kw:.6g} kW")

        kwh = {
            "pv_available": pv_available * h, "pv_used": pv_used_kw * h,
            "pv_curtailed": max(pv_available - pv_used_kw, 0.0) * h, "grid_import": import_kw * h,
            "grid_export": export * h, "charge": charge * h, "discharge": discharge * h, "load": load * h,
        }
        residual = kwh["pv_used"] + kwh["grid_import"] + kwh["discharge"] - kwh["load"] - kwh["charge"] - kwh["grid_export"]

        start_energy = energy
        if storage is not None:
            energy = start_energy + storage.eta_c * kwh["charge"] - kwh["discharge"] / storage.eta_d
            soc = energy / storage.capacity_kwh
            if not (storage.soc_min - LEDGER_TOLERANCE["soc_abs"] <= soc <= storage.soc_max + LEDGER_TOLERANCE["soc_abs"]):
                problems.append(f"slot {i} SOC {soc:.6f} 超出 [{storage.soc_min}, {storage.soc_max}]")
            soc_end = min(max(soc, 0.0), 1.0)
            energy_out = max(energy, 0.0)
        else:
            soc_end, energy_out = 0.0, 0.0
        reported_end = s.reported_energy_end_kwh
        if reported_end is not None:
            if storage is not None and reported_end > storage.capacity_kwh + LEDGER_TOLERANCE["energy_abs_kwh"]:
                problems.append(f"slot {i} 报出期末电量 {reported_end:.6g} kWh 超过容量 {storage.capacity_kwh}")
            expected_end = energy if storage is not None else 0.0  # 无储能时期末电量只能是 0
            if abs(reported_end - expected_end) > LEDGER_TOLERANCE["energy_abs_kwh"]:
                problems.append(f"slot {i} 报出期末电量 {reported_end:.6g} kWh 与重算 {expected_end:.6g} kWh 不符")

        if s.price_cny_per_kwh is None:
            cost = None
            cost_known = False
            problems.append(f"slot {i} 缺电价，费用无法核算")
        else:
            cost = kwh["grid_import"] * s.price_cny_per_kwh
            total_cost += cost
        peak_kw = max(peak_kw, import_kw)
        for key, value in kwh.items():
            totals[key] += value

        breakdown = None
        if s.load_breakdown_kw is not None:
            breakdown = {k: v * h for k, v in s.load_breakdown_kw.items()}
            gap = sum(breakdown.values()) - kwh["load"]
            bad_value = any(not math.isfinite(v) or v < 0 for v in breakdown.values())
            unknown = set(breakdown) - {c.value for c in LoadCategory}
            if bad_value or unknown or abs(gap) > max(LEDGER_TOLERANCE["energy_abs_kwh"], LEDGER_TOLERANCE["energy_rel"] * kwh["load"]):
                problems.append(f"slot {i} 负荷分项之和与负荷相差 {gap:.6g} kWh（或含负值、未知分项 {sorted(unknown)}），分项不入账")
                breakdown = None
        entry = LedgerEntry(
            interval=s.interval, pv_available_kwh=kwh["pv_available"], pv_used_kwh=kwh["pv_used"],
            pv_curtailed_kwh=kwh["pv_curtailed"], grid_import_kwh=kwh["grid_import"], grid_export_kwh=kwh["grid_export"],
            charge_kwh=kwh["charge"], discharge_kwh=kwh["discharge"], load_kwh=kwh["load"],
            energy_start_kwh=max(start_energy, 0.0), energy_end_kwh=energy_out, soc_end=soc_end,
            price_cny_per_kwh=s.price_cny_per_kwh, cost_cny=cost, residual_kwh=residual, load_breakdown=breakdown,
        )
        if not entry.energy_within_tolerance():
            problems.append(f"slot {i} 能量残差 {residual:.6g} kWh 超出容差（报出的购电与重算不符）")
        entries.append(entry)

    objective_gap = None
    if objective_value_cny is not None and cost_known:
        # 模型目标是“购电费用 + 非购电成本项”；total_cost 只含购电，先把另列项加回来再比
        nonpurchase = sum(objective_terms.values()) if objective_terms else 0.0
        objective_gap = total_cost + nonpurchase - objective_value_cny
        allowed = max(COST_TOLERANCE["abs_cny"], COST_TOLERANCE["rel"] * abs(objective_value_cny))
        if meta.kind == "plan" and abs(objective_gap) > allowed:
            problems.append(f"计划账本成本项合计与模型目标相差 {objective_gap:.4f} 元，超出 {allowed:.4f}")

    fields = dict(
        ledger_id=meta.ledger_id, kind=meta.kind, run_id=meta.run_id, plan_id=meta.plan_id, plan_revision=meta.plan_revision,
        version_manifest_id=meta.version_manifest_id, entries=tuple(entries), total_cost_cny=total_cost if cost_known else None,
        peak_import_kw=peak_kw, tolerance_policy_version=LEDGER_TOLERANCE["policy_version"], calculator_version=CALCULATOR_VERSION,
        source_refs=meta.source_refs, storage=storage, objective_gap_cny=objective_gap,
        objective_value_cny=objective_value_cny, objective_terms=dict(objective_terms) if objective_terms is not None else None,
    )
    # 兜底：契约层复核查得出的问题一并记入，绝不在判平后才由契约抛异常
    draft = Ledger(**fields, balanced=False, violations=tuple(problems) or ("待复核",))
    for problem in draft.recheck():
        if problem not in problems:
            problems.append(problem)
    ledger = Ledger(**fields, balanced=not problems, violations=tuple(problems))
    return LedgerResult(ledger=ledger, problems=problems, energy_kwh=totals)


def _check_grid(slots: Sequence[SlotInput]) -> None:
    """时段必须等长、首尾相接：不同步长混在一起说明上游重采样有误。"""
    step = slots[0].interval.end - slots[0].interval.start
    for i in range(1, len(slots)):
        prev, cur = slots[i - 1].interval, slots[i].interval
        if cur.start != prev.end:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"slot {i} 与上一时段不连续")
        if cur.end - cur.start != step:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"slot {i} 时长与第一个时段不同")


def validate_balance(ledger: Ledger, objective_value_cny: float | None = None,
                     objective_terms: Mapping[str, float] | None = None) -> list[str]:
    """复核一份已有账本（例如执行器或规划器交来的）：契约层复核 + 费用按电价另算。返回问题清单。

    账本自带的 violations 原样保留；balanced=False 的账本绝不返回空清单。
    objective_value_cny 给出时（计划账本），按“购电费用 + objective_terms（非购电成本项）合计 = 模型目标”
    核对；未显式给出时用账本记录的目标值与非购电项。"""
    problems = list(ledger.recheck())
    zero = LEDGER_TOLERANCE["energy_abs_kwh"]
    for i, e in enumerate(ledger.entries):
        if e.price_cny_per_kwh is None:
            problems.append(f"entries[{i}] 缺电价，费用无法核算")
        if e.pv_curtailed_kwh > zero and e.grid_import_kwh > zero:
            problems.append(f"entries[{i}] 弃光 {e.pv_curtailed_kwh:.6g} kWh 的同时购电 {e.grid_import_kwh:.6g} kWh")
    if ledger.storage is not None and ledger.entries:
        soc_start = ledger.entries[0].energy_start_kwh / ledger.storage.capacity_kwh
        if not (ledger.storage.soc_min - LEDGER_TOLERANCE["soc_abs"] <= soc_start <= ledger.storage.soc_max + LEDGER_TOLERANCE["soc_abs"]):
            problems.append(f"初始 SOC {soc_start:.6f} 越界")
    target = objective_value_cny if objective_value_cny is not None else ledger.objective_value_cny
    terms = objective_terms if objective_terms is not None else ledger.objective_terms
    if target is not None and ledger.kind == "plan":
        if ledger.total_cost_cny is None:
            problems.append("缺电价，无法与模型目标比对")
        else:
            nonpurchase = sum(terms.values()) if terms else 0.0
            gap = ledger.total_cost_cny + nonpurchase - target
            allowed = max(COST_TOLERANCE["abs_cny"], COST_TOLERANCE["rel"] * abs(target))
            if abs(gap) > allowed:
                problems.append(f"计划账本成本项合计与模型目标相差 {gap:.4f} 元，超出 {allowed:.4f}")
            if ledger.objective_gap_cny is not None and abs(ledger.objective_gap_cny - gap) > COST_TOLERANCE["abs_cny"]:
                problems.append(f"账本记录的 objective_gap_cny {ledger.objective_gap_cny} 与重算 {gap:.4f} 不符")
    total = 0.0
    known = True
    for i, e in enumerate(ledger.entries):
        if e.price_cny_per_kwh is None:
            known = False
            continue
        expected = e.grid_import_kwh * e.price_cny_per_kwh
        if e.cost_cny is None or abs(e.cost_cny - expected) > COST_TOLERANCE["abs_cny"]:
            problems.append(f"entries[{i}] 费用 {e.cost_cny} 与 购电×电价 {expected:.4f} 不符")
        total += expected
    if known and ledger.total_cost_cny is not None and abs(ledger.total_cost_cny - total) > COST_TOLERANCE["abs_cny"]:
        problems.append(f"总费用 {ledger.total_cost_cny} 与逐时段之和 {total:.4f} 不符")
    if ledger.entries:
        peak = max(e.grid_import_kwh / ((e.interval.end - e.interval.start) / timedelta(hours=1)) for e in ledger.entries)
        if abs(peak - ledger.peak_import_kw) > LEDGER_TOLERANCE["energy_abs_kwh"]:
            problems.append(f"峰值 {ledger.peak_import_kw} kW 与重算 {peak:.4f} kW 不符")
    problems += [v for v in ledger.violations if v not in problems]
    if not ledger.balanced and not problems:
        problems.append("账本标记为不平但未给出原因")
    return problems


# ---------------------------------------------------------------- 工序平移


@dataclass(frozen=True)
class ShiftedOperation:
    """一道可平移工序：设备分表曲线（kW，每个时段一个值）从 original_start 时段移到 new_start 时段。"""

    operation_id: str
    profile_kw: tuple[float, ...]
    original_start: int
    new_start: int

    @property
    def energy_kwh_per_hour(self) -> float:
        return sum(self.profile_kw)


@dataclass(frozen=True)
class ShiftReport:
    total_kw: tuple[float, ...]
    residual_kw: tuple[float, ...]  # 扣除全部工序原曲线后的不可平移负荷
    energy_before_kwh: float
    energy_after_kwh: float
    operation_energy_kwh: dict[str, float]


def replace_shifted_load(total_kw: Sequence[float], operations: Sequence[ShiftedOperation], slot_hours: float) -> ShiftReport:
    """总表扣除各工序原时段曲线、再加到新时段（03 §2.4，07 Q25）。

    扣除后任一时段为负，说明分表不属于总表或重复计量，拒绝；工序超出时间范围同样拒绝。
    返回新总负荷；总电量与每道工序电量在平移前后守恒（只换时段，不增减产量）。
    """
    n = len(total_kw)
    tol = LEDGER_TOLERANCE["energy_abs_kwh"]
    if not math.isfinite(slot_hours) or slot_hours <= 0:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"slot_hours={slot_hours} 必须是正有限数")
    ids = [op.operation_id for op in operations]
    if len(ids) != len(set(ids)):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "同一工序不能平移两次")
    residual = [float(v) for v in total_kw]
    for i, v in enumerate(residual):
        if not math.isfinite(v) or v < -_ZERO_KW:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"总表 slot {i} 值 {v} 非法")
    for op in operations:
        length = len(op.profile_kw)
        for start in (op.original_start, op.new_start):
            if start < 0 or start + length > n:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {op.operation_id} 起点 {start} 放不进 {n} 个时段")
        if any(not math.isfinite(p) or p < 0 for p in op.profile_kw):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {op.operation_id} 曲线含负值或非有限数")
        for k, p in enumerate(op.profile_kw):
            residual[op.original_start + k] -= p
    negative = [(i, v) for i, v in enumerate(residual) if v * slot_hours < -tol]
    if negative:
        i, v = negative[0]
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"扣除工序原曲线后 slot {i} 残余 {v:.4f} kW 为负：分表不属于总表或重复计量")
    residual = [max(v, 0.0) for v in residual]
    shifted = list(residual)
    for op in operations:
        for k, p in enumerate(op.profile_kw):
            shifted[op.new_start + k] += p
    before = sum(total_kw) * slot_hours
    after = sum(shifted) * slot_hours
    # 逐段允许 tol 以内的微小负残余并截为 0，守恒容差按时段数累计，两处口径一致
    if abs(before - after) > max(tol * (n + 1), LEDGER_TOLERANCE["energy_rel"] * before):
        raise PlantkgError(ErrorCode.INTERNAL, f"平移前后总电量不守恒：{before:.6f} → {after:.6f} kWh")
    return ShiftReport(
        total_kw=tuple(shifted), residual_kw=tuple(residual), energy_before_kwh=before, energy_after_kwh=after,
        operation_energy_kwh={op.operation_id: sum(op.profile_kw) * slot_hours for op in operations},
    )


# ---------------------------------------------------------------- 查询摘要（ledger 工具 / REST 的数据）


def ledger_summary(ledger: Ledger) -> dict:
    """给 ledger 工具与工作台的摘要：≤ 8KB，不含逐时段明细（明细走分页）。"""
    def total(attr: str) -> float:
        return round(sum(getattr(e, attr) for e in ledger.entries), 6)

    return {
        "ledger_id": ledger.ledger_id, "kind": ledger.kind, "run_id": ledger.run_id, "plan_id": ledger.plan_id,
        "plan_revision": ledger.plan_revision, "balanced": ledger.balanced, "violations": list(ledger.violations[:10]),
        "violation_count": len(ledger.violations), "slots": len(ledger.entries),
        "total_cost_cny": ledger.total_cost_cny, "peak_import_kw": ledger.peak_import_kw,
        "energy_kwh": {k: total(f"{k}_kwh") for k in ("pv_available", "pv_used", "pv_curtailed", "grid_import", "charge", "discharge", "load")},
        "soc_end": ledger.entries[-1].soc_end, "calculator_version": ledger.calculator_version,
        "tolerance_policy_version": ledger.tolerance_policy_version,
    }
