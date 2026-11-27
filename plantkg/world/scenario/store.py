"""场景账本表与运行记录（B17）。分钟账本、槽汇总分表存；按 run_id 读取供 B33 计分、B36 导出。"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import ScenarioSlotSummary, StateFrame
from plantkg.core.versions import atomic, read

from .ledger import MinuteRecord


class ScenarioStore:
    def __init__(self, writer: Any, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id

    # ---- 写（一槽一个短事务）
    def write_slot(self, run_id: str, minutes: Sequence[MinuteRecord], summary: ScenarioSlotSummary | None,
                   frame: StateFrame | None = None, records: Iterable[tuple[str, str, datetime, dict]] = ()) -> None:
        p = self.plant_id

        def write(con):
            con.executemany("INSERT OR REPLACE INTO b17_ledger_minutes(plant_id,run_id,minute,balanced,record) VALUES (?,?,?,?,?)",
                            [(p, run_id, m.entry.interval.start.isoformat(), int(m.balanced), json.dumps(m.to_json(), ensure_ascii=False))
                             for m in minutes])
            if summary is not None:
                con.execute("INSERT INTO b17_slot_summaries(plant_id,run_id,slot,summary) VALUES (?,?,?,?)",
                            (p, run_id, summary.interval.start.isoformat(), summary.model_dump_json()))
            if frame is not None:
                con.execute("INSERT OR REPLACE INTO b17_frames(plant_id,run_id,at,frame) VALUES (?,?,?,?)",
                            (p, run_id, frame.at.isoformat(), frame.model_dump_json()))
            self._records(con, run_id, records)
        atomic(self.writer, write)

    def write_records(self, run_id: str, records: Iterable[tuple[str, str, datetime, dict]]) -> None:
        records = list(records)
        if records:
            atomic(self.writer, lambda con: self._records(con, run_id, records))

    def _records(self, con, run_id, records) -> None:
        con.executemany("INSERT OR REPLACE INTO b17_records(plant_id,run_id,kind,record_id,at,record) VALUES (?,?,?,?,?,?)",
                        [(self.plant_id, run_id, kind, rid, at.isoformat(), json.dumps(rec, ensure_ascii=False, default=str))
                         for kind, rid, at, rec in records])

    def save_snapshot(self, run_id: str, at: datetime, snapshot: dict) -> None:
        atomic(self.writer, lambda con: con.execute("INSERT OR REPLACE INTO b17_snapshots(plant_id,run_id,at,snapshot) VALUES (?,?,?,?)",
                                                    (self.plant_id, run_id, at.isoformat(), json.dumps(snapshot, ensure_ascii=False))))

    def copy_before(self, parent: str, child: str, at: datetime, kinds: Sequence[str] = ("event", "action", "knowledge_version", "decision")) -> None:
        """分叉：复制父运行分叉时刻之前的分钟账本、已汇总的槽、状态帧与事件、动作、知识版本记录。
        时间列都是 isoformat（+00:00）文本，同一格式才能按字符串比较。"""
        p, cut = self.plant_id, at.isoformat()
        frame_cut = (at - timedelta(minutes=15)).isoformat()  # 只复制整帧都在分叉前的状态帧

        def write(con):
            con.execute("INSERT INTO b17_ledger_minutes SELECT plant_id,?,minute,balanced,record FROM b17_ledger_minutes "
                        "WHERE plant_id=? AND run_id=? AND minute<?", (child, p, parent, cut))
            con.execute("INSERT INTO b17_slot_summaries SELECT plant_id,?,slot,summary FROM b17_slot_summaries "
                        "WHERE plant_id=? AND run_id=? AND slot<=?", (child, p, parent, frame_cut))  # 槽起点 + 15 分钟 ≤ 分叉时刻
            con.execute("INSERT INTO b17_frames SELECT plant_id,?,at,frame FROM b17_frames WHERE plant_id=? AND run_id=? AND at<=?",
                        (child, p, parent, frame_cut))
            # 分叉时刻及之前写下的事件与知识版本（含触发分叉的那条）；动作按 apply 时刻，不按生效时刻
            marks = ",".join("?" * len(kinds))
            con.execute(f"INSERT INTO b17_records SELECT plant_id,?,kind,record_id,at,record FROM b17_records "
                        f"WHERE plant_id=? AND run_id=? AND kind IN ({marks}) AND kind<>'action' AND at<=?", (child, p, parent, *kinds, cut))
            con.execute("INSERT INTO b17_records SELECT plant_id,?,kind,record_id,at,record FROM b17_records "
                        "WHERE plant_id=? AND run_id=? AND kind='action' AND json_extract(record,'$.applied_at')<=?", (child, p, parent, cut))
        atomic(self.writer, write)

    # ---- 读（B33 计分、B36 导出）
    def ledger_minutes(self, run_id: str) -> list[MinuteRecord]:
        rows = read(self.writer, lambda con: con.execute("SELECT record FROM b17_ledger_minutes WHERE plant_id=? AND run_id=? ORDER BY minute",
                                                         (self.plant_id, run_id)).fetchall())
        return [MinuteRecord.from_json(json.loads(r[0])) for r in rows]

    def slot_summaries(self, run_id: str) -> list[ScenarioSlotSummary]:
        rows = read(self.writer, lambda con: con.execute("SELECT summary FROM b17_slot_summaries WHERE plant_id=? AND run_id=? ORDER BY slot",
                                                         (self.plant_id, run_id)).fetchall())
        return [ScenarioSlotSummary.model_validate_json(r[0]) for r in rows]

    def frames(self, run_id: str) -> list[StateFrame]:
        rows = read(self.writer, lambda con: con.execute("SELECT frame FROM b17_frames WHERE plant_id=? AND run_id=? ORDER BY at",
                                                         (self.plant_id, run_id)).fetchall())
        return [StateFrame.model_validate_json(r[0]) for r in rows]

    def records(self, run_id: str, kind: str) -> list[dict]:
        rows = read(self.writer, lambda con: con.execute("SELECT record FROM b17_records WHERE plant_id=? AND run_id=? AND kind=? ORDER BY at, record_id",
                                                         (self.plant_id, run_id, kind)).fetchall())
        return [json.loads(r[0]) for r in rows]

    def snapshot(self, run_id: str, at: datetime) -> dict | None:
        row = read(self.writer, lambda con: con.execute("SELECT snapshot FROM b17_snapshots WHERE plant_id=? AND run_id=? AND at=?",
                                                        (self.plant_id, run_id, at.isoformat())).fetchone())
        return json.loads(row[0]) if row else None
