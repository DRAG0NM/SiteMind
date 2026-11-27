"""production 领域包（B13）：单道不可抢占工序在允许窗口内整段平移。

每道待排工序只为能完整放进窗口的开始时段 s 建二进制 x[s]，Σx[s] = 1；工序负荷
load[t] = Σ_s x[s]·profile[t−s]（功率曲线卷积）作为 demand 交给 B09 的公共功率平衡。
- 有 operation_window 规则：窗口内任选开始时段；窗口放不下就建一条必然不成立的约束，让求解器证明不可行；
- 没有窗口规则：固定在未优化开始时段（不平移，B0/B1 基线即此口径）。
多工序、设备独占与交期由 B28 经 extensions 扩展，不改这里的单工序约束。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from plantkg.core.contracts import (
    ConstraintContribution,
    Context,
    ContributionReceipt,
    ErrorCode,
    ForecastBundle,
    Interval,
    PendingIssue,
    PlantkgError,
    Ref,
    Rule,
    StateSnapshot,
    Unit,
)
from plantkg.core.ports import ModelBuilder
from plantkg.packs.production.operation import OperationSpec, slot_index

PACK = "production"
WINDOW_KIND = "operation_window"
COMPILER_VERSION = "b13-window-1"
PLANT_TZ = "Asia/Shanghai"
SHAPE_STEP = timedelta(minutes=15)


def op_key(operation_id: str) -> str:
    """模型组件名只允许小写字母、数字与下划线：JOB1 → job1。"""
    return re.sub(r"[^a-z0-9_]", "_", operation_id.lower())


def window_constraint(operation_id: str) -> str:
    """窗口约束名只由 operation_id 决定：编译声明与实际建模用同一个键，entity 尾名不参与。"""
    return f"window_{op_key(operation_id)}"


class OperationWindowKind:
    """operation_window：工序必须在当地钟面 [lower, upper) 小时内整段完成（bounds 单位 h）。"""

    kind = WINDOW_KIND
    compiler_version = COMPILER_VERSION

    def __init__(self, operations: Sequence[OperationSpec]) -> None:
        self._operation_ids = {s.entity_id: s.operation_id for s in operations}

    def compile(self, rule: Rule, context: Context) -> ConstraintContribution | PendingIssue:
        def issue(reason: str, detail: str) -> PendingIssue:
            return PendingIssue(issue_id=f"production-{rule.rule_id}-{reason}", reason=reason, rule_id=rule.rule_id, detail=detail,
                                refs=(Ref(kind="rule", id=rule.rule_id),))

        operation_id = self._operation_ids.get(rule.subject_ids[0]) if len(rule.subject_ids) == 1 else None
        if operation_id is None:
            return issue("subject_unknown", f"窗口规则只能约束一道已配置的工序，实际对象 {list(rule.subject_ids)}")
        name = window_constraint(operation_id)
        if rule.unit != Unit.hour.value or rule.quantity != "operation_start":
            return issue("unit_unknown", f"窗口规则须为 operation_start、单位 h，实际 {rule.quantity} / {rule.unit}")
        b = rule.bounds
        if b is None or b.lower is None or b.upper is None or not 0 <= b.lower < b.upper <= 24:
            return issue("config_invalid", "窗口须给 0 ≤ lower < upper ≤ 24 的当地小时")
        try:
            ZoneInfo(rule.condition.get("tz", PLANT_TZ))
        except Exception:  # noqa: BLE001 - 时区名无效统一转待处理
            return issue("config_invalid", f"时区 {rule.condition.get('tz')!r} 无效")
        return ConstraintContribution(rule_id=rule.rule_id, pack=PACK, constraint_names=(name,), refs=self.refs(rule),
                                      compiler_version=COMPILER_VERSION)

    @staticmethod
    def refs(rule: Rule) -> tuple[Ref, ...]:
        """编译声明与实际约束共用的依据：规则本身，加它的受信配置来源。"""
        return (Ref(kind="rule", id=rule.rule_id),) + ((Ref(kind="config", id=rule.config_ref),) if rule.config_ref else ())


def feasible_starts(spec: OperationSpec, time_set: Sequence[Interval], rule: Rule) -> list[int]:
    """可行开始时段：开始时刻在规则生效期内的，须整段落在同一当地日 [lower, upper) 内且不越过 effective_to；
    开始时刻不在生效期内的不受这条规则约束。"""
    tz = ZoneInfo(rule.condition.get("tz", PLANT_TZ))
    out = []
    for s in range(len(time_set) - spec.slots + 1):
        start, end = time_set[s].start, time_set[s + spec.slots - 1].end
        day = datetime.combine(start.astimezone(tz).date(), time.min, tz)
        lo, hi = day + timedelta(hours=rule.bounds.lower), day + timedelta(hours=rule.bounds.upper)
        if not rule.applies_to(start) or (lo <= start and end <= hi and (rule.effective_to is None or end <= rule.effective_to)):
            out.append(s)
    return out


@dataclass(frozen=True)
class Placement:
    """一道工序在本次模型里的放置：候选开始时段与对应的 x 变量，供 B28 扩展加依赖、独占与交期。"""

    spec: OperationSpec
    starts: tuple[int, ...]
    start_var: Any


class OperationExtension(Protocol):
    """多工序扩展点（B28 实现，放在 production/multi/）：只读 placements，只经 builder 加本包约束，返回依据。"""

    def contribute(self, mb: ModelBuilder, placements: Mapping[str, Placement]) -> Sequence[Ref]: ...


@dataclass
class ProductionPack:
    operations: Sequence[OperationSpec]
    extensions: Sequence[OperationExtension] = ()
    name: str = PACK
    tool_specs: tuple = ()
    rule_kinds: tuple = field(init=False)

    def __post_init__(self) -> None:
        ids = [s.operation_id for s in self.operations]
        if len(set(ids)) != len(ids) or len({op_key(i) for i in ids}) != len(ids):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序编号重复：{ids}")
        self.rule_kinds = (OperationWindowKind(self.operations),)

    def contribute(self, mb: ModelBuilder, state: StateSnapshot, rules: Sequence[Rule], forecasts: ForecastBundle) -> ContributionReceipt:
        pending = state.pack_states.get(PACK, {}).get("pending_jobs")
        specs = [s for s in self.operations if pending is None or s.operation_id in pending]
        unknown = sorted(set(pending or ()) - {s.operation_id for s in self.operations})
        if unknown:  # 漏掉一道待排工序等于少算它的负荷
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"待排工序 {unknown} 没有配置")
        planned = {s.entity_id for s in specs}
        idle = sorted(r.rule_id for r in rules if r.subject_ids[0] not in planned)
        if idle:  # 规则已声明窗口约束，不建就会被 check_built 判错；直接说清原因
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"窗口规则 {idle} 指向的工序不在本次待排工序 {pending} 里")
        step = mb.time_set[0].end - mb.time_set[0].start
        if specs and step != SHAPE_STEP:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序形状按 15 分钟时段给出，规划步长为 {step}")
        names: dict[str, list[str]] = {"variables": [], "constraints": [], "balance_terms": []}
        refs: set[Ref] = set()
        placements: dict[str, Placement] = {}
        n = len(mb.time_set)
        for spec in specs:
            key = op_key(spec.operation_id)
            own = [r for r in rules if r.subject_ids == (spec.entity_id,)]
            if own:
                starts = sorted(set.intersection(*(set(feasible_starts(spec, mb.time_set, r)) for r in own)))
                rule_refs = [ref for r in own for ref in OperationWindowKind.refs(r)]
                cname, crefs = window_constraint(spec.operation_id), (*rule_refs, *spec.refs)
            else:
                orig = slot_index(mb.time_set, spec.original_start)
                if orig is None or orig + spec.slots > n:
                    raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {spec.operation_id} 无窗口规则，原开始 {spec.original_start} 不在规划区间内")
                starts, cname, crefs = [orig], f"start_once_{key}", spec.refs
            refs.update(crefs)
            if not starts:  # 窗口放不下整段工序：交给求解器证明不可行，冲突线索指回规则
                stuck = mb.add_var(f"no_start_{key}", "continuous", (0, 0), Unit.count, scalar=True)
                mb.add_constraint(cname, stuck == 1, crefs)
                names["variables"].append(f"{PACK}__no_start_{key}")
                names["constraints"].append(f"{PACK}__{cname}")
                continue
            profile = spec.profile_kw
            x = mb.add_var(f"start_{key}", "binary", (0, 1), Unit.count, index=starts, action=(spec.entity_id, "operation_start"))
            load = mb.add_var(f"load_{key}", "continuous", (0, None), Unit.kW, action=(spec.entity_id, "operation_power"))
            mb.add_constraint(cname, sum(x[s] for s in starts) == 1, crefs)
            mb.add_constraint(f"load_def_{key}", lambda t, x=x, load=load, starts=starts, profile=profile: load[t] == sum(
                x[s] * profile[t - s] for s in starts if 0 <= t - s < len(profile)), spec.refs)
            mb.register_balance("demand", load, f"operation_load_{key}")
            names["variables"] += [f"{PACK}__start_{key}", f"{PACK}__load_{key}"]
            names["constraints"] += [f"{PACK}__{cname}", f"{PACK}__load_def_{key}"]
            names["balance_terms"].append(f"operation_load_{key}")
            placements[spec.operation_id] = Placement(spec, tuple(starts), x)
        for ext in self.extensions:
            refs.update(ext.contribute(mb, placements))
        return ContributionReceipt(pack=PACK, variables=tuple(names["variables"]), constraints=tuple(names["constraints"]),
                                   balance_terms=tuple(names["balance_terms"]), refs=tuple(sorted(refs, key=lambda r: (r.kind, r.id))))
