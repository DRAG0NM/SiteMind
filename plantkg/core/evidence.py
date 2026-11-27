"""证据事件（B04）：只追加、带全局单调 seq，写入即可订阅（网关合流用），分页读取供 explain。

seq 由这里分配，调用方给的 seq 被忽略；同一 event_id 重复写入相同内容返回原事件，内容不同拒绝。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from plantkg.core.contracts import Context, ErrorCode, EvidenceEvent, EvidencePage, PlantkgError
from plantkg.core.versions import atomic, canonical, page_args, read


class SqliteEvidenceSink:
    """EvidenceSink 端口实现。"""

    def __init__(self, writer: Any, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id
        self._changed = threading.Condition()
        self._closed = False
        # 运行时钟（回放游标）：装配注入；场景运行没有回放游标时退回该运行已写入的最新事件时刻
        self.run_clock: Callable[[str], Any] | None = None

    def append(self, event: EvidenceEvent) -> EvidenceEvent:
        body = canonical(event.model_dump(mode="json", exclude={"seq"}))

        def write(con):
            row = con.execute("SELECT seq, record FROM evidence_events WHERE plant_id=? AND event_id=?", (self.plant_id, event.event_id)).fetchone()
            if row:
                stored = EvidenceEvent.model_validate_json(row[1])
                if canonical(stored.model_dump(mode="json", exclude={"seq"})) != body:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, f"证据事件 {event.event_id} 已存在且内容不同")
                return stored
            cur = con.execute("INSERT INTO evidence_events(plant_id,event_id,run_id,agent_run_id,trace_id,action_id,record) VALUES (?,?,?,?,?,?,?)",
                              (self.plant_id, event.event_id, event.run_id, event.agent_run_id, event.trace_id, event.action_id, "{}"))
            stored = event.model_copy(update={"seq": cur.lastrowid})
            con.execute("UPDATE evidence_events SET record=? WHERE seq=?", (stored.model_dump_json(), cur.lastrowid))
            return stored
        stored = atomic(self.writer, write)
        with self._changed:
            self._changed.notify_all()
        return stored

    def list(self, run_id: str | None, cursor: str | None, visibility: Context, *, agent_run_id: str | None = None,
             action_id: str | None = None, trace_id: str | None = None, page_size: int | str | None = None) -> EvidencePage:
        if visibility.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能读取其他厂区的证据")
        after, size = page_args(cursor, page_size)
        # 可见性在 SQL 里统一过滤（#122）：事件发生时刻与资料可用时刻都不晚于 as_of。
        # 存的是 ISO 文本，用 datetime() 归一化，避免 "Z" 与 "+00:00" 两种写法按字符串比较出错。
        sql, params = ("SELECT seq, record FROM evidence_events WHERE plant_id=? AND seq>? "
                       "AND datetime(json_extract(record,'$.occurred_at'))<=datetime(?) "
                       "AND (json_extract(record,'$.available_at') IS NULL "
                       "OR datetime(json_extract(record,'$.available_at'))<=datetime(?))"), [self.plant_id, after,
                                                                                         visibility.as_of.isoformat(),
                                                                                         visibility.as_of.isoformat()]
        for column, value in (("run_id", run_id), ("agent_run_id", agent_run_id), ("action_id", action_id), ("trace_id", trace_id)):
            if value:
                sql += f" AND {column}=?"
                params.append(value)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY seq LIMIT ?", (*params, size + 1)).fetchall())
        items = tuple(EvidenceEvent.model_validate_json(r[1]) for r in rows[:size])
        return EvidencePage(items=items, next_cursor=str(rows[size - 1][0]) if len(rows) > size else None)

    def latest_event_at(self, run_id: str) -> Any:
        """该运行已写入事件的最新发生时刻；场景运行没有回放游标时用它作为查询截止。"""
        rows = read(self.writer, lambda con: con.execute(
            "SELECT record FROM evidence_events WHERE plant_id=? AND run_id=? ORDER BY seq DESC LIMIT 50",
            (self.plant_id, run_id)).fetchall())
        return max((EvidenceEvent.model_validate_json(row[0]).occurred_at for row in rows), default=None)

    def run_as_of(self, run_id: str) -> Any:
        """带 run_id 查询的可见时刻：装配注入的运行时钟；没有时钟时返回 None（用只读上下文的 as_of）。"""
        if self.run_clock is None:
            return None
        try:
            return self.run_clock(run_id)
        except Exception:  # noqa: BLE001 — 运行没有可用时钟，交给调用方按只读上下文过滤
            return None

    def subscribe(self, trace_id: str, poll_s: float = 1.0) -> Iterator[EvidenceEvent]:
        """按 seq 顺序给出该 trace 的已有与新事件；阻塞等待，sink 关闭或调用方关闭迭代器时结束。"""
        after = 0
        while True:
            closed = self._closed  # 先取关闭标志再读：关闭前写入的事件至少再读一次
            rows = read(self.writer, lambda con: con.execute(
                "SELECT seq, record FROM evidence_events WHERE plant_id=? AND trace_id=? AND seq>? ORDER BY seq", (self.plant_id, trace_id, after)).fetchall())
            for seq, record in rows:
                after = seq
                yield EvidenceEvent.model_validate_json(record)
            if closed:
                return
            with self._changed:
                if not self._closed:
                    self._changed.wait(poll_s)

    def close(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()


def rest_providers(sink: SqliteEvidenceSink) -> dict[str, Callable[..., Any]]:
    def evidence(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> EvidencePage:
        run_id, action_id = query.get("run_id"), query.get("action_id")
        if action_id and not run_id:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "按动作查询证据时必须指定 run_id")
        # 带 run_id 时按运行时钟过滤（回放游标或该运行已写事件的最新时刻），否则用只读上下文的 as_of（#98）
        as_of = sink.run_as_of(run_id) if run_id else None
        visibility = context.model_copy(update={"as_of": as_of}) if as_of is not None else context
        return sink.list(run_id, query.get("cursor"), visibility, agent_run_id=query.get("agent_run_id"),
                         action_id=action_id, trace_id=query.get("trace_id"), page_size=query.get("page_size"))
    return {"/evidence": evidence}
