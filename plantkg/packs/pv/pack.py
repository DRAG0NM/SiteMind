"""pv 领域包（B10）：光伏 使用 + 弃光 = 可发，按时段给出使用与弃光动作。

- 可发功率只取 B09 读好的 pv_available 日前预测（builder.pv_available）；B05 已把 PVGIS 缩放到目标装机 kW，本包不再缩放，
  可发超过装机即判上游缩放有误（重复缩放或单位错）并拒绝；
- 已审核的光伏边界（如并网点限发）用规则 pv_output_cap 表达：超出上限的功率记为限发 pv_limit（不是弃光），
  B08 账本的光伏可发取 预测 − pv_limit；
- 账本口径归 B08：本包只在 Plan 里给出 pv_use / pv_curtail 动作，pv_breakdown 按账本把光伏使用拆成直接利用、外送、充电，
  拆不清来源的时段不归给光伏（不声称储能电量全为光伏）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyomo.environ as pyo

from plantkg.core.contracts import (
    LEDGER_TOLERANCE,
    ConstraintContribution,
    Context,
    ContributionReceipt,
    ErrorCode,
    ForecastBundle,
    Interval,
    Ledger,
    PendingIssue,
    PlantkgError,
    Ref,
    Rule,
    SourceBundle,
    StateSnapshot,
    Unit,
)
from plantkg.core.ports import ModelBuilder
from plantkg.packs._validation import is_number, read_yaml

PACK = "pv"
CAP_KIND = "pv_output_cap"
COMPILER_VERSION = "b10-cap-1"
USE_QUANTITY, CURTAIL_QUANTITY, LIMIT_QUANTITY = "pv_use", "pv_curtail", "pv_limit"  # 弃光动作量与 B09 _ledger_rules、B08 计划账本一致
PV_SOURCE_QUANTITY = "pv_ac_power"  # B05 PvgisAdapter 输出的规范化量
ZERO_KW = LEDGER_TOLERANCE["power_zero_kw"]
_ENTITY = re.compile(r"^entity://[A-Za-z0-9_-]+/pv/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PvConfig:
    """一组光伏阵列的受信配置：目标装机 kWp 来自装配清单，标 configured。"""

    entity_id: str
    kwp: float
    config_ref: str  # config://{文件}#{键路径}

    def __post_init__(self) -> None:
        if not _ENTITY.match(self.entity_id):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置项 pv.entity_id={self.entity_id!r} 须是 entity://{{厂区}}/pv/{{编号}}")
        if not is_number(self.kwp) or self.kwp <= 0:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置项 pv.kwp={self.kwp!r} 必须是正有限数（bool 不算数值）")
        Ref(kind="config", id=self.config_ref)  # 不是 config:// 直接报错

    @property
    def ref(self) -> Ref:
        return Ref(kind="config", id=self.config_ref)


_PV_KEYS = {"entity_id", "kwp"}


def load_pv_config(path: Path, config_id: str) -> PvConfig:
    """读光伏配置：缺文件、缺段、未知键、bool/非正装机都报 DOMAIN_INVALID，文本指向配置项。"""
    data = read_yaml(path, config_id)
    section = data.get("pv")
    if not isinstance(section, dict):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 没有 pv 配置段")
    unknown = sorted(set(section) - _PV_KEYS)
    if unknown:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 的 pv 段有未知配置项 {unknown}")
    return PvConfig(entity_id=section.get("entity_id"), kwp=section.get("kwp"), config_ref=f"config://{config_id}#pv")


class PvOutputCapKind:
    """pv_output_cap：已审核的光伏输出上限（kW，bounds.upper），生效时段内 使用 + 弃光 ≤ 上限，超出部分记为限发 pv_limit。"""

    kind = CAP_KIND
    compiler_version = COMPILER_VERSION

    def __init__(self, entity_id: str) -> None:
        self._entity = entity_id

    def compile(self, rule: Rule, context: Context) -> ConstraintContribution | PendingIssue:
        def issue(reason: str, detail: str) -> PendingIssue:
            return PendingIssue(issue_id=f"pv-{rule.rule_id}-{reason}", reason=reason, rule_id=rule.rule_id, detail=detail,
                                refs=(Ref(kind="rule", id=rule.rule_id),))

        if rule.subject_ids != (self._entity,):
            return issue("subject_unknown", f"限发规则只能约束光伏 {self._entity}，实际对象 {list(rule.subject_ids)}")
        if rule.unit != Unit.kW.value or rule.quantity != "pv_output":
            return issue("unit_unknown", f"限发规则须为 pv_output、单位 kW，实际 {rule.quantity} / {rule.unit}")
        b = rule.bounds
        if b is None or b.upper is None or b.lower is not None or not (math.isfinite(b.upper) and b.upper >= 0):
            return issue("config_invalid", "限发规则只给非负有限的 bounds.upper（kW）")
        return ConstraintContribution(rule_id=rule.rule_id, pack=PACK, constraint_names=(cap_constraint(rule),),
                                      refs=(Ref(kind="rule", id=rule.rule_id),), compiler_version=COMPILER_VERSION)


def cap_constraint(rule: Rule) -> str:
    return "output_cap_" + re.sub(r"[^a-z0-9_]", "_", rule.rule_id.lower())


@dataclass
class PvPack:
    config: PvConfig
    name: str = PACK
    tool_specs: tuple = ()

    @property
    def rule_kinds(self) -> tuple:
        return (PvOutputCapKind(self.config.entity_id),)

    def contribute(self, mb: ModelBuilder, state: StateSnapshot, rules: Sequence[Rule], forecasts: ForecastBundle) -> ContributionReceipt:
        avail = getattr(mb, "pv_available", None)
        if avail is None:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "启用了光伏包，但请求里没有 pv_available 预测；不能把光伏当 0 规划")
        check_within_kwp(avail, self.config.kwp, "pv_available 预测")
        cfg = self.config
        use = mb.add_var("use", "continuous", (0, None), Unit.kW, action=(cfg.entity_id, USE_QUANTITY))
        cut = mb.add_var("curtail", "continuous", (0, None), Unit.kW, action=(cfg.entity_id, CURTAIL_QUANTITY))
        variables, constraints, refs = [f"{PACK}__use", f"{PACK}__curtail"], [f"{PACK}__conservation"], {cfg.ref}
        if not rules:
            mb.add_constraint("conservation", lambda t: use[t] + cut[t] == avail[t], [cfg.ref])
        else:
            # 限发不是弃光：被规则拿掉的功率按常数记为 pv_limit，B08 账本的光伏可发 = 预测 − pv_limit，
            # 弃光只剩经济性弃光，“弃光同时购电”的核对仍然成立
            starts = [slot.start for slot in mb.time_set]
            caps = [min((r.bounds.upper for r in rules if r.applies_to(s)), default=math.inf) for s in starts]
            rule_refs = [Ref(kind="rule", id=r.rule_id) for r in rules]
            limit = mb.add_var("limit", "continuous", (0, None), Unit.kW, action=(cfg.entity_id, LIMIT_QUANTITY))
            mb.add_constraint("limit_def", lambda t: limit[t] == max(avail[t] - caps[t], 0.0), [*rule_refs, cfg.ref])
            mb.add_constraint("conservation", lambda t: use[t] + cut[t] + limit[t] == avail[t], [cfg.ref])
            for rule, rref in zip(rules, rule_refs):  # 规则可能在规划区间中途生效或失效，逐时段判断
                cap = rule.bounds.upper
                mb.add_constraint(cap_constraint(rule), lambda t, cap=cap, rule=rule: use[t] + cut[t] <= cap if rule.applies_to(starts[t])
                                  else pyo.Constraint.Skip, [rref, cfg.ref])
                constraints.append(f"{PACK}__{cap_constraint(rule)}")
            variables.append(f"{PACK}__limit")
            constraints.append(f"{PACK}__limit_def")
            refs.update(rule_refs)
        mb.register_balance("supply", use, "pv_used")
        return ContributionReceipt(pack=PACK, variables=tuple(variables), constraints=tuple(constraints),
                                   balance_terms=("pv_used",), refs=tuple(sorted(refs, key=lambda r: (r.kind, r.id))))


def check_within_kwp(values: Sequence[float | None], kwp: float, label: str) -> None:
    """可发 kW 须是非负有限数且不超过装机 kWp：超出说明 PVGIS 被重复缩放或单位错（W 当 kW）。缺测 None 由调用方处理。"""
    bad = [(i, v) for i, v in enumerate(values) if v is not None and not (math.isfinite(v) and v >= -ZERO_KW)]
    if bad:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{label} 时段 {bad[0][0]} 为 {bad[0][1]!r}，不是非负有限数")
    over = [(i, v) for i, v in enumerate(values) if v is not None and v > kwp * (1 + 1e-9) + ZERO_KW]
    if over:
        i, v = over[0]
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{label} 时段 {i} 为 {v:.6g} kW，超过装机 {kwp} kWp：上游缩放或单位有误，本包不再缩放")


def pv_actual_kw(bundle: SourceBundle, time_set: Sequence[Interval], as_of: datetime, kwp: float) -> list[float | None]:
    """把 B07 已按可见性取回的 PVGIS 规范化记录对到时段上，给执行账本当光伏可发实际值（kW）。

    不另做时间过滤，只做守卫：落在这些时段里的记录，在 as_of 还不可见、单位不是 kW、性质不是 derived、区间不在时段网格上、
    重复或超过装机，都直接拒绝；时段外的记录不看；缺测返回 None，不当 0。
    """
    starts = {slot.start for slot in time_set}
    lo, hi = time_set[0].start, time_set[-1].end
    by_start: dict[datetime, float | None] = {}
    for r in bundle.records:
        if r.quantity != PV_SOURCE_QUANTITY or r.interval is None or r.interval.end <= lo or r.interval.start >= hi:
            continue
        if r.available_at > as_of:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"光伏记录 {r.event_time.isoformat()} 在 {as_of.isoformat()} 还不可见，不能使用")
        if r.unit != Unit.kW.value or r.nature != "derived":
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"光伏记录须为 derived kW（B05 PVGIS 口径），实际 {r.nature} {r.unit}")
        if r.interval.start not in starts or r.interval.end - r.interval.start != time_set[0].end - time_set[0].start:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"光伏记录 {r.event_time.isoformat()} 的区间不在规划时段网格上")
        if r.interval.start in by_start:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"光伏记录 {r.interval.start.isoformat()} 重复")
        by_start[r.interval.start] = r.value
    values = [by_start.get(slot.start) for slot in time_set]
    check_within_kwp(values, kwp, "光伏实际")
    return values


# ---------------------------------------------------------------- 账本拆分


def pv_breakdown(ledger: Ledger) -> dict[str, float]:
    """按 B08 账本把光伏拆成 可发 = 直接利用 + 充电 + 外送 + 未归属 + 弃光（kWh）。限发已在账本可发之外（见 pv_limit 动作）。

    光伏先供本时段负荷（直接利用）；剩下的光伏只在本时段没有购电、没有放电时才能确定去了充电与外送，
    否则记为未归属：账本不记录电量来源，就不把这部分电量（包括储能里的电量）算成光伏。
    """
    zero = LEDGER_TOLERANCE["energy_abs_kwh"]
    out = dict.fromkeys(("pv_available", "pv_direct", "pv_to_charge", "pv_to_export", "pv_unattributed", "pv_curtailed"), 0.0)
    for e in ledger.entries:
        direct = min(e.pv_used_kwh, e.load_kwh)
        rest = e.pv_used_kwh - direct
        out["pv_available"] += e.pv_available_kwh
        out["pv_curtailed"] += e.pv_curtailed_kwh
        out["pv_direct"] += direct
        to_charge = to_export = 0.0
        if e.grid_import_kwh <= zero and e.discharge_kwh <= zero:
            # 唯一供给就是光伏：多出的部分就是充电 + 外送（差额只剩容差量级，留在未归属里，各项都不为负）
            to_charge = min(e.charge_kwh, rest)
            to_export = min(e.grid_export_kwh, rest - to_charge)
        out["pv_to_charge"] += to_charge
        out["pv_to_export"] += to_export
        out["pv_unattributed"] += rest - to_charge - to_export
    return out
