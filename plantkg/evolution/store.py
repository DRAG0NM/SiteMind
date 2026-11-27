"""B32 持久化记录；候选和报告均追加保存，便于重建进化时间线。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plantkg.core.contracts import BacktestReport, Candidate, PlantkgError, ErrorCode
from plantkg.core.versions import atomic, page_args, read


class EvolutionStore:
    def __init__(self, writer: Any, plant_id: str):
        self.writer, self.plant_id = writer, plant_id

    def correction(self, ticket: str, body: dict[str, Any]) -> None:
        self.writer.submit(lambda con: con.execute(
            "INSERT INTO b32_corrections(plant_id,ticket,body) VALUES(?,?,?)", (self.plant_id, ticket, _json(body))))

    def corrections(self, ticket: str) -> list[dict[str, Any]]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT body FROM b32_corrections WHERE plant_id=? AND ticket=?", (self.plant_id, ticket)).fetchall())
        return [json.loads(row[0]) for row in rows]

    def put_candidate(self, candidate: Candidate, event: dict[str, Any] | None = None) -> None:
        """写候选；给 event 时候选与时间线事件放进同一事务，不留“有候选没事件”的半成品。"""
        body = candidate.model_dump_json()
        encoded = _json(event) if event is not None else None

        def write_row(con):
            row = con.execute("SELECT body FROM b32_candidates WHERE plant_id=? AND candidate_id=?",
                              (self.plant_id, candidate.candidate_id)).fetchone()
            if row and row[0] != body:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选编号已用于其他内容")
            con.execute("INSERT OR IGNORE INTO b32_candidates(plant_id,candidate_id,body) VALUES(?,?,?)",
                        (self.plant_id, candidate.candidate_id, body))
            if encoded is not None and row is None:
                con.execute("INSERT INTO b32_timeline(plant_id,event_key,body) VALUES(?,?,?)",
                            (self.plant_id, None, encoded))
        self.writer.submit(write_row)

    def candidate(self, candidate_id: str) -> Candidate:
        row = read(self.writer, lambda con: con.execute("SELECT body FROM b32_candidates WHERE plant_id=? AND candidate_id=?",
                                                         (self.plant_id, candidate_id)).fetchone())
        if not row:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"进化候选 {candidate_id} 不存在")
        return Candidate.model_validate_json(row[0])

    def update_candidate(self, candidate: Candidate) -> None:
        """只允许改状态和备注类字段；kind / base_manifest_id / changes 一经写入不可覆盖（回测报告绑的就是它们）。"""
        def write_row(con):
            row = con.execute("SELECT body FROM b32_candidates WHERE plant_id=? AND candidate_id=?",
                              (self.plant_id, candidate.candidate_id)).fetchone()
            if row:
                saved = Candidate.model_validate_json(row[0])
                if any(getattr(saved, key) != getattr(candidate, key) for key in ("kind", "base_manifest_id", "changes")):
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选的类型、基准清单与变更内容不允许更新")
            con.execute("UPDATE b32_candidates SET body=? WHERE plant_id=? AND candidate_id=?",
                        (candidate.model_dump_json(), self.plant_id, candidate.candidate_id))
        self.writer.submit(write_row)

    def put_report(self, report: BacktestReport) -> None:
        self.writer.submit(lambda con: con.execute("INSERT INTO b32_reports(plant_id,report_id,candidate_id,body) VALUES(?,?,?,?)",
                                                    (self.plant_id, report.report_id, report.candidate_id, report.model_dump_json())))

    def report(self, report_id: str) -> BacktestReport:
        row = read(self.writer, lambda con: con.execute("SELECT body FROM b32_reports WHERE plant_id=? AND report_id=?",
                                                         (self.plant_id, report_id)).fetchone())
        if not row:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"回测报告 {report_id} 不存在")
        return BacktestReport.model_validate_json(row[0])

    def timeline(self) -> list[dict[str, Any]]:
        rows = read(self.writer, lambda con: con.execute("SELECT body FROM b32_timeline WHERE plant_id=? ORDER BY seq",
                                                          (self.plant_id,)).fetchall())
        return [json.loads(row[0]) for row in rows]

    def timeline_events(self, event: str, *, event_type: str | None = None,
                        skill_name: str | None = None) -> list[dict[str, Any]]:
        """按 (event, event_type, skill_name) 取时间线事件，走 b32_timeline_event_lookup 表达式索引，不全表扫描。"""
        clause, params = "", []
        for expression, value in (("$.event_type", event_type), ("$.skill_name", skill_name)):
            if value is not None:
                clause += f" AND json_extract(body, '{expression}')=?"
                params.append(value)
        rows = read(self.writer, lambda con: con.execute(
            "SELECT body FROM b32_timeline WHERE plant_id=? AND json_extract(body, '$.event')=?" + clause + " ORDER BY seq",
            (self.plant_id, event, *params)).fetchall())
        return [json.loads(row[0]) for row in rows]

    def timeline_page(self, cursor: str | None = None, page_size: int | None = None) -> tuple[list[dict[str, Any]], str | None]:
        after, size = max(int(cursor or 0), 0), min(max(int(page_size or 50), 1), 200)
        rows = read(self.writer, lambda con: con.execute(
            "SELECT seq,body FROM b32_timeline WHERE plant_id=? AND seq>? ORDER BY seq LIMIT ?",
            (self.plant_id, after, size + 1)).fetchall())
        items = []
        for row in rows[:size]:
            body = json.loads(row[1])
            items.append({"seq": row[0], "event": body["event"], "occurred_at": body["at"],
                          "details": {key: value for key, value in body.items()
                                      if key not in ("event", "at", "event_key")}})
        next_cursor = str(rows[size - 1][0]) if len(rows) > size else None
        return items, next_cursor

    def event(self, body: dict[str, Any]) -> None:
        encoded = _json(body)
        key = body.get("event_key")
        if key:
            self.writer.submit(lambda con: con.execute(
                "INSERT OR IGNORE INTO b32_timeline(plant_id,event_key,body) VALUES(?,?,?)",
                (self.plant_id, key, encoded)))
        else:
            self.writer.submit(lambda con: con.execute(
                "INSERT INTO b32_timeline(plant_id,event_key,body) VALUES(?,?,?)",
                (self.plant_id, None, encoded)))

    # ---- 快回测基线缓存：同一知识版本、场景、种子、窗口与场景文件摘要只跑一次基准一侧
    def baseline(self, cache_key: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT body FROM b32_backtest_baselines WHERE plant_id=? AND cache_key=?",
            (self.plant_id, cache_key)).fetchone())
        return json.loads(row[0]) if row else None

    def put_baseline(self, cache_key: str, body: dict[str, Any]) -> None:
        self.writer.submit(lambda con: con.execute(
            "INSERT OR REPLACE INTO b32_backtest_baselines(plant_id,cache_key,body) VALUES(?,?,?)",
            (self.plant_id, cache_key, _json(body))))

    # ---- 反馈累计：B47 批准/驳回/抽查与 B32 反馈共用
    def feedback(self, event_type: str, outcome: str, *, system_error: bool = True,
                 ticket_id: str = "", note: str = "", at: str | None = None, actor: str | None = None,
                 source: str | None = None) -> None:
        self.writer.submit(lambda con: con.execute(
            "INSERT INTO b32_feedback(plant_id,event_type,outcome,system_error,ticket_id,note,at,actor,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.plant_id, event_type, outcome, int(bool(system_error)), ticket_id, note,
             at or datetime.now(timezone.utc).isoformat(), actor, source)))

    def feedback_rows(self, event_type: str, limit: int = 50, *, source: str | None = None) -> list[dict[str, Any]]:
        """按类别取最近 limit 条反馈；给 source 时在 SQL 里先按来源过滤再 LIMIT，窗口不被其他来源的记录挤掉。"""
        clause, params = ("AND source=? ", (source,)) if source else ("", ())
        rows = read(self.writer, lambda con: con.execute(
            "SELECT outcome,system_error,ticket_id,note,at,actor,source FROM b32_feedback "
            "WHERE plant_id=? AND event_type=? " + clause + "ORDER BY seq DESC LIMIT ?",
            (self.plant_id, event_type, *params, limit)).fetchall())
        return [{"outcome": row[0], "system_error": bool(row[1]), "ticket_id": row[2],
                 "note": row[3], "at": row[4], "actor": row[5], "source": row[6]} for row in rows]

    def feedback_categories(self) -> list[str]:
        """出现过抽查结果的类别（用于“至少两个类别满足放宽条件”的全局检查）；source 为空的旧数据不算抽查。"""
        rows = read(self.writer, lambda con: con.execute(
            "SELECT DISTINCT event_type FROM b32_feedback WHERE plant_id=? AND source='sampling' "
            "ORDER BY event_type", (self.plant_id,)).fetchall())
        return [row[0] for row in rows]

    def autonomy_state(self, event_type: str) -> tuple[int, str | None]:
        row = read(self.writer, lambda con: con.execute(
            "SELECT successes,pending_level FROM b32_autonomy_state WHERE plant_id=? AND event_type=?",
            (self.plant_id, event_type)).fetchone())
        return (int(row[0]), row[1]) if row else (0, None)

    def autonomy_pending_candidate(self, event_type: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT pending_candidate_id FROM b32_autonomy_state WHERE plant_id=? AND event_type=?",
            (self.plant_id, event_type)).fetchone())
        return row[0] if row else None

    def put_autonomy_state(self, event_type: str, successes: int, pending_level: str | None,
                           pending_candidate_id: str | None = None) -> None:
        self.writer.submit(lambda con: con.execute(
            "INSERT OR REPLACE INTO b32_autonomy_state(plant_id,event_type,successes,pending_level,pending_candidate_id,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (self.plant_id, event_type, successes, pending_level, pending_candidate_id,
             datetime.now(timezone.utc).isoformat())))

    def clear_autonomy_pending(self, candidate_id: str) -> None:
        """候选被回测拒绝或已发布后释放占位，后续反馈才能生成新候选。"""
        self.writer.submit(lambda con: con.execute(
            "UPDATE b32_autonomy_state SET pending_level=NULL, pending_candidate_id=NULL, updated_at=? "
            "WHERE plant_id=? AND pending_candidate_id=?",
            (datetime.now(timezone.utc).isoformat(), self.plant_id, candidate_id)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
