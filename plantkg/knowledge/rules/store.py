"""入库候选与人工决定的本地持久化。"""
from __future__ import annotations

import json
from typing import Any

from plantkg.core.contracts import ErrorCode, PlantkgError, RuleCandidate
from plantkg.core.versions import canonical, read


class CandidateStore:
    def __init__(self, writer, plant_id: str):
        self.writer, self.plant_id = writer, plant_id
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_candidates "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, decision TEXT, "
            "PRIMARY KEY(plant_id,candidate_id))"))
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_review_scores "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY(plant_id,candidate_id))"))
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_decision_history "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, action_id TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY(plant_id,action_id))"))
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_candidate_routes "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY(plant_id,candidate_id))"))
        writer.submit(lambda con: con.execute(
            "CREATE TABLE IF NOT EXISTS b30_investigations "
            "(plant_id TEXT NOT NULL, candidate_id TEXT NOT NULL, job_id TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY(plant_id,job_id))"))

    def save(self, candidate: RuleCandidate) -> None:
        body = canonical(candidate.model_dump(mode="json"))
        def write(con):
            old = con.execute("SELECT body FROM b30_candidates WHERE plant_id=? AND candidate_id=?",
                              (self.plant_id, candidate.candidate_id)).fetchone()
            if old and old[0] != body:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "同一候选标识的内容发生变化")
            con.execute("INSERT OR IGNORE INTO b30_candidates (plant_id,candidate_id,body) VALUES (?,?,?)",
                        (self.plant_id, candidate.candidate_id, body))
        self.writer.submit(write)

    def get(self, candidate_id: str) -> RuleCandidate:
        row = read(self.writer, lambda con: con.execute("SELECT body FROM b30_candidates WHERE plant_id=? AND candidate_id=?",
                                                        (self.plant_id, candidate_id)).fetchone())
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, "候选规则不存在")
        return RuleCandidate.model_validate_json(row[0])

    def pending(self) -> list[RuleCandidate]:
        rows = read(self.writer, lambda con: con.execute("SELECT body FROM b30_candidates WHERE plant_id=? AND decision IS NULL ORDER BY rowid",
                                                        (self.plant_id,)).fetchall())
        return [RuleCandidate.model_validate_json(row[0]) for row in rows]

    def decision(self, candidate_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute("SELECT decision FROM b30_candidates WHERE plant_id=? AND candidate_id=?",
                                                        (self.plant_id, candidate_id)).fetchone())
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, "候选规则不存在")
        return json.loads(row[0]) if row[0] else None

    def record_score(self, candidate_id: str, score, *, manifest_id: str | None = None, provenance: dict | None = None) -> None:
        self.get(candidate_id)
        if score.status not in {"uncalibrated", "violation", "scored", "incomparable"}:
            raise ValueError("未知复核得分状态")
        body = canonical({"status": score.status, "value": score.value, "days": score.days,
                          "manifest_id": manifest_id, "provenance": provenance})
        self.writer.submit(lambda con: con.execute(
            "INSERT INTO b30_review_scores(plant_id,candidate_id,body) VALUES (?,?,?) "
            "ON CONFLICT(plant_id,candidate_id) DO UPDATE SET body=excluded.body", (self.plant_id, candidate_id, body)))

    def record_route(self, candidate_id: str, route: dict[str, Any]) -> None:
        self.get(candidate_id)
        body = canonical(route)
        self.writer.submit(lambda con: con.execute(
            "INSERT INTO b30_candidate_routes(plant_id,candidate_id,body) VALUES (?,?,?) "
            "ON CONFLICT(plant_id,candidate_id) DO UPDATE SET body=excluded.body", (self.plant_id, candidate_id, body)))

    def route(self, candidate_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute("SELECT body FROM b30_candidate_routes WHERE plant_id=? AND candidate_id=?",
                                                        (self.plant_id, candidate_id)).fetchone())
        return json.loads(row[0]) if row else None

    def record_investigation(self, candidate_id: str, job_id: str, result: dict[str, Any]) -> None:
        self.get(candidate_id)
        body = canonical(result)
        self.writer.submit(lambda con: con.execute(
            "INSERT OR IGNORE INTO b30_investigations(plant_id,candidate_id,job_id,body) VALUES (?,?,?,?)",
            (self.plant_id, candidate_id, job_id, body)))

    def investigations(self, candidate_id: str) -> list[dict[str, Any]]:
        self.get(candidate_id)
        rows = read(self.writer, lambda con: con.execute(
            "SELECT job_id,body FROM b30_investigations WHERE plant_id=? AND candidate_id=? ORDER BY rowid",
            (self.plant_id, candidate_id)).fetchall())
        return [{"job_id": job_id, **json.loads(body)} for job_id, body in rows]

    def ranked_pending(self, manifest_id: str | None = None) -> list[tuple[RuleCandidate, dict[str, Any]]]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT c.body,s.body FROM b30_candidates c LEFT JOIN b30_review_scores s "
            "ON c.plant_id=s.plant_id AND c.candidate_id=s.candidate_id "
            "WHERE c.plant_id=? AND c.decision IS NULL ORDER BY c.rowid", (self.plant_id,)).fetchall())
        items = [(RuleCandidate.model_validate_json(body), json.loads(score) if score else {"status": "uncalibrated", "value": None})
                 for body, score in rows]
        items = [(candidate, score if score.get("manifest_id") == manifest_id else {"status": "uncalibrated", "value": None})
                 for candidate, score in items]
        priority = {"uncalibrated": 3, "violation": 2, "scored": 1, "incomparable": 0}
        return sorted(items, key=lambda pair: (priority[pair[1]["status"]], pair[1].get("value") or 0), reverse=True)

    def decide(self, candidate_id: str, decision: dict[str, Any]) -> None:
        body = canonical(decision)
        def write(con):
            row = con.execute("SELECT decision FROM b30_candidates WHERE plant_id=? AND candidate_id=?",
                              (self.plant_id, candidate_id)).fetchone()
            if row is None:
                raise PlantkgError(ErrorCode.NOT_FOUND, "候选规则不存在")
            if row[0] and json.loads(row[0]) == decision:
                return
            if row[0]:
                previous = json.loads(row[0])
                con.execute("INSERT OR IGNORE INTO b30_decision_history(plant_id,candidate_id,action_id,body) VALUES(?,?,?,?)",
                            (self.plant_id, candidate_id, previous["action_id"], row[0]))
            old = con.execute("SELECT body FROM b30_decision_history WHERE plant_id=? AND action_id=?",
                              (self.plant_id, decision["action_id"])).fetchone()
            if old:
                if old[0] != body:
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "复核动作已用于不同决定")
                return  # 重放旧动作不能覆盖后续修正
            con.execute("INSERT OR IGNORE INTO b30_decision_history(plant_id,candidate_id,action_id,body) VALUES(?,?,?,?)",
                        (self.plant_id, candidate_id, decision["action_id"], body))
            con.execute("UPDATE b30_candidates SET decision=? WHERE plant_id=? AND candidate_id=?",
                        (body, self.plant_id, candidate_id))
        self.writer.submit(write)
