"""B30 发布兜底：候选复核、内容寻址产物与 B04 人工确认发布。

此兜底没有 B32 的回测；它不改变已绑定旧清单的运行。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from plantkg.core.contracts import (
    Actor, Candidate, ComponentRef, Context, ErrorCode, ManifestComponent,
    PendingAction, PlantkgError, Role, Rule, ToolResult,
)
from plantkg.core.versions import canonical, read


class RuleArtifacts:
    """不可变规则集保存在与 B04 同一数据库，以摘要作版本引用。"""

    def __init__(self, writer, plant_id: str):
        self.writer, self.plant_id = writer, plant_id
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_rule_artifacts "
            "(plant_id TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY (plant_id, digest))"))

    def prepare(self, rules: list[Rule], actor: Actor, *, ontology: ComponentRef | None = None) -> ComponentRef:
        if Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "规则复核需要复核员角色")
        if (not rules and ontology is None) or any(r.review_status != "approved" for r in rules):
            raise PlantkgError(ErrorCode.PRECONDITION, "规则集必须全部通过人工复核")
        if any(subject.split("/")[2] != self.plant_id for rule in rules for subject in rule.subject_ids):
            raise PlantkgError(ErrorCode.CROSS_PLANT, "规则不能引用其他厂区对象")
        body = canonical({"plant_id": self.plant_id, "reviewer": actor.actor_id,
                          "rules": [rule.model_dump(mode="json") for rule in rules],
                          "ontology": ontology.model_dump(mode="json") if ontology else None})
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.writer.submit(lambda con: con.execute(
            "INSERT OR IGNORE INTO b30_rule_artifacts (plant_id,digest,body) VALUES (?,?,?)",
            (self.plant_id, sha, body)))
        return ComponentRef(ref="b30-rules:" + sha, content_hash="sha256:" + sha)

    def load(self, component: ComponentRef) -> dict:
        if not component.ref.startswith("b30-rules:"):
            raise PlantkgError(ErrorCode.PRECONDITION, "不是 B30 规则产物")
        sha = component.ref.removeprefix("b30-rules:")
        row = read(self.writer, lambda con: con.execute(
            "SELECT body FROM b30_rule_artifacts WHERE plant_id=? AND digest=?", (self.plant_id, sha)).fetchone())
        if row is None or component.content_hash != "sha256:" + sha or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != sha:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "规则产物缺失或摘要不符")
        return json.loads(row[0])

    def verify(self, component: ComponentRef) -> str:
        try:
            self.load(component)
            return "complete"
        except PlantkgError:
            return "corrupt"


class FallbackPublisher:
    """调用方须提供可信上下文；提议只创建待确认动作。"""

    def __init__(self, versions, actions, artifacts: RuleArtifacts, context: Context):
        self.versions, self.actions, self.artifacts, self.context = versions, actions, artifacts, context

    def with_context(self, context: Context) -> "FallbackPublisher":
        return FallbackPublisher(self.versions, self.actions, self.artifacts, context)

    def supports_candidate(self, candidate: Candidate) -> bool:
        component = candidate.changes.get(ManifestComponent.rules)
        return bool(component and component.ref.startswith("b30-rules:"))

    def propose_publish(self, candidate: Candidate) -> PendingAction:
        if candidate.status != "proposed" or not {ManifestComponent.rules} <= set(candidate.changes) <= {ManifestComponent.rules, ManifestComponent.ontology}:
            raise PlantkgError(ErrorCode.PRECONDITION, "兜底发布只接受待发布的已复核规则集")
        component = candidate.changes[ManifestComponent.rules]
        body = self.artifacts.load(component)
        if any(not Rule.model_validate(rule).is_visible(self.context.as_of) for rule in body["rules"]):
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "候选包含提议时尚不可见的资料规则")
        graph = candidate.changes.get(ManifestComponent.ontology)
        if (graph.model_dump(mode="json") if graph else None) != body.get("ontology"):
            raise PlantkgError(ErrorCode.PRECONDITION, "本体产物与复核记录不一致")
        if self.versions.active().version_manifest_id != candidate.base_manifest_id:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选基准版本已变化，须重新复核")
        return self.actions.propose("publish", {"target_ref": candidate.candidate_id,
                                    "candidate": candidate.model_dump(mode="json"),
                                    "fallback_no_backtest": True}, self.context)


class PublishHandler:
    kind = "publish"

    def __init__(self, versions, artifacts: RuleArtifacts, registry=None):
        self.versions, self.artifacts, self.registry = versions, artifacts, registry

    def apply(self, action: PendingAction, actor: Actor) -> ToolResult:
        if self.registry is not None:
            handlers = self.registry.hooks_for("evolution.handle_publish")
            if handlers:
                return handlers[-1](action, actor)
        candidate = Candidate.model_validate(action.payload["candidate"])
        if action.status != "confirmed" or Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "发布必须由复核员确认")
        body = self.artifacts.load(candidate.changes[ManifestComponent.rules])
        graph = candidate.changes.get(ManifestComponent.ontology)
        if (graph.model_dump(mode="json") if graph else None) != body.get("ontology"):
            raise PlantkgError(ErrorCode.PRECONDITION, "本体产物与复核记录不一致")
        base = self.versions.get(candidate.base_manifest_id)
        components = dict(base.components) | dict(candidate.changes)
        manifest = self.versions.prepare(components, f"B30 候选 {candidate.candidate_id}")
        if manifest.status == "prepared":
            manifest = self.versions.check_integrity(manifest.version_manifest_id)
        manifest = self.versions.activate(candidate.base_manifest_id, manifest.version_manifest_id, actor,
                                          action.reason or "确认发布")
        return ToolResult(ok=True, business_status="done", version_manifest_id=manifest.version_manifest_id,
                          data={"candidate_id": candidate.candidate_id, "manifest_id": manifest.version_manifest_id,
                                "fallback_no_backtest": True})


def candidate_for(component: ComponentRef, base_manifest_id: str, candidate_id: str,
                  *, ontology: ComponentRef | None = None) -> Candidate:
    return Candidate(candidate_id=candidate_id, kind="knowledge", base_manifest_id=base_manifest_id,
                     changes={ManifestComponent.rules: component, **({ManifestComponent.ontology: ontology} if ontology else {})},
                     status="proposed", trigger="人工复核资料规则",
                     created_at=datetime.now(timezone.utc), validation_policy_version="b30-fallback-no-backtest")
