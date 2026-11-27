"""B43 候选 Skill 生成：Pattern → Skill 体与 Nexent SKILL.md，经 B32 的 skill_artifacts 落库。

同名 Skill 已有发布版本时按已发布最大版本号 +1 生成新版本，旧产物不覆盖（内容寻址）。
候选仍走 B32 的 Candidate / pending action / 回测引擎，不另造发布通道。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from plantkg.core.contracts import (
    Candidate, CandidateKind, CandidateStatus, Context, ErrorCode, ManifestComponent, PlantkgError,
    VersionManifest,
)
from plantkg.evolution.artifacts import BodyArtifacts
from plantkg.evolution.skills.miner import Pattern
from plantkg.evolution.store import EvolutionStore


class SkillCandidateGenerator:
    """CandidateGenerator（kind=skill）：重复成功流程 → 候选 Skill。"""

    kind = "skill"

    def __init__(self, versions: Any, store: EvolutionStore, artifacts: BodyArtifacts,
                 *, min_successes: int = 3) -> None:
        self.versions, self.store, self.artifacts = versions, store, artifacts
        self.min_successes = min_successes

    def propose(self, trigger: Mapping[str, Any], base_manifest: VersionManifest,
                context: Context) -> Sequence[Candidate]:
        pattern = _as_pattern(trigger["pattern"])
        event_type = pattern.event_type
        name = str(trigger.get("name") or _skill_name(event_type, pattern.signature))
        content_hash = _content_hash(pattern)
        candidate_id = "b43-" + hashlib.sha1(
            f"{name}:{content_hash}:{base_manifest.version_manifest_id}".encode("utf-8")).hexdigest()[:16]
        try:  # 同名同内容重复 propose 复用已有候选；内容变了内容哈希就变，会生成新候选
            return (self.store.candidate(candidate_id),)
        except PlantkgError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        version = self._next_version(name)
        body = _skill_body(pattern, name, version, trigger)
        ref = self.artifacts.prepare(body, context.actor)
        candidate = Candidate(candidate_id=candidate_id, kind=CandidateKind.skill,
                              base_manifest_id=base_manifest.version_manifest_id,
                              changes={ManifestComponent.skills: ref},
                              status=CandidateStatus.proposed,
                              trigger=(f"重复成功处置：{event_type} ×{pattern.success_count}；"
                                       f"工单 {','.join(pattern.source_tickets)}"),
                              created_at=datetime.now(timezone.utc),
                              validation_policy_version="b43-v1")
        event = {"event": "skill_candidate_created", "candidate_id": candidate_id,
                 "skill_name": name, "version": version, "event_type": event_type,
                 "source_tickets": list(pattern.source_tickets),
                 "success_count": pattern.success_count,
                 "run_id": trigger.get("run_id"), "nature": pattern.nature,
                 "coverage_gap": pattern.coverage_gap,
                 "at": datetime.now(timezone.utc).isoformat()}
        if trigger.get("event_ticket"):
            event["event_ticket"] = trigger["event_ticket"]
        if trigger.get("feedback_actor"):
            event["actor"] = trigger["feedback_actor"]
        self.store.put_candidate(candidate, event)  # 候选与创建事件同一事务
        return (candidate,)

    def _next_version(self, name: str) -> int:
        """同名 Skill 的下一版本号：已发布版本与未发布的 proposed/validated 候选都占号，rejected 不占。"""
        highest = 0
        for event in (*self.store.timeline_events("published"),
                      *self.store.timeline_events("skill_candidate_created", skill_name=name)):
            kind = event.get("event")
            if kind == "published" and event.get("candidate_id"):
                try:
                    candidate = self.store.candidate(str(event["candidate_id"]))
                    ref = candidate.changes.get(ManifestComponent.skills)
                    body = self.artifacts.load(ref) if ref is not None else {}
                except PlantkgError:
                    continue
                if body.get("name") == name and isinstance(body.get("version"), int):
                    highest = max(highest, body["version"])
            elif kind == "skill_candidate_created" and event.get("skill_name") == name:
                try:
                    candidate = self.store.candidate(str(event.get("candidate_id")))
                except PlantkgError:
                    continue
                if candidate.status == CandidateStatus.rejected:
                    continue
                try:
                    highest = max(highest, int(event.get("version")))
                except (TypeError, ValueError):
                    continue
        return highest + 1


def has_pending_candidate(store: EvolutionStore, event_type: str, active_manifest_id: str | None = None) -> bool:
    """同类事件是否已有待发布（proposed/validated）的候选 Skill；反馈触发挖掘的节流条件。

    给 active_manifest_id 时，基准清单已不是活动清单的待发布候选视为过期（期间有别的组件发布，
    它已不可能发布）：标 rejected 并记 skill_candidate_stale，不再占着节流位，允许重新挖掘。
    """
    for event in reversed(store.timeline_events("skill_candidate_created", event_type=event_type)):
        try:
            candidate = store.candidate(str(event.get("candidate_id")))
        except PlantkgError:
            continue
        if candidate.status not in (CandidateStatus.proposed, CandidateStatus.validated):
            continue
        if active_manifest_id is not None and candidate.base_manifest_id != active_manifest_id:
            store.update_candidate(candidate.model_copy(update={"status": CandidateStatus.rejected}))
            store.event({"event": "skill_candidate_stale", "candidate_id": candidate.candidate_id,
                         "event_type": event_type, "base_manifest_id": candidate.base_manifest_id,
                         "active_manifest_id": active_manifest_id,
                         "event_key": f"skill_candidate_stale:{candidate.candidate_id}",
                         "at": datetime.now(timezone.utc).isoformat()})
            continue
        return True
    return False


def _as_pattern(value: Pattern | Mapping[str, Any]) -> Pattern:
    return value if isinstance(value, Pattern) else Pattern(**dict(value))


def _skill_name(event_type: str, signature: Sequence[Sequence[str]] = ()) -> str:
    """稳定 kebab-case + 签名短哈希：同一 event_type 的不同动作签名各有各的 Skill。"""
    base = re.sub(r"[^a-z0-9]", "-", event_type.lower()).strip("-")
    digest = hashlib.sha1(_signature_text(signature).encode("utf-8")).hexdigest()[:6]
    return f"{base}-procedure-{digest}"


def _signature_text(signature: Sequence[Sequence[str]]) -> str:
    return "|".join(f"{entity_type}/{command}" for entity_type, command in signature)


def _content_hash(pattern: Pattern) -> str:
    """候选内容指纹：步骤（entity_type/command）与来源工单；数据变了就会生成新候选。"""
    payload = {"event_type": pattern.event_type,
               "steps": [[step.get("entity_type"), step.get("command")] for step in pattern.steps],
               "source_tickets": list(pattern.source_tickets)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]


def _skill_body(pattern: Pattern, name: str, version: int,
                trigger: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format_version": 1,
        "kind": "procedure",
        "name": name,
        "version": version,
        "event_type": pattern.event_type,
        "title": f"{pattern.event_type} 重复成功处置流程",
        "steps": [dict(step) for step in pattern.steps],
        "preconditions": [f"工单已批准/抽查无误，重复成功 {pattern.success_count} 次"],
        "source_tickets": list(pattern.source_tickets),
        "success_count": pattern.success_count,
        "evidence": [dict(item) for item in pattern.evidence],
        "run_id": trigger.get("run_id"),
        "source": trigger.get("source", "feedback"),
        "nature": pattern.nature,
        "coverage_gap": pattern.coverage_gap,
    }
    # 反馈触发的候选记下原始反馈人与工单；产物由 B43 系统 actor 准备，发布仍要复核员确认
    if trigger.get("feedback_actor"):
        body["feedback_actor"] = str(trigger["feedback_actor"])
    if trigger.get("event_ticket"):
        body["event_ticket"] = str(trigger["event_ticket"])
    body["skill_md"] = _render_skill_md(body)
    return body


def _render_skill_md(body: Mapping[str, Any]) -> str:
    """渲染 Nexent Skill 包里的 SKILL.md：YAML front matter + 编号步骤 + 前置条件 + 来源工单。"""
    description = (f"{body['event_type']} 重复成功处置流程：{body['success_count']} 张工单"
                   f"按相同步骤处置并获批准。")
    lines = ["---", f"name: {body['name']}", f"description: {description}", "---", "",
             f"# {body['title']}", "", "处置步骤：", ""]
    lines += [f"{step['order']}. {step['text']}" for step in body["steps"]]
    lines += ["", "前置条件：", ""]
    lines += [f"- {item}" for item in body["preconditions"]]
    lines += ["", "来源工单：", ""]
    lines += [f"- {ticket}" for ticket in body["source_tickets"]]
    return "\n".join(lines) + "\n"
