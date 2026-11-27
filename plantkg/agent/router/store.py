"""值守层持久化（B27）：事件准入记录、事件单与订阅、派发轮次、研判记录与合批队列。

事件单、订阅与批次状态存 SQLite（表见 migrations/tickets）；订阅键是（图谱实体 ID，事件类型集合），多对多。
"""

from __future__ import annotations

import json
import threading
import weakref
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from plantkg.core.contracts import AgentRunRecord, DayEvent, RunBinding, Ticket, TicketSubscription
from plantkg.core.versions import atomic, read

WATCHDOG_TICKETS_HOOK = "watchdog.tickets"  # fn() -> Sequence[TicketInspectionSource]：B42 每次巡检取当前运行视图
TICKET_WRITEBACK_HOOK = "router.ticket_writeback"  # fn(ticket_id, run_id) -> bool：B47 无绑定拒绝时按运行作用域兜底回写


class _TicketRevision:
    """事件单变更计数的锁与水位：按 writer 索引，不给 writer 加属性（#207 终审）。"""

    __slots__ = ("lock", "value")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value = 0


_revision_guard = threading.Lock()
_revision_state: weakref.WeakKeyDictionary[Any, _TicketRevision] = weakref.WeakKeyDictionary()


def _revision_of(writer: Any) -> _TicketRevision:
    """同一个库的所有 RouterStore 实例与旁路回写共用一个水位；writer 回收后计数随之释放。"""
    with _revision_guard:
        state = _revision_state.get(writer)
        if state is None:
            state = _revision_state[writer] = _TicketRevision()
        return state


class RouterStore:
    def __init__(self, writer: Any, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id
        _revision_of(writer)  # 装配时先建水位

    @property
    def ticket_revision(self) -> int:
        """事件单变更计数：场景 _sync 只在有写入时重读事件单；多实例共用同一水位。"""
        return _revision_of(self.writer).value

    def _touch_ticket(self) -> None:
        state = _revision_of(self.writer)
        with state.lock:
            state.value += 1

    # ---- 事件准入记录
    def record_event(self, event: DayEvent, result: str, ticket_id: str | None = None) -> None:
        row = (self.plant_id, event.event_id, event.at.isoformat(), event.event_type, event.entity_id, event.severity,
               event.source, result, ticket_id, event.model_dump_json())
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_events(plant_id,event_id,at,event_type,entity_id,severity,source,result,ticket_id,record) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", row))

    def event(self, event_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT record,result,ticket_id FROM b27_events WHERE plant_id=? AND event_id=?", (self.plant_id, event_id)).fetchone())
        if row is None:
            return None
        return {"event": DayEvent.model_validate_json(row[0]), "result": row[1], "ticket_id": row[2]}

    def event_ticket(self, event_id: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT ticket_id FROM b27_events WHERE plant_id=? AND event_id=?", (self.plant_id, event_id)).fetchone())
        return row[0] if row else None

    def last_event_for_entity(self, entity_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT record FROM b27_events WHERE plant_id=? AND entity_id=? ORDER BY at DESC, event_id DESC LIMIT 1",
            (self.plant_id, entity_id)).fetchone())
        return json.loads(row[0]) if row else None

    # ---- 事件单
    def save_ticket(self, ticket: Ticket, *, run_id: str, running: bool | None = None) -> None:
        def write(con):
            current = con.execute("SELECT running FROM b27_tickets WHERE plant_id=? AND ticket_id=?",
                                  (self.plant_id, ticket.ticket_id)).fetchone()
            flag = current[0] if (current is not None and running is None) else int(bool(running))
            con.execute(
                "INSERT INTO b27_tickets(plant_id,ticket_id,run_id,status,round,opened_at,closed_at,rehearsal,summary,experts,"
                "event_ids,running) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(plant_id,ticket_id) DO UPDATE SET "
                "run_id=excluded.run_id,status=excluded.status,round=excluded.round,closed_at=excluded.closed_at,"
                "summary=excluded.summary,experts=excluded.experts,event_ids=excluded.event_ids,running=excluded.running",
                (self.plant_id, ticket.ticket_id, run_id, ticket.status, ticket.round, ticket.opened_at.isoformat(),
                 ticket.closed_at.isoformat() if ticket.closed_at else None, int(ticket.rehearsal), ticket.summary,
                 json.dumps(list(ticket.experts), ensure_ascii=False), json.dumps(list(ticket.event_ids), ensure_ascii=False), flag))
        atomic(self.writer, write)
        self._touch_ticket()

    def ticket(self, ticket_id: str) -> Ticket | None:
        rows = read(self.writer, lambda con: (
            con.execute("SELECT plant_id,ticket_id,status,round,opened_at,closed_at,rehearsal,summary,experts,event_ids "
                        "FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone(),
            con.execute("SELECT entity_id,event_types FROM b27_subscriptions WHERE plant_id=? AND ticket_id=? AND active=1 "
                        "ORDER BY entity_id", (self.plant_id, ticket_id)).fetchall()))
        head, subs = rows
        if head is None:
            return None
        return Ticket(ticket_id=head[1], status=head[2], round=head[3], opened_at=datetime.fromisoformat(head[4]),
                      closed_at=datetime.fromisoformat(head[5]) if head[5] else None, rehearsal=bool(head[6]), summary=head[7],
                      experts=tuple(json.loads(head[8])), event_ids=tuple(json.loads(head[9])),
                      subscriptions=tuple(TicketSubscription(entity_id=s[0], event_types=tuple(json.loads(s[1]))) for s in subs))

    def open_tickets(self) -> list[Ticket]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT ticket_id FROM b27_tickets WHERE plant_id=? AND status NOT IN ('resolved','closed') ORDER BY opened_at, ticket_id",
            (self.plant_id,)).fetchall())
        return [t for t in (self.ticket(r[0]) for r in rows) if t is not None]

    def set_running(self, ticket_id: str, running: bool) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_tickets SET running=? WHERE plant_id=? AND ticket_id=?", (int(running), self.plant_id, ticket_id)))
        self._touch_ticket()

    def is_running(self, ticket_id: str) -> bool:
        row = read(self.writer, lambda con: con.execute(
            "SELECT running FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone())
        return bool(row[0]) if row else False

    def run_of_ticket(self, ticket_id: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT run_id FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone())
        return row[0] if row else None

    def save_subscriptions(self, ticket_id: str, subscriptions: Iterable[TicketSubscription], *, active: bool = True) -> None:
        rows = [(self.plant_id, ticket_id, s.entity_id, json.dumps(list(s.event_types), ensure_ascii=False), int(active))
                for s in subscriptions]
        if not rows:
            return
        atomic(self.writer, lambda con: con.executemany(
            "INSERT INTO b27_subscriptions(plant_id,ticket_id,entity_id,event_types,active) VALUES (?,?,?,?,?) "
            "ON CONFLICT(plant_id,ticket_id,entity_id) DO UPDATE SET event_types=excluded.event_types,active=excluded.active", rows))
        self._touch_ticket()

    def matches(self, entity_id: str | None, event_type: str) -> list[str]:
        """订阅命中的事件单：同一实体且事件类型在订阅集合里；多张事件单可同时订阅同一事件流。"""
        if not entity_id:
            return []
        rows = read(self.writer, lambda con: con.execute(
            "SELECT ticket_id,event_types FROM b27_subscriptions WHERE plant_id=? AND entity_id=? AND active=1 ORDER BY ticket_id",
            (self.plant_id, entity_id)).fetchall())
        return [r[0] for r in rows if event_type in json.loads(r[1])]

    def tickets_for_entity(self, entity_id: str) -> list[str]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT DISTINCT ticket_id FROM b27_subscriptions WHERE plant_id=? AND entity_id=? AND active=1 ORDER BY ticket_id",
            (self.plant_id, entity_id)).fetchall())
        return [r[0] for r in rows]

    def close_ticket(self, ticket_id: str, at: datetime, summary: str) -> None:
        def write(con):
            con.execute("UPDATE b27_tickets SET status='closed',closed_at=?,summary=?,probe=NULL,running=0 "
                        "WHERE plant_id=? AND ticket_id=?",
                        (at.isoformat(), summary, self.plant_id, ticket_id))
            con.execute("UPDATE b27_subscriptions SET active=0 WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id))
        atomic(self.writer, write)
        self._touch_ticket()

    def set_ticket_state(self, ticket_id: str, key: str, value: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b27_ticket_state(plant_id,ticket_id,key,value) VALUES (?,?,?,?) "
            "ON CONFLICT(plant_id,ticket_id,key) DO UPDATE SET value=excluded.value",
            (self.plant_id, ticket_id, key, value)))

    def ticket_state(self, ticket_id: str, key: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT value FROM b27_ticket_state WHERE plant_id=? AND ticket_id=? AND key=?",
            (self.plant_id, ticket_id, key)).fetchone())
        return row[0] if row else None

    # ---- 派发轮次
    def save_round(self, binding: RunBinding, experts: Sequence[str], reason: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_rounds(plant_id,ticket_id,round,trigger_at,knowledge_version,state_version,run_id,experts,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.plant_id, binding.ticket_id, binding.round, binding.trigger_at.isoformat(), binding.knowledge_version,
             binding.state_version, binding.run_id, json.dumps(list(experts), ensure_ascii=False), reason)))

    def round_trigger(self, ticket_id: str, round_: int) -> datetime | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT trigger_at FROM b27_rounds WHERE plant_id=? AND ticket_id=? AND round=?",
            (self.plant_id, ticket_id, round_)).fetchone())
        return datetime.fromisoformat(row[0]) if row else None

    def round_binding(self, ticket_id: str, round_: int) -> tuple[str, int] | None:
        """派发该轮时的（运行，知识版本）；这一轮没派发过时为空。"""
        row = read(self.writer, lambda con: con.execute(
            "SELECT run_id,knowledge_version FROM b27_rounds WHERE plant_id=? AND ticket_id=? AND round=?",
            (self.plant_id, ticket_id, round_)).fetchone())
        return (row[0], row[1]) if row else None

    def claim_round(self, ticket_id: str, round_: int) -> bool:
        """认领本轮完成：同一轮只有第一次成功。"""
        def write(con):
            row = con.execute("SELECT value FROM b27_ticket_state WHERE plant_id=? AND ticket_id=? AND key='completed_round'",
                              (self.plant_id, ticket_id)).fetchone()
            if row is not None and int(row[0]) >= round_:
                return False
            con.execute("INSERT INTO b27_ticket_state(plant_id,ticket_id,key,value) VALUES (?,?,'completed_round',?) "
                        "ON CONFLICT(plant_id,ticket_id,key) DO UPDATE SET value=excluded.value",
                        (self.plant_id, ticket_id, str(round_)))
            return True
        return atomic(self.writer, write)

    # ---- 研判记录
    def save_triage(self, ticket_id: str, round_: int, *, at: datetime, gate_accept: bool, domains: dict[str, float],
                    source: str, agent_run_id: str | None, coverage_gap: bool, summary: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_triage(plant_id,ticket_id,round,at,gate_accept,domains,source,agent_run_id,coverage_gap,summary) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.plant_id, ticket_id, round_, at.isoformat(), int(gate_accept), json.dumps(domains, ensure_ascii=False),
             source, agent_run_id, int(coverage_gap), summary)))

    def triage(self, ticket_id: str, round_: int) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT at,gate_accept,domains,source,agent_run_id,coverage_gap,summary FROM b27_triage "
            "WHERE plant_id=? AND ticket_id=? AND round=?", (self.plant_id, ticket_id, round_)).fetchone())
        if row is None:
            return None
        return {"at": row[0], "gate_accept": bool(row[1]), "domains": json.loads(row[2]), "source": row[3],
                "agent_run_id": row[4], "coverage_gap": bool(row[5]), "summary": row[6]}

    # ---- 编号与运行记录
    def next_ticket_id(self) -> str:
        def write(con):
            row = con.execute("SELECT value FROM b27_counters WHERE plant_id=? AND name='ticket'", (self.plant_id,)).fetchone()
            value = (row[0] + 1) if row else 1
            con.execute("INSERT INTO b27_counters(plant_id,name,value) VALUES (?,'ticket',?) "
                        "ON CONFLICT(plant_id,name) DO UPDATE SET value=excluded.value", (self.plant_id, value))
            return f"ticket://TK-{value:04d}"
        return atomic(self.writer, write)

    def save_agent_run(self, record: Any) -> None:
        binding = record.binding
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_agent_runs(plant_id,agent_run_id,ticket_id,at,source,coverage_gap,record) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.plant_id, record.agent_run_id, binding.ticket_id if binding else None, record.timing.requested_at.isoformat(),
             record.source, int(record.coverage_gap), record.model_dump_json())))

    def agent_runs(self, ticket_id: str) -> list[AgentRunRecord]:
        """B27 记录的主 Agent 运行（triage/investigate）；B42 巡检读运行记录走这个端口，没有时再退回 B23。"""
        rows = read(self.writer, lambda con: con.execute(
            "SELECT record FROM b27_agent_runs WHERE plant_id=? AND ticket_id=? ORDER BY at, agent_run_id",
            (self.plant_id, ticket_id)).fetchall())
        return [AgentRunRecord.model_validate_json(r[0]) for r in rows]

    @staticmethod
    def inspection_scopes(writer: Any, plant_id: str) -> list[str]:
        """当前有未关闭事件单的作用域：主路由（plant_id）与各协同组运行（plant_id#run_id）。"""
        rows = read(writer, lambda con: con.execute(
            "SELECT DISTINCT plant_id FROM b27_tickets WHERE status NOT IN ('resolved','closed') AND (plant_id=? OR plant_id LIKE ?) ORDER BY plant_id",
            (plant_id, plant_id + '#%')).fetchall())
        return [r[0] for r in rows]

    def events_of(self, ticket_id: str) -> list[tuple[str, datetime]]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT event_id,at FROM b27_events WHERE plant_id=? AND ticket_id=? AND result IN ('opened','merged','straight_through') "
            "ORDER BY at, event_id", (self.plant_id, ticket_id)).fetchall())
        return [(r[0], datetime.fromisoformat(r[1])) for r in rows]

    # ---- 合批队列
    def add_pending(self, item_id: str, *, at: datetime, kind: str, ticket_id: str, record: dict[str, Any]) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_pending(plant_id,item_id,at,kind,ticket_id,record,taken) VALUES (?,?,?,?,?,?,0)",
            (self.plant_id, item_id, at.isoformat(), kind, ticket_id, json.dumps(record, ensure_ascii=False))))

    def pending(self) -> list[dict[str, Any]]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT item_id,at,kind,ticket_id,record FROM b27_pending WHERE plant_id=? AND taken=0 ORDER BY at, item_id",
            (self.plant_id,)).fetchall())
        return [{"item_id": r[0], "at": r[1], "kind": r[2], "ticket_id": r[3], "record": json.loads(r[4])} for r in rows]

    def take_pending(self, item_ids: Sequence[str]) -> None:
        if not item_ids:
            return
        marks = ",".join("?" * len(item_ids))
        atomic(self.writer, lambda con: con.execute(
            f"UPDATE b27_pending SET taken=1 WHERE plant_id=? AND item_id IN ({marks})", (self.plant_id, *item_ids)))

    # ---- 通道、严重程度、调查、提问与试探（D53/D55/D61）
    def set_channel(self, ticket_id: str, channel: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_tickets SET channel=? WHERE plant_id=? AND ticket_id=?", (channel, self.plant_id, ticket_id)))
        self._touch_ticket()

    def channel_of(self, ticket_id: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT channel FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone())
        return row[0] if row else None

    def save_severity(self, ticket_id: str, estimate: Any) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_tickets SET severity=? WHERE plant_id=? AND ticket_id=?",
            (estimate.model_dump_json(), self.plant_id, ticket_id)))
        self._touch_ticket()

    def severity_of(self, ticket_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT severity FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone())
        return json.loads(row[0]) if row and row[0] else None

    def save_investigation(self, ticket_id: str, round_: int, *, at: datetime, source: str, agent_run_id: str | None,
                           coverage_gap: bool, diagnosis: str, experts: Sequence[str], evidence_refs: Sequence[str],
                           probe: dict[str, Any] | None) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b27_investigations(plant_id,ticket_id,round,at,source,agent_run_id,coverage_gap,"
            "diagnosis,experts,evidence_refs,probe) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.plant_id, ticket_id, round_, at.isoformat(), source, agent_run_id, int(coverage_gap), diagnosis,
             json.dumps(list(experts), ensure_ascii=False), json.dumps(list(evidence_refs), ensure_ascii=False),
             json.dumps(probe, ensure_ascii=False) if probe else None)))

    def investigation(self, ticket_id: str, round_: int) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT at,source,agent_run_id,coverage_gap,diagnosis,experts,evidence_refs,probe FROM b27_investigations "
            "WHERE plant_id=? AND ticket_id=? AND round=?", (self.plant_id, ticket_id, round_)).fetchone())
        if row is None:
            return None
        return {"at": row[0], "source": row[1], "agent_run_id": row[2], "coverage_gap": bool(row[3]), "diagnosis": row[4],
                "experts": json.loads(row[5]), "evidence_refs": json.loads(row[6]),
                "probe": json.loads(row[7]) if row[7] else None}

    def save_probe(self, ticket_id: str, probe: dict[str, Any]) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_tickets SET probe=? WHERE plant_id=? AND ticket_id=?",
            (json.dumps(probe, ensure_ascii=False, default=str), self.plant_id, ticket_id)))
        self._touch_ticket()

    def probe_of(self, ticket_id: str) -> dict[str, Any] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT probe FROM b27_tickets WHERE plant_id=? AND ticket_id=?", (self.plant_id, ticket_id)).fetchone())
        return json.loads(row[0]) if row and row[0] else None

    def due_probes(self, now: datetime) -> list[dict[str, Any]]:
        """观察窗口已结束或试探已失败、还在等接续调查的事件单；已关闭或已解决的不再复活。"""
        rows = read(self.writer, lambda con: con.execute(
            "SELECT ticket_id,probe FROM b27_tickets WHERE plant_id=? AND probe IS NOT NULL AND "
            "status NOT IN ('closed','resolved') AND "
            "(json_extract(probe,'$.state') IN ('rejected','expired','infeasible') OR "
            "(json_extract(probe,'$.state')='executing' AND json_extract(probe,'$.window_end')<=?))",
            (self.plant_id, now.isoformat())).fetchall())
        return [{"ticket_id": r[0], "probe": json.loads(r[1])} for r in rows]

    def set_ticket_status(self, ticket_id: str, status: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_tickets SET status=? WHERE plant_id=? AND ticket_id=?", (status, self.plant_id, ticket_id)))
        self._touch_ticket()

    @staticmethod
    def block_ticket_for_reject(writer: Any, ticket_id: str, run_id: str) -> bool:
        """统一端点拒绝工单/人工待办时的兜底回写：按任务 run_id 直接落库，不依赖活动 Router 实例。

        只改本运行作用域内、未结束的事件单；已经 blocked 的视为成功（可重试幂等）。找不到事件单或已结束返回 False。
        """
        def write(con):
            row = con.execute("SELECT plant_id,status FROM b27_tickets WHERE ticket_id=? AND run_id=?",
                              (ticket_id, run_id)).fetchone()
            if row is None or row[1] in ("resolved", "closed"):
                return None
            con.execute("UPDATE b27_tickets SET status='blocked' WHERE plant_id=? AND ticket_id=?",
                        (row[0], ticket_id))
            return row[0]
        scope = atomic(writer, write)
        if scope is None:
            return False
        state = _revision_of(writer)  # 与同库 RouterStore 共用水位，场景 _sync 能感知这次旁路回写
        with state.lock:
            state.value += 1
        return True

    def block_ticket_if_idle(self, ticket_id: str, round_: int) -> bool:
        """巡检判定受阻后回写：事件单仍是本轮打开、空闲、非紧急且无待办时才改状态；改到行返回 True。"""
        def write(con):
            return con.execute(
                "UPDATE b27_tickets SET status='blocked' WHERE plant_id=? AND ticket_id=? AND status='open' AND running=0 AND round=? "
                "AND COALESCE(channel,'')!='urgent' AND (probe IS NULL OR json_extract(probe,'$.state') NOT IN ('pending','proposed','executing')) "
                "AND NOT EXISTS (SELECT 1 FROM b27_questions q WHERE q.plant_id=b27_tickets.plant_id AND q.ticket_id=b27_tickets.ticket_id AND q.status='pending') "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.plant_id=b27_tickets.plant_id AND j.status IN ('pending','running') AND json_extract(j.record,'$.run_id')=b27_tickets.run_id)",
                (self.plant_id, ticket_id, round_)).rowcount == 1
        updated = atomic(self.writer, write)
        if updated:
            self._touch_ticket()
        return updated

    # ---- 值班提问（D54）
    def ask(self, ticket_id: str, question: str, at: datetime) -> str:
        seq = len(self.questions(ticket_id)) + 1
        question_id = f"question://{ticket_id.rsplit('/', 1)[-1]}-{seq}"
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b27_questions(plant_id,question_id,ticket_id,question,answer,status,at) VALUES (?,?,?,?,NULL,'pending',?)",
            (self.plant_id, question_id, ticket_id, question, at.isoformat())))
        return question_id

    def answer(self, question_id: str, answer: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "UPDATE b27_questions SET answer=?,status='answered' WHERE plant_id=? AND question_id=?",
            (answer, self.plant_id, question_id)))

    def questions(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT question_id,question,answer,status,at FROM b27_questions WHERE plant_id=? AND ticket_id=? ORDER BY at, question_id",
            (self.plant_id, ticket_id)).fetchall())
        return [{"question_id": r[0], "question": r[1], "answer": r[2], "status": r[3], "at": r[4]} for r in rows]


class TicketInspectionSource:
    """B27 给 B42 的只读巡检视图：作用域由构造它的运行决定，B42 不再假定 settings.plant_id。

    读事件单、提问、试探与 B27 运行记录；巡检结论写回同一作用域的 ticket_state。
    """
    def __init__(self, store: RouterStore) -> None:
        self.store = store
        self.writer, self.plant_id = store.writer, store.plant_id

    def open_tickets(self) -> list[Ticket]:
        return self.store.open_tickets()

    def ticket(self, ticket_id: str) -> Ticket | None:
        return self.store.ticket(ticket_id)

    def questions(self, ticket_id: str) -> list[dict[str, Any]]:
        return self.store.questions(ticket_id)

    def probe_of(self, ticket_id: str) -> dict[str, Any] | None:
        return self.store.probe_of(ticket_id)

    def run_of_ticket(self, ticket_id: str) -> str | None:
        return self.store.run_of_ticket(ticket_id)

    def is_running(self, ticket_id: str) -> bool:
        return self.store.is_running(ticket_id)

    def channel_of(self, ticket_id: str) -> str | None:
        return self.store.channel_of(ticket_id)

    def agent_runs(self, ticket_id: str) -> list[AgentRunRecord]:
        return self.store.agent_runs(ticket_id)

    def conclusion(self, ticket_id: str) -> dict[str, Any] | None:
        raw = self.store.ticket_state(ticket_id, "watchdog_conclusion")
        return json.loads(raw) if raw else None

    def save_conclusion(self, ticket_id: str, value: dict[str, Any]) -> None:
        self.store.set_ticket_state(ticket_id, "watchdog_conclusion", json.dumps(value, ensure_ascii=False))

