"""资料入库分流、例外调查与经复核的抽取模板学习。

这里的 Agent 由调用方注入，且只产生复核建议；无标准原文时明确待定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from plantkg.core.contracts import Actor, ErrorCode, JudgeResult, PlantkgError, Role, RuleCandidate
from plantkg.core.versions import canonical, read
from plantkg.knowledge.rules.review import ReviewItem, route_candidate


@dataclass(frozen=True)
class Source:
    asset_version_id: str
    locator: str
    excerpt: str
    available_at: str
    effective_from: str | None = None
    kind: str = "document"


@dataclass(frozen=True)
class Investigation:
    decision: str
    rationale: str
    citations: tuple[str, ...]
    operator_question: str | None = None


class ExceptionAgent:
    """注入模型调用；只允许引用本次提供的原文，不自动发布。"""

    def __init__(self, ask: Callable[[str], str]):
        self.ask = ask

    def investigate(self, item: ReviewItem, sources: list[Source], *, as_of: datetime,
                    ask_operator: Callable[[str], str] | None = None) -> Investigation:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("调查时点须带时区")
        visible = []
        for source in sources:
            available = datetime.fromisoformat(source.available_at.replace("Z", "+00:00"))
            if available.tzinfo is None:
                raise ValueError("来源可见时间须带时区")
            if available <= as_of:
                visible.append(source)
        sources = visible
        if not any(source.kind == "standard" and source.excerpt and source.locator for source in sources):
            return Investigation("pending", "缺少可核对的标准原文，保留候选待复核", ())
        prompt = ("调查资料入库例外，仅给建议，不发布。检查图谱对象、两份资料的可见与生效日期、标准原文。"
                  "只能使用提供的来源，信息不足选 pending；缺现场事实最多问一个问题。"
                  "返回 JSON：decision（merge/replace/add_node/pending）、rationale、citations（来源 ID 数组）、"
                  "operator_question（字符串或 null）。\n"
                  + json.dumps({"candidate": item.candidate.model_dump(mode="json"), "reasons": item.reasons,
                                "sources": [s.__dict__ for s in sources]}, ensure_ascii=False))
        answer = json.loads(self.ask(prompt))
        if answer.get("operator_question") and ask_operator is not None:
            question = answer["operator_question"]
            if not isinstance(question, str) or question.count("?") + question.count("？") > 1:
                raise PlantkgError(ErrorCode.PRECONDITION, "最多向值班人员询问一个问题")
            fact = ask_operator(question)
            answer = json.loads(self.ask(prompt + "\n值班人员回答：" + str(fact) + "\n不再提问，继续调查并输出 JSON。"))
            if answer.get("operator_question"):
                raise PlantkgError(ErrorCode.PRECONDITION, "一次调查只能询问值班人员一个问题")
        choice = answer.get("decision")
        citations = answer.get("citations") or []
        valid_ids = {source.asset_version_id for source in sources}
        question = answer.get("operator_question")
        if (choice not in {"merge", "replace", "add_node", "pending"} or not answer.get("rationale")
                or not isinstance(citations, list) or any(c not in valid_ids for c in citations)
                or (choice != "pending" and not citations) or (question is not None and not isinstance(question, str))
                or (isinstance(question, str) and question.count("?") + question.count("？") > 1)):
            raise PlantkgError(ErrorCode.PRECONDITION, "Agent 建议缺少依据或引用了未提供的资料")
        return Investigation(choice, str(answer["rationale"]), tuple(citations), question)


@dataclass
class ExtractionTemplates:
    """复核确认后才回写；示例和校准标签保留来源以供 B48/B24 使用。"""
    types: set[str] = field(default_factory=set)
    examples: list[dict[str, Any]] = field(default_factory=list)
    calibration: list[dict[str, Any]] = field(default_factory=list)

    def record(self, document_type: str, candidate: RuleCandidate, label: str, actor: Actor,
               *, alias: str | None = None, field_map: dict[str, str] | None = None,
               judge_result: dict[str, Any] | None = None) -> None:
        if Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "模板学习需要复核员确认")
        self.types.add(document_type)
        sample = {"document_type": document_type, "candidate_id": candidate.candidate_id,
                  "asset_version_id": candidate.source_asset_version, "locator": candidate.locator.model_dump(mode="json"),
                  "label": label, "reviewer": actor.actor_id, "alias": alias, "field_map": field_map or {}}
        self.examples.append(sample)
        if judge_result is not None:
            self.calibration.append({**sample, "judge_result": judge_result})


class TemplateStore:
    def __init__(self, writer, plant_id: str):
        self.writer, self.plant_id = writer, plant_id
        writer.submit(lambda con: con.execute("CREATE TABLE IF NOT EXISTS b30_templates "
                                             "(plant_id TEXT PRIMARY KEY, body TEXT NOT NULL)"))
        writer.submit(lambda con: con.execute("CREATE TABLE IF NOT EXISTS b30_batches "
                                             "(plant_id TEXT NOT NULL, batch_id TEXT NOT NULL, body TEXT NOT NULL, "
                                             "PRIMARY KEY(plant_id,batch_id))"))

    def load(self) -> ExtractionTemplates:
        row = read(self.writer, lambda con: con.execute("SELECT body FROM b30_templates WHERE plant_id=?", (self.plant_id,)).fetchone())
        if row is None:
            return ExtractionTemplates()
        body = json.loads(row[0])
        return ExtractionTemplates(types=set(body["types"]), examples=body["examples"], calibration=body["calibration"])

    def record(self, document_type: str, candidate: RuleCandidate, label: str, actor: Actor,
               *, alias: str | None = None, field_map: dict[str, str] | None = None,
               judge_result: dict[str, Any] | None = None) -> ExtractionTemplates:
        # 顺序写入单写者；防止读取后再覆盖并发产生的模板反馈。
        def write(con):
            row = con.execute("SELECT body FROM b30_templates WHERE plant_id=?", (self.plant_id,)).fetchone()
            body = json.loads(row[0]) if row else {"types": [], "examples": [], "calibration": []}
            templates = ExtractionTemplates(set(body["types"]), body["examples"], body["calibration"])
            if any(example["candidate_id"] == candidate.candidate_id and example["reviewer"] == actor.actor_id
                   and example["label"] == label and example.get("alias") == alias
                   and example.get("field_map") == (field_map or {}) for example in templates.examples):
                return templates
            templates.record(document_type, candidate, label, actor, alias=alias, field_map=field_map, judge_result=judge_result)
            body = canonical({"types": sorted(templates.types), "examples": templates.examples, "calibration": templates.calibration})
            con.execute("INSERT INTO b30_templates (plant_id,body) VALUES (?,?) "
                        "ON CONFLICT(plant_id) DO UPDATE SET body=excluded.body", (self.plant_id, body))
            return templates
        return self.writer.submit(write)

    def record_batch(self, batch_id: str, items: list[ReviewItem], *, review_minutes: float,
                     tokens: int, elapsed_seconds: float, reviewer: str | None = None,
                     aligned_count: int | None = None) -> dict[str, float | int]:
        metrics = batch_metrics(items, review_minutes=review_minutes, tokens=tokens, elapsed_seconds=elapsed_seconds)
        if aligned_count is not None:
            if not 0 <= aligned_count <= metrics["items"]:
                raise ValueError("标准对齐数量不合法")
            metrics.update(standard_aligned_count=aligned_count,
                           standard_alignment_rate=aligned_count / metrics["items"] if metrics["items"] else 0.0)
        candidate_ids = [item.candidate.candidate_id for item in unique_items(items)]
        body = canonical({**metrics, "candidate_ids": candidate_ids, "reviewer": reviewer})
        def write(con):
            rows = con.execute("SELECT batch_id, body FROM b30_batches WHERE plant_id=?", (self.plant_id,)).fetchall()
            for other_id, other_body in rows:
                if other_id == batch_id:
                    continue
                clash = set(candidate_ids) & set(json.loads(other_body).get("candidate_ids") or ())
                if clash:
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, f"候选 {sorted(clash)[0]} 已属于批次 {other_id}")
            row = con.execute("SELECT body FROM b30_batches WHERE plant_id=? AND batch_id=?", (self.plant_id, batch_id)).fetchone()
            if row and row[0] != body:
                raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一批次编号对应不同复核指标")
            con.execute("INSERT OR IGNORE INTO b30_batches(plant_id,batch_id,body) VALUES(?,?,?)", (self.plant_id, batch_id, body))
        self.writer.submit(write)
        return metrics

    def batches(self) -> list[dict[str, Any]]:
        rows = read(self.writer, lambda con: con.execute("SELECT batch_id,body FROM b30_batches WHERE plant_id=? ORDER BY rowid",
                                                        (self.plant_id,)).fetchall())
        return [{"batch_id": row[0], **json.loads(row[1])} for row in rows]


def route(candidate: RuleCandidate, judge: JudgeResult, templates: ExtractionTemplates, document_type: str,
          *, entity_exists: bool, standard_aligned: bool, conflicts: bool, impact: float = 0.0) -> ReviewItem:
    return route_candidate(candidate, judge, has_template=document_type in templates.types,
                           entity_exists=entity_exists, standard_aligned=standard_aligned,
                           conflicts=conflicts, impact=impact)


def unique_items(items: list[ReviewItem]) -> list[ReviewItem]:
    """同一候选在一批里只计一次（#146）。"""
    unique: dict[str, ReviewItem] = {}
    for item in items:
        unique.setdefault(item.candidate.candidate_id, item)
    return list(unique.values())


def batch_metrics(items: list[ReviewItem], *, review_minutes: float, tokens: int, elapsed_seconds: float) -> dict[str, float | int]:
    if review_minutes < 0 or tokens < 0 or elapsed_seconds < 0:
        raise ValueError("批次用量不能为负")
    items = unique_items(items)
    total = len(items)
    return {"items": total, "agent_count": sum(bool(item.reasons) for item in items),
            "agent_fraction": sum(bool(item.reasons) for item in items) / total if total else 0.0,
            "review_minutes_per_100": 100 * review_minutes / total if total else 0.0,
            "tokens": tokens, "elapsed_seconds": elapsed_seconds,
            "document_count": len({item.candidate.source_asset_version for item in items})}
