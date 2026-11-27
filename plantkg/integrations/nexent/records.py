"""agent_runs 落库（day.schema.json 的 AgentRunRecord）：每次 Nexent 运行一条，供展示页“查看 Nexent 运行记录”。"""

from __future__ import annotations

import threading
from typing import Any

from plantkg.core.contracts import AgentRunRecord
from plantkg.core.versions import atomic, read


class MemoryAgentRunStore:
    """未开存储（mock）时的替身：只在进程内存里。"""

    def __init__(self) -> None:
        self._records: dict[str, AgentRunRecord] = {}
        self._lock = threading.Lock()

    def save(self, record: AgentRunRecord) -> None:
        with self._lock:
            self._records[record.agent_run_id] = record

    def get(self, agent_run_id: str) -> AgentRunRecord | None:
        return self._records.get(agent_run_id)

    def list(self, *, ticket_id: str | None = None, run_id: str | None = None) -> list[AgentRunRecord]:
        with self._lock:
            items = list(self._records.values())
        return sorted((r for r in items if _match(r, ticket_id, run_id)), key=lambda r: r.timing.requested_at)


class SqliteAgentRunStore:
    """与 B03 共用写者，表 b23_agent_runs。"""

    def __init__(self, writer: Any, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id

    def save(self, record: AgentRunRecord) -> None:
        binding = record.binding
        row = (self.plant_id, record.agent_run_id, binding.run_id if binding else None, binding.ticket_id if binding else None,
               record.kind, record.agent_name, record.timing.requested_at.isoformat(), record.model_dump_json())
        atomic(self.writer, lambda con: con.execute(
            "INSERT OR REPLACE INTO b23_agent_runs(plant_id,agent_run_id,run_id,ticket_id,kind,agent_name,requested_at,record) VALUES (?,?,?,?,?,?,?,?)", row))

    def get(self, agent_run_id: str) -> AgentRunRecord | None:
        row = read(self.writer, lambda con: con.execute("SELECT record FROM b23_agent_runs WHERE plant_id=? AND agent_run_id=?",
                                                        (self.plant_id, agent_run_id)).fetchone())
        return AgentRunRecord.model_validate_json(row[0]) if row else None

    def list(self, *, ticket_id: str | None = None, run_id: str | None = None) -> list[AgentRunRecord]:
        sql, params = "SELECT record FROM b23_agent_runs WHERE plant_id=?", [self.plant_id]
        for column, value in (("ticket_id", ticket_id), ("run_id", run_id)):
            if value:
                sql += f" AND {column}=?"
                params.append(value)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY requested_at", params).fetchall())
        return [AgentRunRecord.model_validate_json(r[0]) for r in rows]


def _match(record: AgentRunRecord, ticket_id: str | None, run_id: str | None) -> bool:
    binding = record.binding
    if ticket_id and (binding is None or binding.ticket_id != ticket_id):
        return False
    return not (run_id and (binding is None or binding.run_id != run_id))
