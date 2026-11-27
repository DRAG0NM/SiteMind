"""本体复核工作区：候选与批准分离，生成的命名图只有发布后才可见。"""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence

from pyoxigraph import Literal, NamedNode

from plantkg.core.contracts import (
    Actor, ComponentRef, Context, ErrorCode, EvidenceRef, OntologyCandidate, PendingIssue, PlantkgError, Role,
)
from plantkg.knowledge.ontology.shapes import validate_turtle
from plantkg.core.versions import read

BRICK = "https://brickschema.org/schema/Brick#"
PK = "https://plantkg.dev/ns/ontology#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_SAME = "http://www.w3.org/2002/07/owl#sameAs"
OPTIONS = {
    "concept": (BRICK + "Compressor", BRICK + "Temperature_Sensor", BRICK + "HVAC_Zone"),
    "relation": (BRICK + "feeds", BRICK + "isPartOf", BRICK + "hasLocation"),
    "constraint": (PK + "TemperatureLimit", PK + "PressureDropLimit", PK + "OperatingWindow", PK + "ElevatorCongestionRule"),
    "instance": (OWL_SAME, PK + "aliasOf", PK + "relatedTo"),
}
DEFINITIONS = {
    BRICK + "Compressor": "air compressor 空压机 压缩机",
    BRICK + "Temperature_Sensor": "temperature sensor 温度传感器 温度点位",
    BRICK + "HVAC_Zone": "HVAC zone 暖通区域 温控区",
    BRICK + "feeds": "feeds 供应 供给",
    BRICK + "isPartOf": "is part of 属于 组成",
    BRICK + "hasLocation": "has location 位于 区域",
    PK + "TemperatureLimit": "temperature limit 温度限值",
    PK + "PressureDropLimit": "pressure drop limit 压力下降限值",
    PK + "OperatingWindow": "operating window 操作时段",
    PK + "ElevatorCongestionRule": "elevator congestion rule 电梯候梯 排队 拥堵分区",
    OWL_SAME: "same entity 同一实体",
    PK + "aliasOf": "alias 别名",
    PK + "relatedTo": "related 相关",
}


def _ngrams(text: str) -> Counter:
    text = "".join(c for c in text.lower() if c.isalnum())
    return Counter(text[i:i + 2] for i in range(max(0, len(text) - 1)))


def vector_recall(subject: str, options: tuple[str, ...]) -> tuple[str | None, dict[str, float]]:
    """离线字符二元组向量余弦召回；不是嵌入模型，覆盖率未知。"""
    source = _ngrams(subject)
    norm = math.sqrt(sum(v * v for v in source.values()))
    scores = {}
    for option in options:
        target = _ngrams(DEFINITIONS[option])
        den = norm * math.sqrt(sum(v * v for v in target.values()))
        scores[option] = sum(v * target.get(k, 0) for k, v in source.items()) / den if den else 0.0
    ordered = sorted(options, key=lambda o: (-scores[o], o))
    top = ordered[0] if scores[ordered[0]] > 0 and (len(ordered) == 1 or scores[ordered[0]] > scores[ordered[1]]) else None
    return top, scores


class OntologyWorkbench:
    def __init__(self, graph_store, versions):
        self.graph_store, self.versions = graph_store, versions
        self.candidates: dict[str, OntologyCandidate] = {}
        self.targets: dict[str, str] = {}
        self.approved: dict[str, tuple[str, str]] = {}
        self.rejected: set[str] = set()
        self.review_actions: dict[str, str] = {}
        self.writer, self.plant_id = versions.writer, versions.plant_id
        self.writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_ontology_candidates "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, target TEXT, choice TEXT, reviewer TEXT, review_action TEXT, "
            "PRIMARY KEY(plant_id,candidate_id))"))
        columns = read(self.writer, lambda con: [row[1] for row in con.execute("PRAGMA table_info(b30_ontology_candidates)")])
        if "review_action" not in columns:
            self.writer.submit(lambda con: con.execute("ALTER TABLE b30_ontology_candidates ADD COLUMN review_action TEXT"))
        rows = read(self.writer, lambda con: con.execute(
            "SELECT candidate_id,body,target,choice,reviewer,review_action FROM b30_ontology_candidates WHERE plant_id=?",
            (self.plant_id,)).fetchall())
        for cid, body, target, choice, reviewer, action in rows:
            self.candidates[cid] = OntologyCandidate.model_validate_json(body)
            if target:
                self.targets[cid] = target
            if choice == "__rejected__":
                self.rejected.add(cid)
            elif choice and reviewer:
                self.approved[cid] = (choice, reviewer)
                if action:
                    self.review_actions[cid] = action

    def _save(self, candidate: OntologyCandidate, target: str | None = None) -> None:
        body = candidate.model_dump_json()
        def write(con):
            row = con.execute("SELECT body,target FROM b30_ontology_candidates WHERE plant_id=? AND candidate_id=?",
                              (self.plant_id, candidate.candidate_id)).fetchone()
            if row and row != (body, target):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "本体候选标识对应不同内容")
            con.execute("INSERT OR IGNORE INTO b30_ontology_candidates(plant_id,candidate_id,body,target) VALUES(?,?,?,?)",
                        (self.plant_id, candidate.candidate_id, body, target))
        self.writer.submit(write)

    def propose_candidates(self, context: Context, subject_ids: Sequence[str], layer: str) -> list[OntologyCandidate]:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "本体工作区不属于当前厂区")
        if layer not in OPTIONS:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "未知本体层")
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "本体候选需要可信写上下文")
        result = []
        for subject in subject_ids:
            if not subject.startswith(f"entity://{context.plant_id}/"):
                raise PlantkgError(ErrorCode.CROSS_PLANT, "本体对象必须属于当前厂区")
            candidate_id = "ont-" + hashlib.sha256(f"{layer}:{subject}".encode()).hexdigest()[:16]
            top, _ = vector_recall(subject, OPTIONS[layer])
            candidate = OntologyCandidate(candidate_id=candidate_id, layer=layer, subject=subject,
                                          options=OPTIONS[layer], recall_source="vector" if top else "rule")
            self._save(candidate)
            self.candidates[candidate_id] = candidate
            result.append(candidate)
        return result

    def link_entities(self, context: Context, subject_id: str, target_id: str, relation: str) -> OntologyCandidate:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "本体工作区不属于当前厂区")
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "本体候选需要可信写上下文")
        if any(not value.startswith(f"entity://{context.plant_id}/") for value in (subject_id, target_id)):
            raise PlantkgError(ErrorCode.CROSS_PLANT, "本体对象必须属于当前厂区")
        if relation not in OPTIONS["relation"] + OPTIONS["instance"]:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "关系必须使用已支持的 Brick 或项目命名空间")
        layer = "instance" if relation in OPTIONS["instance"] else "relation"
        candidate_id = "ont-" + hashlib.sha256(f"{layer}:{subject_id}:{target_id}:{relation}".encode()).hexdigest()[:16]
        candidate = OntologyCandidate(candidate_id=candidate_id, layer=layer, subject=subject_id,
                                      options=OPTIONS[layer], recall_source="manual")
        self._save(candidate, target_id)
        self.candidates[candidate_id], self.targets[candidate_id] = candidate, target_id
        return candidate

    def propose_document_candidate(self, context: Context, subject: str, evidence: EvidenceRef) -> OntologyCandidate:
        """为明确关联的资料保留独立来源，不改写其他资料的候选。"""
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "本体候选需要可信写上下文")
        if context.plant_id != self.plant_id or not subject.startswith(f"entity://{self.plant_id}/"):
            raise PlantkgError(ErrorCode.CROSS_PLANT, "本体对象必须属于当前厂区")
        if not evidence.asset_version_id or not evidence.locator:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "资料候选需要原文定位")
        key = f"concept:{subject}:{evidence.asset_version_id}:{evidence.locator.model_dump_json()}"
        candidate_id = "ont-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        top, _ = vector_recall(subject, OPTIONS['concept'])
        candidate = OntologyCandidate(candidate_id=candidate_id, layer='concept', subject=subject,
                                      options=OPTIONS['concept'], evidence=(evidence,),
                                      recall_source='vector' if top else 'rule')
        self._save(candidate)
        self.candidates[candidate_id] = candidate
        return candidate

    def review(self, candidate_id: str, choice: str, actor: Actor, *, action_id: str | None = None) -> None:
        if Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "本体复核需要复核员角色")
        candidate = self.candidates[candidate_id]
        if choice not in candidate.options:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核选择不在候选中")
        if candidate.layer in ("relation", "instance") and candidate_id not in self.targets:
            raise PlantkgError(ErrorCode.PRECONDITION, "关系候选缺少目标实体")
        self.writer.submit(lambda con: con.execute(
            "UPDATE b30_ontology_candidates SET choice=?,reviewer=?,review_action=? WHERE plant_id=? AND candidate_id=?",
            (choice, actor.actor_id, action_id, self.plant_id, candidate_id)))
        self.approved[candidate_id] = (choice, actor.actor_id)
        if action_id:
            self.review_actions[candidate_id] = action_id
        else:
            self.review_actions.pop(candidate_id, None)
        self.rejected.discard(candidate_id)

    def reject(self, candidate_id: str, actor: Actor) -> None:
        if Role.reviewer not in actor.roles or candidate_id not in self.candidates:
            raise PlantkgError(ErrorCode.FORBIDDEN, "本体复核需要复核员角色和已登记候选")
        self.writer.submit(lambda con: con.execute(
            "UPDATE b30_ontology_candidates SET choice='__rejected__',reviewer=? WHERE plant_id=? AND candidate_id=?",
            (actor.actor_id, self.plant_id, candidate_id)))
        self.approved.pop(candidate_id, None)
        self.review_actions.pop(candidate_id, None)
        self.rejected.add(candidate_id)

    def prepare_graph(self, context: Context, actor: Actor) -> ComponentRef:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "本体工作区不属于当前厂区")
        if Role.reviewer not in actor.roles or not self.approved:
            raise PlantkgError(ErrorCode.PRECONDITION, "命名图必须含已复核本体候选")
        triples = []
        for cid, (choice, reviewer) in sorted(self.approved.items()):
            candidate = self.candidates[cid]
            subject = str(NamedNode(candidate.subject))
            if candidate.layer in ("concept", "constraint"):
                predicate, obj = NamedNode(RDF_TYPE), NamedNode(choice)
            else:
                predicate, obj = NamedNode(choice), NamedNode(self.targets[cid])
            triples.append(f"{subject} {predicate} {obj} .")
            triples.append(f"{subject} {NamedNode(PK + 'reviewedBy')} {Literal(reviewer)} .")
            for evidence in candidate.evidence:
                if evidence.asset_version_id:
                    triples.append(f"{subject} {NamedNode(PK + 'sourceAsset')} {NamedNode(evidence.asset_version_id)} .")
        turtle = "\n".join(triples) + "\n"
        issues = validate_turtle(turtle)
        if issues:
            raise PlantkgError(ErrorCode.PRECONDITION, "本体图未通过 SHACL 结构检查", issues=[i.model_dump(mode="json") for i in issues])
        sha = hashlib.sha256(turtle.encode("utf-8")).hexdigest()
        ref = self.graph_store.prepare("b30-" + sha[:24], turtle)
        return ComponentRef(ref=ref, content_hash="sha256:" + sha)

    def validate(self, context: Context, graph_name: str) -> list[PendingIssue]:
        # B03 读图强制绑定清单；不能用此入口读取未发布候选。
        rows, cursor = self.graph_store.read_named_graph(context, graph_name, "triples", {"page_size": 500})
        while cursor:
            page, cursor = self.graph_store.read_named_graph(context, graph_name, "triples", {"page_size": 500}, cursor)
            rows.extend(page)
        if not rows:
            return [PendingIssue(issue_id="b30-empty-graph", reason="subject_unknown", detail="本体图为空")]
        return validate_turtle("\n".join(f"{r['subject']} {r['predicate']} {r['object']} ." for r in rows))

    def snapshot(self, context: Context) -> str:
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "本体快照需要运行固定清单")
        return self.versions.get(context.version_manifest_id).manifest_hash
