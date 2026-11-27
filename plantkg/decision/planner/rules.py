"""规则编译登记（B09）：按 kind 找到包注册的 RuleKind，先筛可见、已审核、在规划区间内生效的规则，再编译。

编译器只声明会生成哪些约束（ConstraintContribution），真正的约束由该包在 contribute 里建。
check_built 在所有包贡献完后核对：声明的约束确实在模型里、属于该包、带着这条规则的 Ref；对不上就是包的缺陷。
不能编译的规则一律变成 PendingIssue 返回，不静默丢弃。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from plantkg.core.contracts import (
    ConstraintContribution,
    Context,
    ErrorCode,
    Interval,
    PendingIssue,
    PlantkgError,
    Ref,
    Rule,
)
from plantkg.core.ports import DomainPack, RuleKind


@dataclass
class CompiledRules:
    by_pack: dict[str, list[Rule]] = field(default_factory=dict)
    contributions: list[ConstraintContribution] = field(default_factory=list)
    pending: list[PendingIssue] = field(default_factory=list)


class RuleCompilerRegistry:
    def __init__(self, packs: Mapping[str, DomainPack]) -> None:
        self._kinds: dict[str, tuple[str, RuleKind]] = {}
        for pack_name, pack in packs.items():
            for kind in pack.rule_kinds:
                if kind.kind in self._kinds:
                    raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"规则类型 {kind.kind} 被 {self._kinds[kind.kind][0]} 与 {pack_name} 重复注册")
                self._kinds[kind.kind] = (pack_name, kind)

    def owner(self, kind: str) -> str | None:
        entry = self._kinds.get(kind)
        return entry[0] if entry else None

    def compile(self, rules: Sequence[Rule], context: Context, horizon: Interval, active_packs: Sequence[str]) -> CompiledRules:
        out = CompiledRules(by_pack={name: [] for name in active_packs})
        for rule in rules:
            issue = self._screen(rule, context, horizon, active_packs)
            if issue == "skip":
                continue
            if issue is not None:
                out.pending.append(issue)
                continue
            pack_name, kind = self._kinds[rule.kind]
            result = kind.compile(rule, context)
            if isinstance(result, PendingIssue):
                out.pending.append(result)
                continue
            if result.rule_id != rule.rule_id or result.pack != pack_name:
                raise PlantkgError(ErrorCode.INTERNAL, f"{kind.kind} 编译 {rule.rule_id} 返回了 {result.rule_id}@{result.pack}")
            out.contributions.append(result)
            out.by_pack[pack_name].append(rule)
        return out

    def _screen(self, rule: Rule, context: Context, horizon: Interval, active_packs: Sequence[str]) -> PendingIssue | str | None:
        def issue(reason: str, detail: str) -> PendingIssue:
            return PendingIssue(issue_id=f"planner-{rule.rule_id}-{reason}", reason=reason, rule_id=rule.rule_id, detail=detail)

        if rule.effective_from >= horizon.end or (rule.effective_to is not None and rule.effective_to <= horizon.start):
            return "skip"  # 不在规划区间内生效：不是问题，只是不适用
        if not rule.is_visible(context.as_of):
            return issue("not_visible", f"规则 {rule.rule_id} 在 {context.as_of.isoformat()} 尚不可见")
        if not rule.compilable:
            return issue("not_reviewed", f"规则 {rule.rule_id} 复核状态 {rule.review_status} 不可编译")
        owner = self.owner(rule.kind)
        if owner is None:
            return issue("unsupported_kind", f"规则类型 {rule.kind} 没有包注册编译器")
        if owner not in active_packs:
            return issue("unsupported_kind", f"规则 {rule.rule_id} 归 {owner} 包，但本次规划未启用该包")
        return None


def priced_rules(rules: Sequence[Rule], as_of: datetime, horizon: Interval) -> tuple[Rule, ...]:
    """账本与展示入口共用的电价规则过滤：只留可见、可编译且与区间重叠的 tariff_tou 规则。

    与 RuleCompilerRegistry 的筛选口径一致；看不到或未复核的规则不参与定价，它们与已批准规则的重叠
    不应让合法最优计划在 price_schedule 处报“多条覆盖”。"""
    return tuple(rule for rule in rules if rule.kind == "tariff_tou"
                 and rule.effective_from < horizon.end
                 and (rule.effective_to is None or rule.effective_to > horizon.start)
                 and rule.is_visible(as_of) and rule.compilable)


def check_built(compiled: CompiledRules, constraints: Mapping[str, object], model: Any) -> list[str]:
    """声明的约束必须由同一个包建出、带这条规则的 Ref，且至少有一条实际约束（全部 Skip 等于没建）。
    constraints 为 builder.state.constraints，model 为共享模型。"""
    errors: list[str] = []
    for contrib in compiled.contributions:
        ref = Ref(kind="rule", id=contrib.rule_id)
        for name in contrib.constraint_names:
            info = constraints.get(f"{contrib.pack}__{name}")
            if info is None:
                errors.append(f"规则 {contrib.rule_id} 声明的约束 {contrib.pack}__{name} 没有建进模型")
            elif ref not in info.refs:  # type: ignore[attr-defined]
                errors.append(f"约束 {contrib.pack}__{name} 没有带规则 {contrib.rule_id} 的 Ref")
            elif len(model.component(f"{contrib.pack}__{name}")) == 0:
                errors.append(f"规则 {contrib.rule_id} 的约束 {contrib.pack}__{name} 一条也没有（全部跳过），规则等于没生效")
    return errors
