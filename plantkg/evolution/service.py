"""纠错 → 候选 → trial 回测 → 确认发布或回滚。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from plantkg.core.contracts import (
    Actor, BacktestReport, Candidate, CandidateKind, CandidateStatus, ComponentRef, Context,
    ErrorCode, EvolutionTimelineEntry, JobKind, Locator, ManifestComponent, Page, PendingAction, PlantkgError,
    PolicyConfig, ResponseBudget, Role, Rule, ToolResult,
)
from plantkg.core.ports import JobHandlerRegistrar, JobRunner, KnowledgePublisher, PendingActionStore, RuleMatcher, RunStore, VersionStore
from plantkg.evolution.artifacts import policy_artifacts, sensor_artifacts, skill_artifacts
from plantkg.evolution.store import EvolutionStore

log = logging.getLogger("plantkg.evolution")

BACKTEST_EVALUATOR_HOOK = "evolution.backtest_evaluator"
SKILL_PUBLISH_CHECK_HOOK = "evolution.skill_publish_check"  # B43：Skill 候选发布前的评估门
AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3")
HIGHER_IS_BETTER = ("target", "resolved", "safety", "fast", "score")
LOWER_IS_BETTER = ("capacity_violation_minutes", "cost_cny")
OBSERVABLE_COMPONENTS = ("rules",)  # 回测场景里按目标事件观测效果的组件；其余组件（policy 阈值、Skill、图谱、传感器案例等）只验证非退化
COST_TOLERANCE = 0.05  # 非目标剧本的电费容差：超过才按退化处理
MINING_RULES_PREFIX = "rules@b26-workorder-"  # B26 工单挖掘候选的规则引用；不是可加载产物，须先由 B30 结构化
REPO_ROOT = Path(__file__).resolve().parents[2]
HASH_VERSION = "b32-v4"  # 报告哈希算法版本：v4 覆盖候选 changes、场景内容、版本、回退次数与报告结论；b32-v2 及更早的历史报告不带绑定信息
DEV_HASH_VERSION = "b32-v3"  # 本分支开发期产物（哈希不含结论），不再接受，须重新回测
BACKTEST_VERSIONS_HOOK = "evolution.backtest_versions"  # 装配层登记：prompt / model 等影响回测结果的版本标识


def _canonical(value: Any) -> Any:
    """归一成与落库一致的 JSON 结构（datetime 等经 default=str 转文本），重算哈希才不会因类型差异误拒。"""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def report_artifact_hash(candidate: Candidate, binding: Mapping[str, Any], outcome: Mapping[str, Any] | None = None) -> str:
    """b32-v4 报告哈希：候选 changes 的 ref 与 content_hash、base_manifest_id、回测绑定信息，
    以及报告结论（verdict、failures、metrics、constraint_violations），改库里的报告结论会被发现。"""
    payload = {"hash_version": HASH_VERSION, "candidate": candidate.candidate_id,
               "kind": str(getattr(getattr(candidate, "kind", ""), "value", getattr(candidate, "kind", ""))),
               "base": candidate.base_manifest_id,
               "changes": {str(getattr(key, "value", key)): {"ref": value.ref, "content_hash": value.content_hash}
                           for key, value in sorted(candidate.changes.items(), key=lambda item: str(getattr(item[0], "value", item[0])))},
               "binding": _canonical(dict(binding)), "outcome": _canonical(dict(outcome or {}))}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _report_outcome(report: BacktestReport) -> dict[str, Any]:
    return {"verdict": report.verdict, "failures": list(report.failures), "metrics": report.metrics,
            "constraint_violations": report.constraint_violations}


def verify_report_binding(candidate: Candidate, report: BacktestReport, registry: Any = None) -> None:
    """发布前重算报告哈希并核对场景文件与版本标识：回测报告必须证明“测的就是这个候选”。
    b32-v2 及更早的历史报告按旧口径只核对候选编号与基准。"""
    if report.candidate_id != candidate.candidate_id or report.base_manifest_id != candidate.base_manifest_id:
        raise PlantkgError(ErrorCode.PRECONDITION, "回测报告与候选内容不符")
    if report.policy_version == DEV_HASH_VERSION:
        raise PlantkgError(ErrorCode.PRECONDITION, "回测报告与候选内容不符：报告是 b32-v3 算法产物（哈希不含报告结论），需重新回测")
    if report.policy_version != HASH_VERSION:
        return
    if report.artifact_hash != report_artifact_hash(candidate, report.binding, _report_outcome(report)):
        raise PlantkgError(ErrorCode.PRECONDITION, "回测报告与候选内容不符")
    for scenario in report.binding.get("scenarios", ()):
        path, digest = scenario.get("path"), scenario.get("sha256")
        if not (path and digest):
            continue
        file = Path(path) if Path(path).is_absolute() else REPO_ROOT / path  # 相对路径按仓库根解析，不依赖 cwd
        if not file.is_file():
            raise PlantkgError(ErrorCode.PRECONDITION, f"回测报告与候选内容不符：场景文件 {path} 不存在，无法复核，请重新回测")
        if _file_sha256(file) != digest:
            raise PlantkgError(ErrorCode.PRECONDITION, "回测报告与候选内容不符：场景文件在回测后已改动，请重新回测")
    hooks = registry.hooks_for(BACKTEST_VERSIONS_HOOK) if registry is not None else ()
    if hooks and _canonical(dict(hooks[-1]() or {})) != _canonical(report.binding.get("versions") or {}):
        raise PlantkgError(ErrorCode.PRECONDITION, "回测报告与候选内容不符：回测所用 prompt/model 版本与当前不一致，请重新回测")


def component_diff(versions: Any, base: str, changes: Mapping[Any, ComponentRef]) -> list[dict[str, Any]]:
    try:
        before = versions.get(base).components
    except PlantkgError:
        before = {}
    diff = []
    for key, ref in changes.items():
        old = before.get(key)
        diff.append({"component": str(getattr(key, "value", key)), "from_ref": old.ref if old else None,
                     "to_ref": ref.ref, "to_content_hash": ref.content_hash})
    return diff


def _file_sha256(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evolution_config(settings: Any = None) -> dict[str, Any]:
    """B32 进化参数（configs/defaults.yaml 的 evolution 节，标 configured）。"""
    data: dict[str, Any] = {}
    if settings is not None:
        section = settings.section("evolution") if hasattr(settings, "section") else None
        if isinstance(section, Mapping):
            data = dict(section)
    return {"fast_window_min": int(data.get("fast_window_min", 120)),
            "fast_tail_min": int(data.get("fast_tail_min", 30)),
            "autonomy_upgrade_after": int(data.get("autonomy_upgrade_after", 3)),
            "feedback_window": int(data.get("feedback_window", 50)),
            "autonomy_downgrade_min": int(data.get("autonomy_downgrade_min", 2)),
            "sampling_error_high": float(data.get("sampling_error_high", 0.2)),
            "sampling_error_low": float(data.get("sampling_error_low", 0.05)),
            "default_policy": dict(data.get("default_policy") or {})}


class EvolutionService:
    def __init__(self, store: EvolutionStore, versions: Any, actions: Any, artifacts: Any,
                 registry: Any, context: Context, evaluator=None, config: Mapping[str, Any] | None = None):
        self.store, self.versions, self.actions, self.artifacts = store, versions, actions, artifacts
        self.registry, self.context, self.evaluator = registry, context, evaluator
        self.config = evolution_config() | dict(config or {})

    def with_context(self, context: Context) -> EvolutionService:
        return EvolutionService(self.store, self.versions, self.actions, self.artifacts,
                                self.registry, context, self.evaluator, self.config)

    def _prewarm(self, candidate: Candidate) -> None:
        """候选生成时预生成快回测基准摘要（实现由装配层登记），发布时只跑候选一侧。"""
        hooks = self.registry.hooks_for("evolution.prewarm_baseline")
        if not hooks:
            return
        try:
            hooks[-1](candidate, self.context)
        except Exception:  # noqa: BLE001 — 预热失败不挡候选生成，发布时仍会现跑基准
            pass

    def record_correction(self, event_ticket: str, correction: Mapping[str, Any], *, source: str = "correction") -> Candidate:
        if not event_ticket:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "反馈需要事件单编号")
        if correction.get("temporary"):
            raise PlantkgError(ErrorCode.PRECONDITION, "临时规则不能进入长期知识候选")
        base = correction.get("base_manifest_id") or self.versions.active().version_manifest_id
        normalized = dict(correction)
        if correction.get("changes"):
            changes = {ManifestComponent(key): ComponentRef.model_validate(value)
                       for key, value in correction["changes"].items()}
        elif correction.get("rule"):
            changes = self._rule_changes(base, correction["rule"])
            normalized["rule"] = Rule.model_validate(correction["rule"]).model_dump(mode="json")
        elif correction.get("sensor_case"):
            changes = self._sensor_case_changes(event_ticket, base, correction, normalized)
        else:
            changes = self._generated_changes(event_ticket, base, correction, normalized)
        # P2-②：同一份纠错（同一事件单、同一内容、同一基线）重复提交返回已有候选
        content = _sha1(json.dumps({"ticket": event_ticket, "base": base, "correction": normalized, "source": source},
                                   sort_keys=True, ensure_ascii=False, default=str))
        cid = "b32-" + content[:16]
        try:
            return self.store.candidate(cid)
        except PlantkgError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        candidate = Candidate(candidate_id=cid, kind=CandidateKind(correction.get("kind", "knowledge")),
                              base_manifest_id=base, changes=changes, status=CandidateStatus.proposed,
                              trigger=f"人工纠错：{event_ticket}；{correction.get('note', '')}".strip("；"),
                              created_at=datetime.now(timezone.utc), validation_policy_version="b32-v1")
        self.store.correction(event_ticket, normalized)
        self.store.put_candidate(candidate)
        authorized_category = (str(correction.get('autonomy_category') or normalized.get('event_type'))
                               if (correction.get('autonomy_level') is not None
                                   or correction.get('autonomy_delta') is not None
                                   or correction.get('autonomy_direction'))
                               and not any(correction.get(key) for key in ('changes', 'rule', 'sensor_case'))
                               and ManifestComponent.policy in changes else None)
        self.store.event({"event": "candidate_proposed", "candidate_id": cid, "source": source,
                          "event_type": normalized.get("event_type"),
                          "actor": self.context.actor.actor_id if self.context.actor else None,
                          "component_diff": self._component_diff(base, changes),
                          "authorized_category": authorized_category,
                          "event_ticket": event_ticket, "source_asset": correction.get("source_asset"),
                          "at": datetime.now(timezone.utc).isoformat()})
        self._prewarm(candidate)
        return candidate

    def _rule_changes(self, base: str, rule_payload: Mapping[str, Any]) -> dict[ManifestComponent, ComponentRef]:
        if self.artifacts is None:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "B30 规则产物存储未接入")
        if not self.context.actor or Role.reviewer not in self.context.actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "由纠错内容生成规则候选需要复核员上下文")
        proposed_rule = Rule.model_validate(rule_payload)
        if proposed_rule.review_status != "approved" or proposed_rule.reviewer != self.context.actor.actor_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "规则候选须记录当前复核员的批准")
        retained = [rule for rule in self._existing_rules(base) if not self._same_access(rule, proposed_rule)]
        return {ManifestComponent.rules: self.artifacts.prepare(retained + [proposed_rule], self.context.actor)}

    def _same_access(self, existing: Rule, proposed: Rule) -> bool:
        """既有规则与候选规则是否作用于同一消费端主体（等价写法如 grid_connection/MAIN 与 grid/GRID 算同一条）。

        经 B30 的 knowledge.rules.same_access 钩子判断；钩子缺失时退回原始 (kind, subject_ids) 比较并记 note。"""
        hooks = self.registry.hooks_for("knowledge.rules.same_access") if self.registry is not None else ()
        if hooks:
            return bool(hooks[-1](existing, proposed))
        log.warning("缺少 knowledge.rules.same_access 钩子，纠错候选的规则去重退回原始 (kind, subject_ids) 比较")
        return (existing.kind, existing.subject_ids) == (proposed.kind, proposed.subject_ids)

    def _generated_changes(self, event_ticket: str, base: str, correction: Mapping[str, Any],
                           normalized: dict[str, Any]) -> dict[ManifestComponent, ComponentRef]:
        """一句话纠错/抽查/运行反馈 → 可执行规则 + 调查 Skill 模板 + 自主等级或阈值变更。"""
        if not self.context.actor or Role.reviewer not in self.context.actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "由纠错内容生成候选需要复核员上下文（人对发布内容的确认）")
        event_type = str(correction.get("event_type") or correction.get("category") or "").strip()
        if not event_type:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "一句话纠错需要事件类别 event_type（或 category）")
        if self.artifacts is None:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "B30 规则产物存储未接入")
        rule = self._correction_rule(event_ticket, correction, event_type)
        retained = [item for item in self._existing_rules(base) if not self._same_access(item, rule)]
        changes = {ManifestComponent.rules: self.artifacts.prepare(retained + [rule], self.context.actor)}
        normalized["event_type"], normalized["rule"] = event_type, rule.model_dump(mode="json")

        steps = correction.get("steps") or _default_steps(event_type, str(correction.get("note", "")))
        if isinstance(steps, str):
            steps = (steps,)
        skill = {"format_version": 1, "kind": "investigation", "event_type": event_type,
                 "subject_ids": list(rule.subject_ids), "steps": [str(step) for step in steps],
                 "note": str(correction.get("note", "")), "source": f"correction:{event_ticket}", "nature": "synthetic"}
        changes[ManifestComponent.skills] = skill_artifacts(self.versions.writer, self.versions.plant_id).prepare(
            skill, self.context.actor)
        normalized["skill"] = skill

        policy = self._policy_change(base, correction, event_type)
        if policy is not None:
            changes[ManifestComponent.policy] = policy_artifacts(self.versions.writer, self.versions.plant_id).prepare(
                policy.model_dump(mode="json"), self.context.actor)
            normalized["policy"] = policy.model_dump(mode="json")
        return changes

    def _component_diff(self, base: str, changes: Mapping[Any, ComponentRef]) -> list[dict[str, Any]]:
        """候选相对基准清单的组件变化：组件名、旧/新引用与新内容摘要，时间线不再只写组件名。"""
        return component_diff(self.versions, base, changes)

    def _existing_rules(self, base: str) -> list[Rule]:
        current_ref = self.versions.get(base).components.get(ManifestComponent.rules)
        if current_ref and current_ref.ref.startswith("b30-rules:"):
            return [Rule.model_validate(value) for value in self.artifacts.load(current_ref)["rules"]]
        if current_ref and current_ref.ref != "scenario-knowledge:empty":
            raise PlantkgError(ErrorCode.PRECONDITION, "当前规则清单格式无法安全合并纠错规则")
        return []

    def record_feedback(self, event_ticket: str, feedback: Mapping[str, Any]) -> dict[str, Any]:
        """B47 批准/驳回/抽查与 B28 纠错经钩子进来：按“事件类型|实体类型”类别累计。

        连续无误到阈值生成升一级候选；窗口内至少 autonomy_downgrade_min 次“系统判断有误”的驳回/纠错且错误率超过上限
        才生成降一级候选（单次驳回不降级）；抽查滑窗（只统计 source=sampling，旧数据 source 为空不计）错误率高于上限时
        收紧一级并提高抽查比例。候选仍走回测 + 人确认；原因标为“不是系统判断有误”（#193）时只记录、不计入错误次数与错误率。
        """
        category = str(feedback.get("category") or feedback.get("event_type") or "").strip()
        if not category:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "反馈需要事件类别 event_type（或 category）")
        outcome = str(feedback.get("outcome") or "").strip().lower()
        if outcome not in ("approved", "accepted", "correct", "rejected", "wrong", "corrected"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知反馈结果 {outcome}")
        system_error = bool(feedback.get("system_error", True))
        source = str(feedback.get("source") or feedback.get("kind") or "").strip() or None
        note = str(feedback.get("note") or "")
        self.store.feedback(category, outcome, system_error=system_error, ticket_id=event_ticket, note=note,
                            actor=self.context.actor.actor_id if self.context.actor else None, source=source)
        active = self.versions.active().version_manifest_id
        policy = self._current_policy(active)
        current = policy.autonomy_levels.get(category) or policy.autonomy_levels.get(
            category.split("|", 1)[0]) or policy.autonomy_unseen_level
        successes, pending = self.store.autonomy_state(category)
        high = float(self.config["sampling_error_high"])
        sampling_rows, fallback_from = self._sampling_rows(category)
        minimum = min(int(self.config["feedback_window"]), 10)
        error_rate = self._rows_error_rate(sampling_rows)
        candidate_id = None
        if error_rate > high and current != "L0" and len(sampling_rows) >= minimum:
            # 滑窗错误率高于上限且样本够：收紧一级（候选仍走回测 + 人确认；P2-③ 最小样本数）
            downgrade = _shift_level(current, -1)
            if downgrade != current and pending != downgrade:
                candidate_id = self._autonomy_candidate(
                    category, downgrade,
                    f"抽查错误率 {error_rate:.0%} 高于上限 {high:.0%}，{current}→{downgrade}"
                    + (f"（类别 {category} 无抽查样本，回退按 {fallback_from} 匹配）" if fallback_from else ""))
                if candidate_id:
                    successes, pending = 0, downgrade
        elif outcome in ("approved", "accepted", "correct"):
            upgrade = _shift_level(current, 1)
            if (successes + 1 >= int(self.config["autonomy_upgrade_after"]) and upgrade != current
                    and pending != upgrade):
                candidate_id = self._autonomy_candidate(
                    category, upgrade,
                    f"反馈累计：{event_ticket} 连续 {successes + 1} 次批准/抽查无误，{current}→{upgrade}")
                if candidate_id:
                    successes, pending = 0, upgrade
                # 候选没生成（如非复核员上下文）就保留累计，不清零、不占 pending
            else:
                successes += 1
        else:
            successes = 0
            if system_error:
                downgrade = _shift_level(current, -1)
                errors, window_rate = self._system_error_window(category)
                # 最小样本：窗口内至少 autonomy_downgrade_min 次“系统判断有误”的驳回/纠错，且错误率超过上限才降级
                if (downgrade != current and pending != downgrade
                        and errors >= int(self.config["autonomy_downgrade_min"]) and window_rate > high):
                    candidate_id = self._autonomy_candidate(
                        category, downgrade,
                        f"人工驳回/纠错：{event_ticket}，窗口内 {errors} 次系统判断有误（错误率 {window_rate:.0%} 高于上限 "
                        f"{high:.0%}），{current}→{downgrade}")
                    if candidate_id:
                        pending = downgrade
        pending_candidate = candidate_id or (self.store.autonomy_pending_candidate(category) if pending else None)
        low = float(self.config["sampling_error_low"])
        if (candidate_id is None and error_rate < low
                and len(sampling_rows) >= minimum
                and not self.store.autonomy_pending_candidate(category) and float(policy.confidence_low) > 0.1
                and self._relax_categories_ready(category, low)):
            # 错误率低于下限且至少两个类别都满足：放宽置信度阈值（sampling_ratio 目前无消费端，不改）
            candidate_id = self._relax_candidate(
                category, policy, f"抽查错误率 {error_rate:.0%} 低于下限 {low:.0%}，放宽置信度阈值")
            if candidate_id:
                pending_candidate = candidate_id
        self.store.put_autonomy_state(category, successes, pending, pending_candidate_id=pending_candidate)
        self.store.event({"event": "feedback_recorded", "event_type": category, "outcome": outcome,
                          "system_error": system_error, "ticket_id": event_ticket, "candidate_id": candidate_id,
                          "category_fallback": fallback_from,
                          "actor": self.context.actor.actor_id if self.context.actor else None,
                          "at": datetime.now(timezone.utc).isoformat()})
        if outcome in ("approved", "accepted", "correct") and self.registry is not None:
            # B43: 每次成功反馈后触发一次 Skill 挖掘（是否产出由挖掘方按配置与节流判断）
            for hook in self.registry.hooks_for("evolution.skill_mining"):
                try:
                    hook(self.context, event_ticket, {"event_type": category, "outcome": outcome})
                except Exception as exc:  # noqa: BLE001 — 挖 Skill 失败不影响反馈记录，但要在时间线留痕，不静默吞掉
                    log.warning("B43 Skill 挖掘失败（反馈已记录）：%s", exc)
                    self.store.event({"event": "skill_mining_failed", "event_type": category, "ticket_id": event_ticket,
                                      "error_code": str(getattr(getattr(exc, "code", None), "value", "") or ""),
                                      "error": str(getattr(exc, "message", None) or exc)[:200],
                                      "actor": self.context.actor.actor_id if self.context.actor else None,
                                      "at": datetime.now(timezone.utc).isoformat()})
        return {"event_type": category, "outcome": outcome, "level": current,
                "candidate_id": candidate_id, "successes": successes}

    def _sampling_rows(self, category: str) -> tuple[list[dict[str, Any]], str | None]:
        """抽查窗口只统计 source=sampling 的记录（SQL 先过滤再 LIMIT；source 为空的旧数据不计）。

        复合类别（event_type|entity_type）没有抽查样本时回退按 event_type 匹配，并返回回退来源供报告注明。"""
        window = int(self.config["feedback_window"])
        rows = self.store.feedback_rows(category, limit=window, source="sampling")
        if not rows and "|" in category:
            base = category.split("|", 1)[0]
            return self.store.feedback_rows(base, limit=window, source="sampling"), base
        return rows, None

    def _system_error_window(self, category: str) -> tuple[int, float]:
        """窗口内“系统判断有误”的驳回/纠错次数与错误率；“不是系统判断有误”的驳回不计入（#193）。

        只统计该类别上次自主等级变更（已发布）之后的反馈，降级发布前的旧驳回不再触发连环降级。"""
        changed_at = self._last_level_change_at(category)
        rows = [row for row in self.store.feedback_rows(category, limit=int(self.config["feedback_window"]))
                if (row["outcome"] in ("approved", "accepted", "correct") or row["system_error"])
                and (changed_at is None or datetime.fromisoformat(row["at"]) > changed_at)]
        return sum(1 for row in rows if row["outcome"] not in ("approved", "accepted", "correct")), self._rows_error_rate(rows)

    def _last_level_change_at(self, category: str) -> datetime | None:
        """该类别自主等级最近一次变更：已发布的等级候选，或让该类别等级变化的回滚。"""
        times = [datetime.fromisoformat(event["at"]) for event in self.store.timeline_events("published")
                 if event.get("authorized_category") == category]
        for event in self.store.timeline_events("rollback"):
            try:
                changed = (self.published_autonomy(category, event["manifest_id"])
                           != self.published_autonomy(category, event["from_manifest_id"]))
            except (KeyError, PlantkgError):
                continue
            if changed:
                times.append(datetime.fromisoformat(event["at"]))
        return max(times) if times else None

    @staticmethod
    def _rows_error_rate(rows: Sequence[Mapping[str, Any]]) -> float:
        if not rows:
            return 0.0
        errors = sum(1 for row in rows if row["outcome"] in ("rejected", "wrong", "corrected") and row["system_error"])
        return errors / len(rows)

    def _sampling_error_rate(self, category: str) -> float:
        return self._rows_error_rate(self._sampling_rows(category)[0])

    def _relax_categories_ready(self, category: str, low: float) -> bool:
        """全局阈值放宽前要求至少两个类别的抽查窗口都低于下限（当前类别 + 另一个）。"""
        minimum = min(int(self.config["feedback_window"]), 10)
        for other in self.store.feedback_categories():
            if other == category:
                continue
            rows, _fallback = self._sampling_rows(other)
            if len(rows) >= minimum and self._rows_error_rate(rows) < low:
                return True
        return False

    def _autonomy_candidate(self, event_type: str, level: str, trigger: str) -> str | None:
        if not self.context.actor or Role.reviewer not in self.context.actor.roles:
            return None
        base = self.versions.active().version_manifest_id
        policy = self._current_policy(base)
        update = {"autonomy_levels": {**policy.autonomy_levels, event_type: level},
                  "policy_version": f"b32-feedback-{_sha1(f'{event_type}:{base}:{level}')[:12]}"}
        updated = policy.model_copy(update=update)
        ref = policy_artifacts(self.versions.writer, self.versions.plant_id).prepare(
            updated.model_dump(mode="json"), self.context.actor)
        candidate = Candidate(candidate_id="b32-" + secrets.token_hex(8), kind=CandidateKind.parameter,
                              base_manifest_id=base, changes={ManifestComponent.policy: ref},
                              status=CandidateStatus.proposed, trigger=trigger,
                              created_at=datetime.now(timezone.utc), validation_policy_version="b32-v1")
        self.store.put_candidate(candidate)
        self.store.event({"event": "autonomy_candidate", "candidate_id": candidate.candidate_id,
                          "event_type": event_type, "level": level, "trigger": trigger,
                          "actor": self.context.actor.actor_id,
                          "at": datetime.now(timezone.utc).isoformat()})
        self._prewarm(candidate)
        return candidate.candidate_id

    def _relax_candidate(self, category: str, policy: PolicyConfig, trigger: str) -> str | None:
        """抽查错误率低于下限：放宽置信度阈值的 policy 候选（sampling_ratio 暂无消费端，不改）。"""
        if not self.context.actor or Role.reviewer not in self.context.actor.roles:
            return None
        base = self.versions.active().version_manifest_id
        confidence_low = max(0.05, round(float(policy.confidence_low) - 0.05, 2))
        confidence_high = min(0.95, max(confidence_low + 0.05, round(float(policy.confidence_high) - 0.05, 2)))
        updated = policy.model_copy(update={
            "confidence_low": confidence_low, "confidence_high": confidence_high,
            "policy_version": f"b32-relax-{_sha1(f'{category}:{base}')[:12]}"})
        ref = policy_artifacts(self.versions.writer, self.versions.plant_id).prepare(
            updated.model_dump(mode="json"), self.context.actor)
        candidate = Candidate(candidate_id="b32-" + secrets.token_hex(8), kind=CandidateKind.parameter,
                              base_manifest_id=base, changes={ManifestComponent.policy: ref},
                              status=CandidateStatus.proposed, trigger=trigger,
                              created_at=datetime.now(timezone.utc), validation_policy_version="b32-v1")
        self.store.put_candidate(candidate)
        self.store.event({"event": "threshold_candidate", "candidate_id": candidate.candidate_id,
                          "event_type": category, "trigger": trigger,
                          "actor": self.context.actor.actor_id,
                          "at": datetime.now(timezone.utc).isoformat()})
        self._prewarm(candidate)
        return candidate.candidate_id

    def _sensor_case_changes(self, event_ticket: str, base: str, correction: Mapping[str, Any],
                             normalized: dict[str, Any]) -> dict[ManifestComponent, ComponentRef]:
        """D59 最简版：把人工确认的“传感器 vs 设备”案例写成故障特征矩阵候选（synthetic 模板）。"""
        if not self.context.actor or Role.reviewer not in self.context.actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "传感器案例候选需要复核员上下文")
        case = dict(correction["sensor_case"])
        relation_id = str(case.get("relation_id") or "").strip()
        judgement = str(case.get("judgement") or "").strip().lower()
        if not relation_id or judgement not in ("sensor", "equipment"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST,
                               "传感器案例需要 relation_id 与 judgement（sensor/equipment）")
        body = {"format_version": 1, "kind": "sensor_case", "relation_id": relation_id,
                "judgement": judgement, "label": str(case.get("label") or f"人工确认的{judgement}故障"),
                "sensors": [str(item) for item in (case.get("sensors") or ())],
                "entity_id": case.get("entity_id"), "note": str(case.get("note") or correction.get("note") or ""),
                "source": f"correction:{event_ticket}", "nature": "synthetic"}
        cases = [item for item in self._published_sensor_case_list(base)
                 if (item.get("relation_id"), item.get("judgement")) != (relation_id, judgement)]
        cases.append(body)
        ref = sensor_artifacts(self.versions.writer, self.versions.plant_id).prepare(
            {"format_version": 1, "kind": "sensor_cases", "cases": cases}, self.context.actor)
        normalized["sensor_case"] = body
        return {ManifestComponent.sensor_cases: ref}

    def _published_sensor_case_list(self, manifest_id: str) -> tuple[dict[str, Any], ...]:
        """已发布案例累积列表；旧清单把案例放在 parameters 槽，读取时兼容。"""
        manifest = self.versions.get(manifest_id)
        ref = manifest.components.get(ManifestComponent.sensor_cases)
        if ref is None:
            legacy = manifest.components.get(ManifestComponent.parameters)
            if legacy is None or not str(legacy.ref).startswith("b32-sensor:"):
                return ()
            body = sensor_artifacts(self.versions.writer, self.versions.plant_id).load(legacy)
            return tuple(body["cases"]) if body.get("kind") == "sensor_cases" else (body,)
        body = sensor_artifacts(self.versions.writer, self.versions.plant_id).load(ref)
        return tuple(body.get("cases") or ())

    def published_autonomy(self, category: str, manifest_id: str | None = None) -> str | None:
        """当前活动清单里发布的自主等级；B47 把它排在本地缓存之前，发布/回滚即时生效。"""
        manifest = self.versions.get(manifest_id) if manifest_id else self.versions.active()
        if not manifest.ever_active:
            return None
        ref = manifest.components.get(ManifestComponent.policy)
        if ref is None or not str(ref.ref).startswith("policy:"):
            return None
        policy = PolicyConfig.model_validate(policy_artifacts(self.versions.writer, self.versions.plant_id).load(ref))
        return policy.autonomy_levels.get(category) or policy.autonomy_levels.get(category.split("|", 1)[0])

    def published_sensor_cases(self) -> tuple[dict[str, Any], ...]:
        """当前活动清单里的传感器故障案例（D59 参考知识，不直接当闸门；累积列表）。"""
        try:
            return self._published_sensor_case_list(self.versions.active().version_manifest_id)
        except PlantkgError:
            return ()

    def _correction_rule(self, event_ticket: str, correction: Mapping[str, Any], event_type: str) -> Rule:
        subject = str(correction.get("subject_id") or correction.get("entity_id")
                      or f"entity://{self.context.plant_id}/{event_type}/{_slug(event_ticket)}")
        if not subject.startswith("entity://"):
            subject = f"entity://{self.context.plant_id}/{correction.get('entity_type') or 'device'}/{_slug(subject)}"
        source_asset = correction.get("source_asset_version") or correction.get("source_asset")
        try:
            return Rule(rule_id="b32-" + _sha1(f"{event_ticket}:{event_type}")[:12], kind=event_type,
                        subject_ids=(subject,),
                        condition=correction.get("condition") or {
                            "event_type": event_type,
                            "action": correction.get("action") or "按人工纠错后的处置方式处理",
                            "note": str(correction.get("note", ""))},
                        quantity=correction.get("quantity") or "event_handling", unit=correction.get("unit") or "count",
                        value=float(correction.get("value", 1.0)),
                        effective_from=_time(correction.get("effective_from"), self.context.as_of),
                        available_at=_time(correction.get("available_at"), self.context.as_of),
                        source_asset_version=source_asset,
                        locator=Locator(line_start=int(correction.get("line_start", 1))) if source_asset else None,
                        config_ref=None if source_asset else f"config://corrections/{_slug(event_ticket)}",
                        nature=correction.get("nature", "synthetic"), review_status="approved",
                        reviewer=self.context.actor.actor_id, compiler_version="b32-correction-v1")
        except (TypeError, ValueError) as exc:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"纠错内容无法生成规则：{exc}") from None

    def _policy_change(self, base: str, correction: Mapping[str, Any], event_type: str) -> PolicyConfig | None:
        level = correction.get("autonomy_level")
        category = str(correction.get('autonomy_category') or event_type)
        if category != event_type and not category.startswith(event_type + '|'):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '自主等级类别必须属于纠错事件类型')
        delta = correction.get("autonomy_delta")
        if delta is None and correction.get("autonomy_direction"):
            delta = 1 if str(correction["autonomy_direction"]).lower() in ("up", "raise", "升级", "approve") else -1
        thresholds = {key: correction[key] for key in ("confidence_low", "confidence_high", "sampling_ratio")
                      if key in correction}
        if level is None and delta is None and not thresholds:
            return None
        policy = self._current_policy(base)
        update: dict[str, Any] = {}
        if level is not None:
            level = str(level).upper()
            if level not in AUTONOMY_LEVELS:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, f"自主等级只能是 {AUTONOMY_LEVELS}")
            update["autonomy_levels"] = {**policy.autonomy_levels, category: level}
        elif delta is not None:
            current = policy.autonomy_levels.get(category) or policy.autonomy_levels.get(event_type) or policy.autonomy_unseen_level
            index = max(0, min(len(AUTONOMY_LEVELS) - 1, AUTONOMY_LEVELS.index(current) + int(delta)))
            update["autonomy_levels"] = {**policy.autonomy_levels, category: AUTONOMY_LEVELS[index]}
        if thresholds:
            update.update(thresholds)
        update["policy_version"] = f"b32-correction-{_sha1(f'{event_type}:{base}')[:12]}"
        return policy.model_copy(update=update)

    def _current_policy(self, base: str) -> PolicyConfig:
        ref = self.versions.get(base).components.get(ManifestComponent.policy)
        if ref is not None:
            try:
                return PolicyConfig.model_validate(policy_artifacts(self.versions.writer, self.versions.plant_id).load(ref))
            except PlantkgError:
                pass
        return PolicyConfig.model_validate({
            "policy_version": f"b32-default-{_sha1(base)[:12]}",
            "arbitration_weights": {"cny": 1.0, "wait_min": 0.5, "comfort_min": 0.8},
            "priority_profiles": {"default": ("safety", "production", "comfort", "cost")},
            "response_budget": ResponseBudget(layer12_min=1, layer3_min=3),
            **dict(self.config.get("default_policy") or {})})

    def backtest(self, candidate_id: str, scenarios: Sequence[Mapping[str, Any]], *,
                 fast: bool = False, deadline: datetime | None = None) -> BacktestReport:
        candidate = self.store.candidate(candidate_id)
        if candidate.status != CandidateStatus.proposed or not scenarios:
            raise PlantkgError(ErrorCode.PRECONDITION, "回测只接受待验证候选和非空场景")
        evaluators = self.registry.hooks_for(BACKTEST_EVALUATOR_HOOK) if self.evaluator is None else ()
        evaluator = self.evaluator or (evaluators[-1] if evaluators else None)
        results: list[dict[str, Any]] = []
        if evaluator is None:
            results.append({"failure": "no isolated scenario evaluator is registered",
                            "executed": False, "ledger_verified": False})
        else:
            trial_context = self._trial_context().model_copy(update={"version_manifest_id": candidate.base_manifest_id})
            for index, scenario in enumerate(scenarios):
                if deadline is not None and datetime.now(timezone.utc) > deadline:
                    raise PlantkgError(ErrorCode.SOLVER_TIMEOUT, "回测超过任务时限")
                spec = dict(scenario)
                if fast:
                    spec["fast"] = True
                try:
                    result = dict(evaluator(candidate, spec, trial_context) or {})
                except PlantkgError as exc:
                    result = {"failure": exc.message, "executed": False, "ledger_verified": False}
                result.setdefault("scenario_id", str(scenario.get("scenario_id", f"scenario-{index + 1}")))
                results.append(result)
        report = self._backtest_report(candidate, scenarios, results, event_driven=self._event_driven(candidate))
        self.store.put_report(report)
        updated = candidate.model_copy(update={"status": "validated" if report.verdict == "pass" else
                                               "rejected" if report.verdict == "fail" else "proposed",
                                               "backtest_report_id": report.report_id if report.verdict != "inconclusive" else None})
        self.store.update_candidate(updated)
        self.store.event({"event": "backtest_completed", "candidate_id": candidate_id,
                          "report_id": report.report_id, "verdict": report.verdict,
                          "scope": report.scope,
                          "deterministic_fallbacks": int(report.metrics["coverage"]["deterministic_fallbacks"]),
                          "at": datetime.now(timezone.utc).isoformat()})
        if report.verdict == "fail":  # 候选被拒：释放自主等级待发布占位（P2-7）
            self.store.clear_autonomy_pending(candidate_id)
        return report

    def fast_backtest(self, candidate_id: str) -> BacktestReport:
        """演示发布用快回测：优先用快场景集；目标类不在快场景里时退回演示日并截短窗口。"""
        candidate = self.store.candidate(candidate_id)
        providers = self.registry.hooks_for("evolution.fast_scenarios")
        scenarios = providers[-1](candidate, self.context) if providers else ()
        if not scenarios:
            scenarios = ({"scenario_id": "factory_demo_day", "seed": 32,
                          "path": str(Path(__file__).resolve().parents[2] / "configs" / "scenarios" / "factory_demo_day.yaml"),
                          "fast": True},)
        return self.backtest(candidate_id, scenarios, fast=True)

    def _event_driven(self, candidate: Candidate) -> bool:
        """带事件类型（event_type）的纠错候选，其目标一律是事件类：窗口里没有目标事件就不能算通过。

        优先读 store.correction 存的纠错内容（合并前就有的候选没有 candidate_proposed.event_type），再读时间线事件；都没有才退回白名单口径。"""
        ticket = candidate.trigger.split("：", 1)[1].split("；", 1)[0] if candidate.trigger.startswith("人工纠错：") else None
        if ticket and any(body.get("event_type") for body in self.store.corrections(ticket)):
            return True
        return any(item.get("candidate_id") == candidate.candidate_id and item.get("event_type")
                   for item in self.store.timeline_events("candidate_proposed"))

    def _backtest_report(self, candidate: Candidate, scenarios: Sequence[Mapping[str, Any]],
                         results: Sequence[Mapping[str, Any]], event_driven: bool = False) -> BacktestReport:
        """逐场景比较；目标类须改善、其余指标不得退化，任何一项退化即判 fail。"""
        failures: list[str] = []
        complete = True
        base_fallbacks = candidate_fallbacks = 0
        scenario_bindings: list[dict[str, Any]] = []
        warnings: list[str] = []
        cold_s = hot_s = 0.0
        regressions: list[str] = []
        target_measured = target_improved = False
        target_events, reports_events = 0, False
        baseline_reused = 0
        policy_targets = False
        targets_unmeasurable = True
        rule_coverage = 0
        combo_required: set[str] = set()
        combo_covered: set[str] = set()
        policy_required: set[str] = set()
        policy_seen: set[str] = set()
        policy_met: set[str] = set()
        policy_failed: set[str] = set()
        policy_undecided: set[str] = set()
        metrics: dict[str, dict[str, float | None]] = {}
        aggregate: dict[str, dict[str, float]] = {"base": {}, "candidate": {}}
        violations = {"base": 0, "candidate": 0}
        rules_changed = any(str(getattr(key, "value", key)) in OBSERVABLE_COMPONENTS for key in candidate.changes)
        targets_event_like = False
        for result in results:
            sid = str(result.get("scenario_id") or "scenario")
            if result.get("failure"):
                failures.append(str(result["failure"]))
            complete = complete and bool(result.get("executed")) and bool(result.get("ledger_verified"))
            if "base_deterministic_fallbacks" in result or "candidate_deterministic_fallbacks" in result:
                base_fallbacks += int(result.get("base_deterministic_fallbacks", 0) or 0)
                candidate_fallbacks += int(result.get("candidate_deterministic_fallbacks", 0) or 0)
            else:  # 兼容只给合计的旧 evaluator：无法分侧，按候选侧记（合计保持不变）
                candidate_fallbacks += int(result.get("deterministic_fallbacks", result.get("coverage_gap", 0)) or 0)
            scenario_bindings.append({key: result[key] for key in ("scenario_id", "path", "sha256", "seed", "window_end",
                                                                    "window_minutes", "scope") if key in result})
            if "base_elapsed_s" in result or "candidate_elapsed_s" in result:
                cold_s += float(result.get("base_elapsed_s", 0.0)) + float(result.get("candidate_elapsed_s", 0.0))
                hot_s += float(result.get("candidate_elapsed_s", 0.0))
            if result.get("scope") == "fast_window" and result.get("window_minutes") is not None:
                config = getattr(self, "config", None) or evolution_config()
                if float(result["window_minutes"]) > float(config["fast_window_min"]):
                    warnings.append(f"{sid}：快回测窗口 {float(result['window_minutes']):g} 分钟，超过 fast_window_min="
                                    f"{config['fast_window_min']}（目标事件靠后，窗口已按 fast_tail_min={config['fast_tail_min']} 余量延长）")
            if "target_events" in result:
                reports_events = True
                target_events += int(result.get("target_events") or 0)
            targets_event_like = targets_event_like or bool(result.get("targets_event_like"))
            policy_targets = policy_targets or bool(result.get("policy_targets"))
            combo_required.update(result.get('combo_required') or ())
            combo_covered.update(result.get('combo_covered') or ())
            policy_required.update(result.get('policy_required') or ())
            policy_seen.update(result.get('policy_seen') or ())
            policy_met.update(result.get('policy_met') or ())
            policy_failed.update(result.get('policy_failed') or ())
            policy_undecided.update(result.get('policy_undecided') or ())
            targets_unmeasurable = targets_unmeasurable and bool(result.get("targets_unmeasurable", True))
            pairs = result.get("rule_coverage") or {}
            rule_coverage += int(pairs.get("candidate", 0) or 0)
            baseline_reused += int(bool(result.get("baseline_reused")))
            pairs = result.get("metrics") or {}
            if not pairs and "baseline_score" in result:  # 兼容只返回综合分的旧 evaluator：综合分即目标指标
                pairs = {"score": {"base": result.get("baseline_score", 0.0),
                                   "candidate": result.get("candidate_score", 0.0)}}
                pairs["target"] = dict(pairs["score"])
            row: dict[str, float | None] = {}
            for name, pair in pairs.items():
                try:
                    base_value, candidate_value = float(pair["base"]), float(pair["candidate"])
                except (KeyError, TypeError, ValueError):
                    continue
                row[f"base_{name}"], row[f"candidate_{name}"] = base_value, candidate_value
                aggregate["base"][name] = aggregate["base"].get(name, 0.0) + base_value
                aggregate["candidate"][name] = aggregate["candidate"].get(name, 0.0) + candidate_value
                if name in HIGHER_IS_BETTER and candidate_value < base_value:
                    regressions.append(f"{sid}.{name} 退化：{base_value:g} → {candidate_value:g}")
                elif name in LOWER_IS_BETTER:
                    tolerance = abs(base_value) * COST_TOLERANCE if name == "cost_cny" else 0.0
                    if candidate_value > base_value + tolerance:
                        regressions.append(f"{sid}.{name} 退化：{base_value:g} → {candidate_value:g}")
                if name == "target":
                    target_measured = True
                    target_improved = target_improved or candidate_value > base_value
            metrics[sid] = row
            for key in ("base", "candidate"):
                violations[key] += int((result.get("constraint_violations") or {}).get(key, 0) or 0)
        metrics.update(aggregate)
        fallback_count = base_fallbacks + candidate_fallbacks
        metrics["coverage"] = {"deterministic_fallbacks": float(fallback_count),
                               "base_deterministic_fallbacks": float(base_fallbacks),
                               "candidate_deterministic_fallbacks": float(candidate_fallbacks),
                               "target_events": float(target_events),
                               "target_measured": float(bool(target_measured)),
                               "baseline_reused": float(baseline_reused),
                               "rule_coverage": float(rule_coverage)}
        # 规则候选且目标是事件类（带 event_type 的纠错，或目标类型在 router/场景/严重度配置里出现过）：窗口里必须测到目标事件
        observable_change = rules_changed and (targets_event_like or event_driven)
        if failures or not complete:
            verdict = "inconclusive"
            failures = failures or ["scenario execution or ledger verification incomplete"]
        elif regressions:
            verdict, failures = "fail", regressions
        elif combo_required - combo_covered:
            verdict, failures = 'inconclusive', [
                '场景未验证组合规则的设备、条件或生效时间：' + ', '.join(sorted(combo_required - combo_covered))]
        elif policy_failed:
            verdict, failures = 'fail', ['自主等级未达到候选策略：' + ', '.join(sorted(policy_failed))]
        elif policy_undecided:
            verdict, failures = 'inconclusive', ['事件尚未形成自主等级决策：' + ', '.join(sorted(policy_undecided))]
        elif policy_required - policy_seen:
            verdict, failures = 'inconclusive', [
                '场景未覆盖自主等级类别：' + ', '.join(sorted(policy_required - policy_seen))]
        elif policy_required - policy_met:
            verdict, failures = 'fail', [
                '自主等级未达到候选策略：' + ', '.join(sorted(policy_required - policy_met))]
        elif target_improved:
            verdict = "pass"
        elif policy_targets:
            # P1：B47 类别级政策候选必须有目标验证，不能借 target_events==0 的非退化路径放行
            verdict, failures = "fail", ["policy 候选未测到类别级自主等级生效"]
        elif target_measured and reports_events and target_events == 0 and observable_change:
            # 目标类是事件类型的规则候选必须在回测窗口里测到目标事件；没有目标事件不能算通过（B32 A.3）
            verdict, failures = "inconclusive", ["目标类事件未出现在回测窗口"]
        elif target_measured and reports_events and target_events == 0 and targets_unmeasurable:
            # 目标不是场景里的事件类型（电价/温度限值/工单类规则、Skill、图谱、传感器案例等）：只验证非退化，报告里如实标注
            verdict, failures = "pass", ()
            warnings.append("目标类不是场景事件，仅验证非退化")
        elif target_measured:
            verdict, failures = "fail", ["目标类没有改善，不能发布"]
        else:
            verdict = "inconclusive"
            failures = ["回测没有提供目标类指标，无法证明修好这一类"]
        scope = "fast_window" if any(str(result.get("scope")) == "fast_window" for result in results) else "full"
        registry = getattr(self, "registry", None)
        versions_hook = registry.hooks_for(BACKTEST_VERSIONS_HOOK) if registry is not None else ()
        binding = {"scenarios": scenario_bindings, "policy_version": HASH_VERSION, "scope": scope,
                   "versions": dict(versions_hook[-1]() or {}) if versions_hook else {},
                   "base_deterministic_fallbacks": base_fallbacks,
                   "candidate_deterministic_fallbacks": candidate_fallbacks,
                   "timing": {"cold_s": round(cold_s, 3), "hot_s": round(hot_s, 3),
                              "note": "cold=基准侧+候选侧；hot=基准命中缓存只跑候选侧"},
                   "machine": {"cpu_count": os.cpu_count(), "platform": platform.platform(),
                               "python": platform.python_version()},
                   "warnings": warnings}
        return BacktestReport(report_id="b32-report-" + secrets.token_hex(8), candidate_id=candidate.candidate_id,
                              base_manifest_id=candidate.base_manifest_id,
                              split_ids=tuple(str(s.get("scenario_id", f"scenario-{i + 1}")) for i, s in enumerate(scenarios)),
                              seed=int(scenarios[0].get("seed", 0)), metrics=metrics,
                              constraint_violations=violations, failures=tuple(failures),
                              thresholds={"cost_tolerance_pct": COST_TOLERANCE * 100, "target_improvement": 0.0},
                              policy_version=HASH_VERSION, verdict=verdict, scope=scope, binding=binding,
                              artifact_hash=report_artifact_hash(candidate, binding, {
                                  "verdict": verdict, "failures": list(failures), "metrics": metrics,
                                  "constraint_violations": violations}))

    def _consumer_mismatch(self, rules: Sequence[Rule]) -> str | None:
        """发布前按消费端契约校验候选规则；normalise 里的实现由 B30 经钩子提供，跨平面不直连。

        有规则候选却缺校验器时失败关闭（P2-4），不把未校验的规则放出去。
        """
        if not rules:
            return None
        validators = self.registry.hooks_for("knowledge.rules.consumer_mismatch")
        if not validators:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "缺少规则消费端校验器，规则发布已关闭")
        return validators[-1](rules)

    def _changed_rules(self, base_manifest_id: str, rules: Sequence[Rule]) -> list[Rule]:
        """只校验候选新增/变更的规则：历史规则集里的坏条目不应连带阻断本次发布（P2-9）。"""
        try:
            base = {(rule.kind, rule.subject_ids): rule for rule in self._existing_rules(base_manifest_id)}
        except PlantkgError:
            base = {}
        return [rule for rule in rules if base.get((rule.kind, rule.subject_ids)) != rule]

    def propose_publish(self, candidate: Candidate) -> PendingAction:
        try:
            saved = self.store.candidate(candidate.candidate_id)
        except PlantkgError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            self.store.put_candidate(candidate)
            saved = self.store.candidate(candidate.candidate_id)
        immutable = ("kind", "base_manifest_id", "changes", "trigger")
        if any(getattr(saved, key) != getattr(candidate, key) for key in immutable):
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选编号已用于其他内容")
        _require_b30_rules(saved)
        if saved.publish_action_id:  # 同一候选已有待确认的发布动作：返回已有动作，不重复提议（否则确认两次会写重复版本记录）
            try:
                existing = self.actions.get(saved.publish_action_id)
            except PlantkgError:
                existing = None
            if existing is not None and existing.kind == "publish" and existing.status == "proposed":
                return existing
        report = self.store.report(saved.backtest_report_id) if saved.backtest_report_id else None
        if report is None:
            # B30 兼容与一句话纠错的发布都走快回测；全量场景集仍走 backtest 后台任务。
            attempted_report = self.fast_backtest(saved.candidate_id)
            saved = self.store.candidate(saved.candidate_id)
            report = self.store.report(saved.backtest_report_id) if saved.backtest_report_id else attempted_report
        if saved.status == CandidateStatus.published:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选已发布")  # P2-⑤：已发布不能再提议
        if saved.status != CandidateStatus.validated or report is None:
            detail = "；".join(report.failures) if report is not None and report.failures else "缺少回测报告"
            raise PlantkgError(ErrorCode.PRECONDITION, f"候选须先通过回测：{detail}")
        if report.verdict != "pass" or self.versions.active().version_manifest_id != saved.base_manifest_id:
            detail = "；".join(report.failures) if report.failures else "回测未通过"
            raise PlantkgError(ErrorCode.PRECONDITION, f"回测未通过或基准清单已变化：{detail}")
        verify_report_binding(saved, report, self.registry)  # 发布前重算报告哈希：报告必须对得上当前候选内容
        _check_skill_publish(self.registry, self.context, saved)
        rules = saved.changes.get(ManifestComponent.rules)
        if rules is not None:
            if self.artifacts is None:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "B30 规则产物存储未接入")
            body = self.artifacts.load(rules)
            parsed = [Rule.model_validate(rule) for rule in body["rules"]]
            if any(not rule.is_visible(self.context.as_of) for rule in parsed):
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "候选规则在当前时点不可见")
            mismatch = self._consumer_mismatch(self._changed_rules(saved.base_manifest_id, parsed))
            if mismatch is not None:
                raise PlantkgError(ErrorCode.PRECONDITION, "consumer_mismatch：" + mismatch)
        proposal_payload = {"target_ref": saved.candidate_id,
                            "candidate": saved.model_copy(update={"publish_action_id": "pending"}).model_dump(mode="json"),
                            "backtest_report": report.model_dump(mode="json")}
        authorization = _candidate_authorization(self.store, self.versions, saved)
        if authorization:
            proposal_payload['authorization'] = authorization
        action = self.actions.propose("publish", proposal_payload, self.context)
        self.store.update_candidate(saved.model_copy(update={"publish_action_id": action.action_id}))
        self.store.event({"event": "publish_proposed", "candidate_id": saved.candidate_id,
                          "event_key": f"publish_proposed:{saved.candidate_id}",  # P2-① 重复提议不写两条时间线
                          "report_id": report.report_id, "action_id": action.action_id,
                          "scope": report.scope,
                          "actor": self.context.actor.actor_id if self.context.actor else None,
                          "component_diff": self._component_diff(saved.base_manifest_id, saved.changes),
                          "at": datetime.now(timezone.utc).isoformat()})
        return action

    def propose_publish_id(self, candidate_id: str, rationale: str) -> PendingAction:
        candidate = self.store.candidate(candidate_id)
        if not rationale:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "发布提议需要理由")
        return self.propose_publish(candidate)

    def propose_rollback(self, target_manifest_id: str, rationale: str) -> PendingAction:
        target = self.versions.get(target_manifest_id)
        if not target.ever_active:
            raise PlantkgError(ErrorCode.PRECONDITION, "回滚目标必须是曾经激活的清单")
        action = self.actions.propose("rollback", {"target_ref": target_manifest_id,
                                    "target_manifest_id": target_manifest_id,
                                    "expected_active": self.versions.active().version_manifest_id,
                                    "rationale": rationale}, self.context)
        return action

    def _trial_context(self) -> Context:
        return self.context.model_copy(update={"run_mode": "trial"})


class PublishedRulesMatcher:
    """RuleMatcher：当前活动清单里的已发布规则命中即视为有规则覆盖；没有命中时沿用 B27 的静态白名单兜底。"""

    def __init__(self, versions: Any, registry: Any):
        self.versions, self.registry = versions, registry

    def covers(self, event) -> bool:
        try:
            manifest_id = self.versions.active().version_manifest_id
        except PlantkgError:
            manifest_id = None
        return self._covers(event, manifest_id, None)

    def covers_for_run(self, event, run_id) -> bool:
        try:
            run = self.registry.resolve(RunStore).get_run(run_id)
        except (KeyError, PlantkgError):
            whitelist = self.registry.hooks_for('router.rule_covered_events')
            return bool(whitelist) and event.event_type in whitelist[-1]()
        as_of = max(run.as_of, run.forked_at) if run.forked_at else run.as_of
        return self._covers(event, run.version_manifest_id, as_of)

    def authorizes_for_run(self, event, run_id, actions=()) -> bool:
        try:
            run = self.registry.resolve(RunStore).get_run(run_id)
        except (KeyError, PlantkgError):
            return False
        as_of = max(run.as_of, run.forked_at) if run.forked_at else run.as_of
        return self._covers(event, run.version_manifest_id, as_of, executable=True, actions=actions)

    def _covers(self, event, manifest_id, as_of, executable=False, actions=()) -> bool:
        providers = self.registry.hooks_for("knowledge.b30.rules")
        if providers and manifest_id:
            try:
                for rule in providers[-1](manifest_id, event.at, as_of):
                    if rule.kind == event.event_type or str(rule.condition.get("event_type") or "") == event.event_type:
                        if event.event_type == 'unknown_combo':
                            matchers = self.registry.hooks_for('knowledge.rules.matches_combo')
                            matched = bool(matchers) and matchers[-1](rule, event.event_type, event.entity_id, event.summary)
                            if matched and executable:
                                matched = bool(actions) and all(
                                    not action.params and any(item['entity_id'] == action.entity_id
                                                              and item['command'] == action.command
                                                              for item in rule.condition['actions'])
                                    for action in actions)
                            if not matched:
                                continue
                        return True
            except PlantkgError:
                pass
        if executable:
            return False
        whitelist = self.registry.hooks_for("router.rule_covered_events")
        return bool(whitelist) and event.event_type in whitelist[-1]()


def _b30_rules_mismatch(candidate: Candidate) -> str | None:
    rules = candidate.changes.get(ManifestComponent.rules)
    if rules is None or not str(rules.ref).startswith(MINING_RULES_PREFIX):
        return None
    return (f"规则产物 {rules.ref} 是 B26 工单挖掘的候选格式，不是 B30 规则格式（b30-rules:），消费端不能直接加载；"
            "须先由 B30 结构化复核成规则产物后再发布")


def _require_b30_rules(candidate: Candidate) -> None:
    """只保留一种规则产物格式（b30-rules:）：B26 挖掘候选（rules@b26-workorder-*）明确报 consumer_mismatch。"""
    mismatch = _b30_rules_mismatch(candidate)
    if mismatch is not None:
        raise PlantkgError(ErrorCode.PRECONDITION, "consumer_mismatch：" + mismatch)


def _candidate_authorization(store, versions, candidate):
    proposed = next((item for item in reversed(store.timeline())
                     if item.get('candidate_id') == candidate.candidate_id
                     and item.get('event') in ('candidate_proposed', 'autonomy_candidate')), {})
    category = proposed.get('authorized_category') or (
        proposed.get('event_type') if proposed.get('event') == 'autonomy_candidate' else None)
    policy_ref = candidate.changes.get(ManifestComponent.policy)
    if not category or policy_ref is None or not policy_ref.ref.startswith('policy:'):
        return None
    approved_policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(policy_ref))
    level = approved_policy.autonomy_levels.get(category)
    return {'category': category, 'level': level} if level else None


class EvolutionPublisher:
    def __init__(self, service: EvolutionService):
        self.service = service

    def with_context(self, context: Context) -> EvolutionPublisher:
        return EvolutionPublisher(self.service.with_context(context))

    def supports_candidate(self, candidate: Candidate) -> bool:
        """只接收规则产物已是 B30 格式（b30-rules:）的候选；B26 挖掘候选（rules@b26-workorder-*）由调用方落资产等 B30 结构化。"""
        return _b30_rules_mismatch(candidate) is None

    def propose_publish(self, candidate: Candidate) -> PendingAction:
        return self.service.propose_publish(candidate)

    def propose_publish_id(self, candidate_id: str, rationale: str) -> PendingAction:
        return self.service.propose_publish_id(candidate_id, rationale)


class PublishHandler:
    kind = "publish"

    def __init__(self, versions: Any, artifacts: Any, store: EvolutionStore, service: EvolutionService | None = None):
        self.versions, self.artifacts, self.store, self.service = versions, artifacts, store, service

    def apply(self, action: PendingAction, actor: Actor) -> ToolResult:
        if action.status != "confirmed" or Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "发布必须由复核员确认")
        candidate = Candidate.model_validate(action.payload["candidate"])
        report_data = action.payload.get("backtest_report")
        report = BacktestReport.model_validate(report_data) if report_data else None
        if report is not None and (report.candidate_id != candidate.candidate_id or report.verdict != "pass"):
            raise PlantkgError(ErrorCode.PRECONDITION, "发布动作缺少通过的匹配回测报告")
        if report is None:
            if self.service is None:
                raise PlantkgError(ErrorCode.PRECONDITION, "旧发布动作须先完成 B32 隔离回测")
            # A B30 action may have been proposed before B32 took ownership. Re-validate it now.
            try:
                saved = self.store.candidate(candidate.candidate_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                self.store.put_candidate(candidate)
                saved = candidate
            immutable = ('kind', 'base_manifest_id', 'changes', 'trigger')
            if any(getattr(saved, key) != getattr(candidate, key) for key in immutable):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, '旧发布动作与持久化候选不一致')
            if saved.status not in (CandidateStatus.proposed, CandidateStatus.validated):
                raise PlantkgError(ErrorCode.PRECONDITION, "旧发布动作的候选不能重新回测")
            report = self.store.report(saved.backtest_report_id) if saved.backtest_report_id else None
            if report is None or report.verdict != "pass":
                context = self.service.context.model_copy(update={"run_id": action.proposed_by.run_id,
                                                                  "trace_id": action.proposed_by.trace_id,
                                                                  "version_manifest_id": candidate.base_manifest_id})
                report = self.service.with_context(context).fast_backtest(candidate.candidate_id)
                if report.verdict != "pass":
                    raise PlantkgError(ErrorCode.PRECONDITION, "旧发布动作的回测未通过")
                saved = self.store.candidate(candidate.candidate_id)
            candidate = saved
        saved_candidate = self.store.candidate(candidate.candidate_id)
        immutable = ('kind', 'base_manifest_id', 'changes', 'trigger')
        if any(getattr(saved_candidate, key) != getattr(candidate, key) for key in immutable):
            raise PlantkgError(ErrorCode.STATE_CONFLICT, '待发布候选与持久化候选不一致')
        _check_skill_publish(getattr(self.store, "registry", None),
                             self.service.context if self.service is not None else None, candidate)
        if saved_candidate.status not in (CandidateStatus.validated, CandidateStatus.published):
            raise PlantkgError(ErrorCode.STATE_CONFLICT, '待发布候选不再处于可发布状态')
        if report_data is not None and saved_candidate.publish_action_id != action.action_id:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, '发布动作与候选绑定不一致')
        if (saved_candidate.backtest_report_id != report.report_id
                or self.store.report(report.report_id) != report):
            raise PlantkgError(ErrorCode.PRECONDITION, '待发布报告与持久化回测不一致')
        verify_report_binding(saved_candidate, report, getattr(self.store, "registry", None))
        expected_authorization = _candidate_authorization(self.store, self.versions, saved_candidate)
        authorization = action.payload.get('authorization') or {}
        if authorization and authorization != expected_authorization:
            raise PlantkgError(ErrorCode.PRECONDITION, '待确认自主等级与候选策略不一致')
        base = self.versions.get(candidate.base_manifest_id)
        components = dict(base.components) | dict(candidate.changes)
        manifest = self.versions.prepare(components, f"B32 候选 {candidate.candidate_id}")
        if manifest.status == "prepared":
            manifest = self.versions.check_integrity(manifest.version_manifest_id)
        current = self.versions.active().version_manifest_id
        if current == manifest.version_manifest_id:
            activated = manifest
        else:
            if current != candidate.base_manifest_id:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选基准版本已变化")
            activated = self.versions.activate(candidate.base_manifest_id, manifest.version_manifest_id,
                                               actor, action.reason or "确认发布")
        version = self.versions.knowledge_version(activated.version_manifest_id)
        self.store.update_candidate(candidate.model_copy(update={"status": "published",
                                                                "publish_action_id": action.action_id}))
        self.store.clear_autonomy_pending(candidate.candidate_id)  # 已发布：释放待发布占位（P2-7）
        published_event = {"event_key": f"published:{action.action_id}", "event": "published",
                           "candidate_id": candidate.candidate_id, "manifest_id": activated.version_manifest_id,
                           "version": version, "changes": [str(key) for key in candidate.changes],
                           "component_diff": component_diff(self.versions, candidate.base_manifest_id, candidate.changes),
                           "source": candidate.trigger.split("：", 1)[0],
                           "report_id": report.report_id, "comparison": report.metrics, "scope": report.scope,
                           "actor": actor.actor_id, "at": datetime.now(timezone.utc).isoformat()}
        authorized = authorization.get('category') if authorization == expected_authorization else None
        if authorized:
            published_event['authorized_category'] = authorized
        policy_ref = candidate.changes.get(ManifestComponent.policy)
        if policy_ref is not None and str(policy_ref.ref).startswith("policy:"):
            try:  # 等级变化写进发布时间线：确认后以已发布等级为准
                published_event["autonomy_levels"] = PolicyConfig.model_validate(
                    policy_artifacts(self.versions.writer, self.versions.plant_id).load(policy_ref)).autonomy_levels
            except PlantkgError:
                pass
        self.store.event(published_event)
        for hook in self.service.registry.hooks_for('evolution.authorization_published'):
            try:
                hook(candidate.base_manifest_id, activated.version_manifest_id, authorized, action.action_id,
                     datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001 — failed notification must not undo an activated manifest
                log.exception('B47 authorization notification failed; authorization remains restricted')
        self._fork_published_run(action, candidate, activated.version_manifest_id)
        return ToolResult(ok=True, business_status="done", version_manifest_id=activated.version_manifest_id,
                          data={"candidate_id": candidate.candidate_id, "manifest_id": activated.version_manifest_id,
                                "knowledge_version": version, "report_id": report.report_id})

    def _fork_published_run(self, action: PendingAction, candidate: Candidate, manifest_id: str) -> None:
        run_id = action.proposed_by.run_id
        hooks = self.store.registry.hooks_for("evolution.fork_published") if hasattr(self.store, "registry") else ()
        if not run_id or not hooks:
            return
        for hook in hooks:
            hook(run_id, manifest_id, candidate)


def register(registry: Any, settings: Any = None) -> None:
    from plantkg.core.ports import VersionStore, PendingActionStore
    if not all(port in registry.ports for port in (VersionStore, PendingActionStore)):
        def unavailable(*_args, **_kwargs):
            from plantkg.core.contracts import tool_error
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "纠错进化需要启用 B04 持久化存储")

        registry.register_tool("propose_candidates_evo", unavailable, fallback=True)
        registry.register_tool("backtest", unavailable, fallback=True)
        registry.register_tool("propose_rollback", unavailable, fallback=True)
        return
    versions, actions = registry.resolve(VersionStore), registry.resolve(PendingActionStore)
    for artifact_store in (policy_artifacts(versions.writer, versions.plant_id),
                           skill_artifacts(versions.writer, versions.plant_id),
                           sensor_artifacts(versions.writer, versions.plant_id)):
        if not versions.has_verifier(artifact_store.prefix):
            versions.register_verifier(artifact_store.prefix, artifact_store.verify)

    def copy_artifacts(component, trial_versions):
        for artifact_store in (policy_artifacts(versions.writer, versions.plant_id),
                               skill_artifacts(versions.writer, versions.plant_id),
                               sensor_artifacts(versions.writer, versions.plant_id)):
            if component.ref.startswith(artifact_store.prefix):
                return artifact_store.copy(component, trial_versions.writer, trial_versions.plant_id)
        return None

    registry.register_hook("evolution.copy_artifacts", copy_artifacts)
    artifact_factories = registry.hooks_for("knowledge.rules.artifacts")
    artifacts = artifact_factories[-1](versions.writer, versions.plant_id) if artifact_factories else None
    fallback_ctx = Context(plant_id=versions.plant_id, industry_pack=settings.industry_pack,
                           actor=Actor(actor_id="b32-service", roles=(Role.operator,)), channel="offline",
                           trace_id="b32-evolution", as_of=datetime.now(timezone.utc))
    store = EvolutionStore(versions.writer, versions.plant_id)
    store.registry = registry
    service = EvolutionService(store, versions, actions, artifacts, registry, fallback_ctx,
                               config=evolution_config(settings))
    evaluators = registry.hooks_for(BACKTEST_EVALUATOR_HOOK)
    service.evaluator = evaluators[-1] if evaluators else None
    registry.register_hook("evolution.published_autonomy", service.published_autonomy)
    def policy_for_run(run_id, fallback_policy):
        run = registry.resolve(RunStore).get_run(run_id)
        manifest = versions.get(run.version_manifest_id)
        ref = manifest.components.get(ManifestComponent.policy)
        if not manifest.ever_active or ref is None:
            return None
        if str(ref.ref).startswith('policy:'):
            bound_policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(ref))
        elif ref.ref == 'scenario-policy:' + fallback_policy.policy_version:
            body = json.dumps(fallback_policy.model_dump(mode='json'), sort_keys=True, ensure_ascii=False).encode()
            if ref.content_hash != 'sha256:' + hashlib.sha256(body).hexdigest():
                return None
            bound_policy = fallback_policy
        else:
            return None
        return bound_policy
    registry.register_hook("evolution.policy_for_run", policy_for_run)
    def authorized_category_for_manifest(manifest_id):
        return next((item.get('authorized_category') for item in reversed(store.timeline())
                     if item.get('event') == 'published' and item.get('manifest_id') == manifest_id
                     and item.get('authorized_category')), None)
    registry.register_hook('evolution.authorized_category_for_manifest', authorized_category_for_manifest)
    def autonomy_for_run(category, run_id, fallback_policy):
        bound_policy = policy_for_run(run_id, fallback_policy)
        if bound_policy is None:
            return 'L0'
        if category == 'probe':
            return bound_policy.autonomy_levels.get(category) or bound_policy.autonomy_default_level
        return (bound_policy.autonomy_levels.get(category) or bound_policy.autonomy_levels.get(category.split('|', 1)[0])
                or bound_policy.autonomy_unseen_level)
    registry.register_hook("evolution.autonomy_for_run", autonomy_for_run)
    registry.register_hook("sensors.config_overrides", service.published_sensor_cases)
    registry.register(KnowledgePublisher, EvolutionPublisher(service))
    publish_handler = PublishHandler(versions, artifacts, store, service)
    registry.register_action_handler("publish", publish_handler)
    registry.register_hook("evolution.handle_publish", publish_handler.apply)
    registry.register_action_handler("rollback", RollbackHandler(versions, store))
    if RuleMatcher not in registry.ports:  # #177：候选规则的 RuleMatcher 由 B32 注册
        registry.register(RuleMatcher, PublishedRulesMatcher(versions, registry))
        registry.register_hook("scenario_plugin", _scenario_rule_matcher(versions, registry))

    def record_correction(context, event_ticket, correction):
        try:
            if correction.get("feedback"):
                result = service.with_context(context).record_feedback(event_ticket, correction["feedback"])
                return ToolResult(ok=True, business_status="done", data=result)
            candidate = service.with_context(context).record_correction(event_ticket, correction)
            return ToolResult(ok=True, business_status="done", data={"candidate_id": candidate.candidate_id,
                                                                      "candidate": candidate.model_dump(mode="json")})
        except PlantkgError as exc:
            from plantkg.core.contracts import tool_error
            return tool_error(exc.code, exc.message, **exc.details)
        except (TypeError, ValueError) as exc:
            from plantkg.core.contracts import tool_error
            return tool_error(ErrorCode.INVALID_REQUEST, str(exc))

    def default_scenarios(_candidate, _context):
        root = Path(__file__).resolve().parents[2] / "configs" / "scenarios"
        return tuple({"scenario_id": path.stem, "seed": 32, "path": str(path)}
                     for path in sorted(root.glob("*.yaml")))

    registry.register_hook("evolution.default_scenarios", default_scenarios)

    def backtest_scenarios():
        providers = registry.hooks_for("evolution.default_scenarios")
        return providers[-1](None, None) if providers else (
            {"scenario_id": "factory_demo_day", "seed": 32,
             "path": str(Path(__file__).resolve().parents[2] / "configs" / "scenarios" / "factory_demo_day.yaml")},)

    def run_backtest_job(payload, context, _job_id):
        limit = float(payload.get("time_limit_s", 900))
        deadline = datetime.now(timezone.utc) + timedelta(seconds=limit)
        report = service.with_context(context).backtest(payload["candidate_id"], payload["scenarios"],
                                                        deadline=deadline)
        return "b32-report:" + report.report_id

    if JobHandlerRegistrar in registry.ports:
        registry.resolve(JobHandlerRegistrar).register(JobKind.backtest, run_backtest_job, fallback=True)

    def backtest(context, candidate_id, scenarios=None):
        if JobRunner not in registry.ports:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "回测需要后台任务队列（启用 B04 存储）")
        specs = _scenario_specs(scenarios) if scenarios is not None else list(backtest_scenarios())
        job = registry.resolve(JobRunner).submit(
            JobKind.backtest, {"candidate_id": candidate_id, "scenarios": specs,
                               "time_limit_s": 900},  # 全量场景集回测比默认 300 秒预算长
            context.idempotency_key or "b32-backtest-" + secrets.token_hex(12), context)
        return ToolResult(ok=True, business_status="pending", job_id=job.job_id)

    def propose_candidates(context, kind, trigger):
        source = trigger.get("source", "correction")
        if source == "temporary":
            raise PlantkgError(ErrorCode.PRECONDITION, "临时规则不进入长期知识候选")
        if kind not in ("knowledge", "parameter", "skill"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知候选类型：{kind}")
        correction = dict(trigger.get("correction") or {})
        for key, value in trigger.items():
            if key in ("source", "kind", "event_ticket", "temporary", "correction", "changes", "rule"):
                continue
            correction.setdefault(key, value)
        if "changes" not in correction and trigger.get("changes"):
            correction["changes"] = trigger["changes"]
        if "rule" not in correction and trigger.get("rule"):
            correction["rule"] = trigger["rule"]
        if "note" not in correction:
            correction["note"] = trigger.get("note", "")
        correction["kind"] = kind
        if trigger.get("source_asset") is not None:
            correction["source_asset"] = trigger["source_asset"]
        correction["temporary"] = trigger.get("temporary", False)
        candidate = service.with_context(context).record_correction(
            trigger.get("event_ticket", context.run_id or context.trace_id), correction, source=source)
        return ToolResult(ok=True, business_status="done", data={"candidate_id": candidate.candidate_id,
                                                                  "candidate": candidate.model_dump(mode="json")})

    def propose_publish(context, candidate_id, rationale):
        action = service.with_context(context).propose_publish_id(candidate_id, rationale)
        return ToolResult(ok=True, business_status="done", data={"candidate_id": candidate_id,
                                                                      "action_id": action.action_id,
                                                                      "status": action.status})

    def propose_rollback(context, target_manifest_id, rationale):
        return service.with_context(context).propose_rollback(target_manifest_id, rationale)

    for name, handler in (("propose_candidates_evo", propose_candidates), ("propose_rollback", propose_rollback)):
        registry.register_tool(name, _tool_boundary(handler))
    registry.register_tool("backtest", _tool_boundary(backtest))
    registry.register_hook("evolution.record_correction", record_correction)
    store.registry = registry
    def propose_publish_id(context, candidate_id, rationale):
        try:
            return service.with_context(context).propose_publish_id(candidate_id, rationale)
        except PlantkgError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return None
            raise

    registry.register_hook("evolution.propose_publish", propose_publish_id)
    registry.register_hook("evolution.propose_publish_candidate", lambda context, candidate, rationale:
                           service.with_context(context).propose_publish(candidate))
    registry.register_hook("evolution.timeline", lambda context, cursor=None, page_size=None:
                           store.timeline_page(cursor, page_size))
    def timeline_rest(context, _path, query):
        items, next_cursor = store.timeline_page(query.get("cursor"), query.get("page_size"))
        return Page[EvolutionTimelineEntry](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)

    registry.register_rest("GET", "/evolution/timeline", timeline_rest)
    for entry in versions.history():
        if entry["to_status"] == "active":
            store.event({"event_key": "b04:" + ":".join((entry["manifest_id"], entry["occurred_at"])),
                         "event": "manifest_activated", "manifest_id": entry["manifest_id"],
                         "previous_active_id": entry["previous_active_id"], "actor": entry["actor_id"],
                         "reason": entry["reason"], "at": entry["occurred_at"], "source": "B04 history"})
    scenario_runners: dict[str, Any] = {}

    def scenario_runner(run_id: str | None = None, *, runner: Any = None) -> Any:
        """evolution.scenario_runner 钩子：按 run_id 登记（runner=）或查询场景运行器。"""
        if runner is not None:
            scenario_runners[runner.run_info.run_id if runner.run_info else ""] = runner
            return None
        return scenario_runners.get(run_id)

    registry.register_hook("evolution.scenario_runner", scenario_runner)

    forked: set[tuple[str, str]] = set()

    def fork_published(run_id, manifest_id, candidate):
        """发布分叉：以 (candidate_id, manifest_id) 为幂等键，重试（含分叉后钩子中断再确认）不会重复 fork。"""
        key = (candidate.candidate_id, manifest_id)
        if versions.writer.path == ":memory:" or key in forked:
            return
        for hook in registry.hooks_for("evolution.scenario_runner"):
            runner = hook(run_id)
            if runner is not None:
                if runner.run_info.status != "running":
                    return
                if runner.run_info.version_manifest_id == manifest_id:
                    forked.add(key)  # 已有子运行绑定了新版本（上一次分叉已完成）
                    return
                child = runner.fork(runner.now, manifest_id, summary=f"B32 确认发布 {candidate.candidate_id}")
                forked.add(key)
                hook(child.run_id, runner=runner)
                return

    registry.register_hook("evolution.fork_published", fork_published)

    def install_scenario_hooks(scenario_registry):
        scenario_registry.register_hook("evolution.register_scenario_runner",
                                        lambda runner: _register_scenario_runner(registry, runner))
        scenario_registry.register_hook("evolution.published_autonomy", service.published_autonomy)
        scenario_registry.register_hook("sensors.config_overrides", service.published_sensor_cases)

    registry.register_hook("scenario_plugin", install_scenario_hooks)


def _tool_boundary(handler):
    def call(context, **params):
        try:
            result = handler(context, **params)
        except PlantkgError as exc:
            from plantkg.core.contracts import tool_error
            return tool_error(exc.code, exc.message, **exc.details)
        except (TypeError, ValueError) as exc:
            from plantkg.core.contracts import tool_error
            return tool_error(ErrorCode.INVALID_REQUEST, str(exc))
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, Candidate):
            return ToolResult(ok=True, business_status="done", data={"candidate": result.model_dump(mode="json"),
                                                                       "candidate_id": result.candidate_id})
        if isinstance(result, BacktestReport):
            return ToolResult(ok=True, business_status="done", data={"report": result.model_dump(mode="json"),
                                                                       "report_id": result.report_id})
        if isinstance(result, PendingAction):
            return ToolResult(ok=True, business_status="done", data={"action_id": result.action_id,
                                                                          "status": result.status})
        raise PlantkgError(ErrorCode.INTERNAL, "进化工具返回了不支持的结果类型")
    return call


def _register_scenario_runner(registry, runner):
    """把运行器登记进 evolution.scenario_runner 钩子，发布分叉时按 run_id 查询。"""
    hooks = registry.hooks_for("evolution.scenario_runner")
    if hooks:
        hooks[-1](runner=runner)


def _scenario_rule_matcher(versions, registry):
    def plugin(scenario_registry):
        if RuleMatcher not in scenario_registry.ports:
            scenario_registry.register(RuleMatcher, PublishedRulesMatcher(versions, registry))
    return plugin


class RollbackHandler:
    kind = "rollback"

    def __init__(self, versions: Any, store: EvolutionStore):
        self.versions, self.store = versions, store

    def apply(self, action: PendingAction, actor: Actor):
        if action.status != "confirmed" or Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "回滚必须由复核员确认")
        target = self.versions.get(action.payload["target_manifest_id"])
        current = self.versions.active()
        expected = action.payload["expected_active"]
        if not target.ever_active:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回滚目标或活动清单已变化")
        if current.version_manifest_id == target.version_manifest_id:
            manifest, rolled_back_from = current, expected
        else:
            if current.version_manifest_id != expected:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "回滚目标或活动清单已变化")
            manifest = self.versions.activate(current.version_manifest_id, target.version_manifest_id, actor,
                                              action.reason or "确认回滚")
            rolled_back_from = current.version_manifest_id
        self.store.event({"event_key": f"rollback:{action.action_id}", "event": "rollback",
                          "manifest_id": manifest.version_manifest_id, "from_manifest_id": rolled_back_from,
                          "actor": actor.actor_id,
                          "at": datetime.now(timezone.utc).isoformat()})
        return ToolResult(ok=True, business_status="done", version_manifest_id=manifest.version_manifest_id,
                          data={"manifest_id": manifest.version_manifest_id, "rolled_back_from": rolled_back_from})


def _check_skill_publish(registry: Any, context: Context | None, candidate: Candidate) -> None:
    """Skill 候选发布前的评估门：经注册表钩子交给 B43 判定；缺少校验器时失败关闭。"""
    if candidate.kind != CandidateKind.skill:
        return
    hooks = registry.hooks_for(SKILL_PUBLISH_CHECK_HOOK) if registry is not None else ()
    if not hooks:
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "缺少 Skill 发布前评估校验器，Skill 发布已关闭")
    hooks[-1](context, candidate)


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _shift_level(level: str, delta: int) -> str:
    try:
        index = AUTONOMY_LEVELS.index(level)
    except ValueError:
        index = 0
    return AUTONOMY_LEVELS[max(0, min(len(AUTONOMY_LEVELS) - 1, index + delta))]


def _scenario_specs(value: Any) -> list[dict[str, Any]]:
    """校验 backtest 工具传入的场景数组；每个场景至少要有 path。"""
    if not isinstance(value, (list, tuple)) or not value:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "scenarios 须为非空数组")
    specs = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or not item.get("path"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "每个场景须给 path")
        spec = {"scenario_id": str(item.get("scenario_id") or f"scenario-{index + 1}"),
                "seed": int(item.get("seed", 0)), "path": str(item["path"])}
        if item.get("targets"):
            spec["targets"] = [str(target) for target in item["targets"]]
        specs.append(spec)
    return specs


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "correction"


def _time(value: Any, default: datetime) -> datetime:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _default_steps(event_type: str, note: str) -> list[str]:
    """没有给调查步骤时按常理给模板（synthetic）；Agent 可在 correction.steps 里覆盖。"""
    return ["沿图谱追溯 " + event_type + " 的上下游对象与受影响产线",
            "查传感器历史与残差报警，区分读数异常与设备异常",
            "检索历史工单与相关手册条文，确认同类事件的处置",
            "按现场证据给出根因判断：" + (note or "待复核员补充")]
