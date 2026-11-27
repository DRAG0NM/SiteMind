"""storage 领域包（B11）：储能能量递推、SOC/功率/期末边界、充放互斥与备用比例 reserve_ratio。

- 容量、功率、效率、SOC 范围是有来源的受信配置（config_ref，configured），求解前校验，不合格即拒绝；
- 充放互斥一律用二进制 u[t]（ADR-0001），不靠“电价为正时同时充放不划算”的经验；
- 符号口径与 B08 账本相同：充放功率为非负 kW，E[t] = E[t−1] + η_c·P_c·Δt − P_d·Δt/η_d，E 为时段末电量；
- reserve_ratio 是版本清单里的参数（按 version_manifest_id 取值），包只校验和使用，不自行发布候选；
  备用线 E ≥ 容量 × (soc_min + reserve_ratio × (soc_max − soc_min))；
- 衰减成本只在配置明确给出单价时才进目标。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyomo.environ as pyo

from plantkg.core.contracts import (
    LEDGER_TOLERANCE,
    ConstraintContribution,
    Context,
    ContributionReceipt,
    ErrorCode,
    ForecastBundle,
    ParamSpec,
    PendingIssue,
    PlantkgError,
    Ref,
    Rule,
    StateSnapshot,
    StorageParams,
    Unit,
)
from plantkg.core.ports import ModelBuilder
from plantkg.packs._validation import is_number, read_yaml

PACK = "storage"
SOC_KIND, POWER_KIND = "storage_soc_window", "storage_power_limit"
COMPILER_VERSION = "b11-storage-1"
CHARGE_QUANTITY, DISCHARGE_QUANTITY = "charge_power", "discharge_power"
RESERVE_SPEC = ParamSpec(name="reserve_ratio", type="number", minimum=0.0, maximum=0.5,
                         description="备用比例：在 SOC 可用区间内保留给备用的份额，0 表示不留备用")
# 工程上下界（configured）：单程效率覆盖锂电、铅酸、抽水蓄能；衰减成本常见 0.02～0.5 元/kWh，上界取十倍。
# 更极端的值会让模型到能量平衡/期末约束才失败，错误与配置无关，所以在求解前拒绝。
ETA_RANGE = (0.5, 1.0)
DEGRADATION_MAX_CNY_PER_KWH = 2.0
_ENTITY = re.compile(r"^entity://[A-Za-z0-9_-]+/battery/[A-Za-z0-9_.-]+$")


def _issue(key: str, detail: str, config_ref: str | None) -> PendingIssue:
    refs = (Ref(kind="config", id=config_ref),) if config_ref and config_ref.startswith("config://") else ()
    return PendingIssue(issue_id=f"storage-config-{key}", reason="config_invalid", subject=key, detail=detail, refs=refs)


def validate_storage_config(data: Mapping[str, Any], config_ref: str | None) -> list[PendingIssue]:
    """储能配置的求解前校验，每个问题一条 config_invalid。返回空表才可以建模。"""
    issues = []
    if not (config_ref or "").startswith("config://"):
        issues.append(_issue("config_ref", f"储能配置必须注明受信来源 config://…，实际 {config_ref!r}", None))
    if not _ENTITY.match(str(data.get("entity_id", ""))):
        issues.append(_issue("entity_id", f"储能对象 {data.get('entity_id')!r} 须是 entity://{{厂区}}/battery/{{编号}}", config_ref))
    for key in ("capacity_kwh", "charge_kw", "discharge_kw"):
        if not (is_number(data.get(key)) and data[key] > 0):
            issues.append(_issue(key, f"{key}={data.get(key)!r} 必须是正有限数", config_ref))
    for key in ("eta_c", "eta_d"):
        if not (is_number(data.get(key)) and ETA_RANGE[0] <= data[key] <= ETA_RANGE[1]):
            issues.append(_issue(key, f"{key}={data.get(key)!r} 必须在工程范围 [{ETA_RANGE[0]}, {ETA_RANGE[1]}]（configured）", config_ref))
    lo, hi = data.get("soc_min"), data.get("soc_max")
    if not (is_number(lo) and is_number(hi) and 0 <= lo < hi <= 1):
        issues.append(_issue("soc", f"SOC 范围 [{lo!r}, {hi!r}] 须满足 0 ≤ soc_min < soc_max ≤ 1", config_ref))
    end = data.get("end_soc")
    if end is not None and not (is_number(end) and is_number(lo) and is_number(hi) and lo <= end <= hi):
        issues.append(_issue("end_soc", f"期末 SOC {end!r} 须在 [soc_min, soc_max] 内", config_ref))
    cost = data.get("degradation_cny_per_kwh")
    if cost is not None and not (is_number(cost) and 0 <= cost <= DEGRADATION_MAX_CNY_PER_KWH):
        issues.append(_issue("degradation_cny_per_kwh",
                             f"degradation_cny_per_kwh={cost!r} 须在工程范围 [0, {DEGRADATION_MAX_CNY_PER_KWH}] 内（configured）", config_ref))
    unknown = sorted(set(data) - {"entity_id", "capacity_kwh", "charge_kw", "discharge_kw", "eta_c", "eta_d", "soc_min", "soc_max",
                                  "end_soc", "degradation_cny_per_kwh"})
    if unknown:
        issues.append(_issue("unknown", f"未知配置项 {unknown}（拼错的键不能被静默忽略）", config_ref))
    return issues


@dataclass(frozen=True)
class StorageConfig:
    """一组储能的受信配置。end_soc=None 表示期末电量不低于初始电量；degradation_cny_per_kwh=None 表示不计衰减成本。"""

    entity_id: str
    capacity_kwh: float
    charge_kw: float
    discharge_kw: float
    eta_c: float
    eta_d: float
    soc_min: float
    soc_max: float
    config_ref: str
    end_soc: float | None = None
    degradation_cny_per_kwh: float | None = None

    def __post_init__(self) -> None:
        data = {k: v for k, v in self.__dict__.items() if k != "config_ref"}
        issues = validate_storage_config(data, self.config_ref)
        if issues:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "储能配置无效，求解前拒绝：" + "；".join(i.detail for i in issues))

    @property
    def ref(self) -> Ref:
        return Ref(kind="config", id=self.config_ref)

    def ledger_params(self) -> StorageParams:
        """B08 账本复核 SOC 用的参数，与模型同一组数。"""
        return StorageParams(capacity_kwh=self.capacity_kwh, eta_c=self.eta_c, eta_d=self.eta_d, soc_min=self.soc_min, soc_max=self.soc_max)

    def reserve_floor_kwh(self, reserve_ratio: float) -> float:
        return self.capacity_kwh * (self.soc_min + reserve_ratio * (self.soc_max - self.soc_min))


def load_storage_config(path: Path, config_id: str) -> StorageConfig:
    data = read_yaml(path, config_id).get("storage")
    if not isinstance(data, dict):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} 没有 storage 配置段")
    config_ref = f"config://{config_id}#storage"
    issues = validate_storage_config(data, config_ref)
    if issues:  # 缺键、拼错的键也走同一条拒绝路径，不抛裸 TypeError
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "储能配置无效，求解前拒绝：" + "；".join(i.detail for i in issues))
    return StorageConfig(**data, config_ref=config_ref)


# ---------------------------------------------------------------- reserve_ratio


def validate_reserve_ratio(value: Any) -> PendingIssue | None:
    if is_number(value) and RESERVE_SPEC.minimum <= value <= RESERVE_SPEC.maximum:
        return None
    return PendingIssue(issue_id="storage-reserve-ratio", reason="config_invalid", subject="reserve_ratio",
                        detail=f"reserve_ratio={value!r} 须是 [{RESERVE_SPEC.minimum}, {RESERVE_SPEC.maximum}] 内的有限数")


def reserve_ratio_range(config: StorageConfig, energy_start_kwh: float, step_hours: float) -> tuple[float, float]:
    """候选 reserve_ratio 的可行域：备用线不能高于首个时段满功率充电后能到的电量，也不超出参数范围。

    这是给 B18 生成候选用的上界提示，只看首个时段和本包自身的约束（不含期末目标与 SOC 规则）；包不发布候选。
    """
    reachable = min(energy_start_kwh + config.eta_c * config.charge_kw * step_hours, config.soc_max * config.capacity_kwh)
    usable = config.capacity_kwh * (config.soc_max - config.soc_min)
    upper = (reachable - config.soc_min * config.capacity_kwh) / usable
    if upper < RESERVE_SPEC.minimum:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"初始电量 {energy_start_kwh} kWh 一个时段内到不了 SOC 下限，任何备用比例都不可行")
    return RESERVE_SPEC.minimum, min(upper, RESERVE_SPEC.maximum)


# ---------------------------------------------------------------- 规则


class _StorageRuleKind:
    kind = ""
    quantity, unit = "", ""
    compiler_version = COMPILER_VERSION

    def __init__(self, entity_id: str) -> None:
        self._entity = entity_id

    def compile(self, rule: Rule, context: Context) -> ConstraintContribution | PendingIssue:
        def issue(reason: str, detail: str) -> PendingIssue:
            return PendingIssue(issue_id=f"storage-{rule.rule_id}-{reason}", reason=reason, rule_id=rule.rule_id, detail=detail,
                                refs=(Ref(kind="rule", id=rule.rule_id),))

        if rule.subject_ids != (self._entity,):
            return issue("subject_unknown", f"{self.kind} 只能约束储能 {self._entity}，实际对象 {list(rule.subject_ids)}")
        if rule.unit != self.unit or rule.quantity != self.quantity:
            return issue("unit_unknown", f"{self.kind} 须为 {self.quantity}、单位 {self.unit}，实际 {rule.quantity} / {rule.unit}")
        problem = self._bounds_problem(rule)
        if problem:
            return issue("config_invalid", problem)
        return ConstraintContribution(rule_id=rule.rule_id, pack=PACK, constraint_names=(rule_constraint(rule),),
                                      refs=(Ref(kind="rule", id=rule.rule_id),), compiler_version=COMPILER_VERSION)


class SocWindowKind(_StorageRuleKind):
    """storage_soc_window：已审核的 SOC 范围（0～1，可只给一侧），生效时段内时段末电量落在范围内。"""

    kind, quantity, unit = SOC_KIND, "soc", Unit.fraction.value

    def _bounds_problem(self, rule: Rule) -> str | None:
        b = rule.bounds
        if b is None or any(v is not None and not (math.isfinite(v) and 0 <= v <= 1) for v in (b.lower, b.upper)):
            return "SOC 范围须给 0～1 之间的 bounds"
        return None


class PowerLimitKind(_StorageRuleKind):
    """storage_power_limit：已审核的充放功率上限（kW，bounds.upper），生效时段内 充电 + 放电 ≤ 上限（两者互斥，即各自 ≤ 上限）。"""

    kind, quantity, unit = POWER_KIND, "power", Unit.kW.value

    def _bounds_problem(self, rule: Rule) -> str | None:
        b = rule.bounds
        if b is None or b.upper is None or b.lower is not None or not (math.isfinite(b.upper) and b.upper >= 0):
            return "功率上限只给非负有限的 bounds.upper（kW）"
        return None


def rule_constraint(rule: Rule) -> str:
    prefix = "soc_window_" if rule.kind == SOC_KIND else "power_limit_"
    return prefix + re.sub(r"[^a-z0-9_]", "_", rule.rule_id.lower())


# ---------------------------------------------------------------- 包


@dataclass
class StoragePack:
    """parameters：version_manifest_id → 该版本的储能参数（目前只有 reserve_ratio）；params_config_id 为参数文件，用于依据引用。"""

    config: StorageConfig
    parameters: Mapping[str, Mapping[str, float]]
    params_config_id: str
    name: str = PACK
    tool_specs: tuple = ()
    rule_kinds: tuple = field(init=False)

    def __post_init__(self) -> None:
        for manifest, params in self.parameters.items():
            if not isinstance(params, Mapping) or set(params) != {"reserve_ratio"}:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"版本清单 {manifest} 的储能参数只能有 reserve_ratio，实际 {params!r}")
            issue = validate_reserve_ratio(params.get("reserve_ratio"))
            if issue:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"版本清单 {manifest} 的参数无效：{issue.detail}")
        # 只读副本：基准与候选参数只能经版本清单切换，不能在包实例上就地改
        self.parameters = MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self.parameters.items()})
        self.rule_kinds = (SocWindowKind(self.config.entity_id), PowerLimitKind(self.config.entity_id))

    def reserve_ratio(self, version_manifest_id: str) -> float:
        params = self.parameters.get(version_manifest_id)
        if params is None:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"版本清单 {version_manifest_id} 没有储能参数，不能用默认值代替")
        return params["reserve_ratio"]

    def contribute(self, mb: ModelBuilder, state: StateSnapshot, rules: Sequence[Rule], forecasts: ForecastBundle) -> ContributionReceipt:
        cfg = self.config
        e0 = energy_start(state, cfg)
        ratio = self.reserve_ratio(state.version_manifest_id)
        param_ref = Ref(kind="config", id=f"config://{self.params_config_id}#{state.version_manifest_id}.reserve_ratio")
        n = len(mb.time_set)
        hours = [(slot.end - slot.start) / timedelta(hours=1) for slot in mb.time_set]
        starts = [slot.start for slot in mb.time_set]
        ref = [cfg.ref]

        pc = mb.add_var("charge", "continuous", (0, cfg.charge_kw), Unit.kW, action=(cfg.entity_id, CHARGE_QUANTITY))
        pd = mb.add_var("discharge", "continuous", (0, cfg.discharge_kw), Unit.kW, action=(cfg.entity_id, DISCHARGE_QUANTITY))
        u = mb.add_var("is_charging", "binary", (0, 1), Unit.fraction)
        e = mb.add_var("energy", "continuous", (cfg.soc_min * cfg.capacity_kwh, cfg.soc_max * cfg.capacity_kwh), Unit.kWh)
        mb.add_constraint("charge_excl", lambda t: pc[t] <= cfg.charge_kw * u[t], ref)
        mb.add_constraint("discharge_excl", lambda t: pd[t] <= cfg.discharge_kw * (1 - u[t]), ref)
        mb.add_constraint("energy_balance", lambda t: e[t] == (e0 if t == 0 else e[t - 1]) + cfg.eta_c * pc[t] * hours[t]
                          - pd[t] * hours[t] / cfg.eta_d, ref)
        floor = cfg.reserve_floor_kwh(ratio)
        mb.add_constraint("reserve_floor", lambda t: e[t] >= floor, [param_ref, cfg.ref])
        end = e0 if cfg.end_soc is None else cfg.end_soc * cfg.capacity_kwh
        mb.add_constraint("energy_end", e[n - 1] >= end, ref)
        names = ["charge_excl", "discharge_excl", "energy_balance", "reserve_floor", "energy_end"]

        refs = {cfg.ref, param_ref}
        for rule in rules:  # 规则可能在规划区间中途生效或失效，逐时段判断
            rref = Ref(kind="rule", id=rule.rule_id)
            if rule.kind == SOC_KIND:
                lo, hi = rule.bounds.lower, rule.bounds.upper
                expr = _soc_window(e, cfg.capacity_kwh, lo, hi)
            elif rule.kind == POWER_KIND:
                cap = rule.bounds.upper
                expr = lambda t, cap=cap: pc[t] + pd[t] <= cap  # noqa: E731
            mb.add_constraint(rule_constraint(rule), lambda t, rule=rule, expr=expr: expr(t) if rule.applies_to(starts[t])
                              else pyo.Constraint.Skip, [rref, cfg.ref])
            names.append(rule_constraint(rule))
            refs.add(rref)

        cost_terms = ()
        if cfg.degradation_cny_per_kwh is not None:
            price = cfg.degradation_cny_per_kwh
            mb.register_cost(lambda t: price * (pc[t] + pd[t]) * hours[t], "degradation")
            cost_terms = ("degradation",)
        mb.register_balance("supply", pd, "discharge")
        mb.register_balance("demand", pc, "charge")
        return ContributionReceipt(
            pack=PACK, variables=tuple(f"{PACK}__{v}" for v in ("charge", "discharge", "is_charging", "energy")),
            constraints=tuple(f"{PACK}__{c}" for c in names), balance_terms=("discharge", "charge"), cost_terms=cost_terms,
            refs=tuple(sorted(refs, key=lambda r: (r.kind, r.id))))


def _soc_window(e: Any, capacity: float, lo: float | None, hi: float | None) -> Any:
    if lo is not None and hi is not None:
        return lambda t: pyo.inequality(lo * capacity, e[t], hi * capacity)
    if lo is not None:
        return lambda t: e[t] >= lo * capacity
    return lambda t: e[t] <= hi * capacity


def energy_start(state: StateSnapshot, config: StorageConfig) -> float:
    """初始电量只读快照（运行状态由执行器 B16 写入）。缺失、非有限或超出 SOC 范围（容差同 B08 账本 soc_abs）都拒绝，不猜默认值；
    容差内的越界截回范围，避免“账本认可、模型却不可行”。"""
    value = state.pack_states.get(PACK, {}).get("energy_kwh")
    lo, hi = config.soc_min * config.capacity_kwh, config.soc_max * config.capacity_kwh
    tol = LEDGER_TOLERANCE["soc_abs"] * config.capacity_kwh
    if not is_number(value) or not lo - tol <= value <= hi + tol:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"快照 {state.snapshot_id} 的储能初始电量 {value!r} 缺失或不在 SOC 范围 [{lo}, {hi}] kWh 内")
    return min(max(float(value), lo), hi)
