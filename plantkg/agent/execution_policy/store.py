"""分级执行的持久化（B47）：操作工单、执行幂等表、抽查/纠错队列、临时规则与自主等级。

一个 plant 一份；SQLite 单写者，读写走 core.versions 的 atomic/read。执行幂等键以
（run_id, proposal_id|预案 ID, 动作序号, round）为主键，重复提交直接忽略。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from plantkg.core.versions import atomic, read

from .records import ExecutionRecord, QueueItem, StoredTempRule, WorkOrderTask


class ExecutionStore:
    def __init__(self, writer: Any, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id

    # ---------------------------------------------------------------- 编号
    def next_id(self, prefix: str) -> str:
        def write(con):
            row = con.execute("SELECT value FROM b47_counters WHERE plant_id=? AND name=?",
                              (self.plant_id, prefix)).fetchone()
            value = (row[0] + 1) if row else 1
            con.execute("INSERT INTO b47_counters(plant_id,name,value) VALUES (?,?,?) "
                        "ON CONFLICT(plant_id,name) DO UPDATE SET value=excluded.value", (self.plant_id, prefix, value))
            return f"{prefix}-{value:04d}"
        return atomic(self.writer, write)

    # ---------------------------------------------------------------- 操作工单 / 人工待办
    def save_task(self, task: WorkOrderTask) -> WorkOrderTask:
        row = (self.plant_id, task.work_order_id, task.ticket_id, task.run_id, task.created_at.isoformat(), task.reason,
               task.kind, task.status, task.playbook_id, json.dumps([a.model_dump(mode="json") for a in task.actions],
                                                                     ensure_ascii=False),
               task.pending_action_id, task.confirmed_at.isoformat() if task.confirmed_at else None,
               task.done_at.isoformat() if task.done_at else None,
               (task.done_at or task.confirmed_at or task.created_at).isoformat(), task.model_dump_json())
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b47_work_orders(plant_id,work_order_id,ticket_id,run_id,created_at,reason,kind,status,playbook_id,"
            "actions,pending_action_id,confirmed_at,done_at,updated_at,record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plant_id,work_order_id) DO UPDATE SET ticket_id=excluded.ticket_id,run_id=excluded.run_id,"
            "reason=excluded.reason,kind=excluded.kind,status=excluded.status,playbook_id=excluded.playbook_id,"
            "actions=excluded.actions,pending_action_id=excluded.pending_action_id,confirmed_at=excluded.confirmed_at,"
            "done_at=excluded.done_at,updated_at=excluded.updated_at,record=excluded.record", row))
        return task

    def task(self, work_order_id: str) -> WorkOrderTask | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT record FROM b47_work_orders WHERE plant_id=? AND work_order_id=?",
            (self.plant_id, work_order_id)).fetchone())
        return WorkOrderTask.model_validate_json(row[0]) if row else None

    def tasks(self, ticket_id: str | None = None, status: str | None = None) -> list[WorkOrderTask]:
        sql = "SELECT record FROM b47_work_orders WHERE plant_id=?"
        params: list[Any] = [self.plant_id]
        if ticket_id is not None:
            sql += " AND ticket_id=?"
            params.append(ticket_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY created_at, work_order_id", params).fetchall())
        return [WorkOrderTask.model_validate_json(r[0]) for r in rows]

    def pending_task(self, ticket_id: str, run_id: str | None = None) -> WorkOrderTask | None:
        """该事件单最近一张待确认的工单/人工待办；同轮只保留一张，新事项并入它。

        暂停自动执行期间的积压提议（autonomy_paused）一条一张、逐条批准，不参与“并入”。
        """
        sql = "SELECT record FROM b47_work_orders WHERE plant_id=? AND ticket_id=? AND status='proposed' "
        params = [self.plant_id, ticket_id]
        if run_id is not None:
            sql += "AND run_id=? "
            params.append(run_id)
        rows = read(self.writer, lambda con: con.execute(
            sql + "ORDER BY created_at DESC, work_order_id DESC", params).fetchall())
        tasks = (WorkOrderTask.model_validate_json(row[0]) for row in rows)
        return next((task for task in tasks if not task.autonomy_paused), None)

    # ---------------------------------------------------------------- 暂停自动执行（#245）
    # 存在 B27 的事件单状态表里：作用域键借用 ticket_id 位置（“@autonomy:plant” 整厂、“@autonomy:run:<run_id>” 单个运行）。
    @staticmethod
    def pause_scope(run_id: str | None) -> str:
        return f"@autonomy:run:{run_id}" if run_id else "@autonomy:plant"

    def pause_record(self, run_id: str | None) -> dict[str, Any] | None:
        """该作用域最近一次暂停记录（含已恢复的）；没有暂停过返回 None。"""
        row = read(self.writer, lambda con: con.execute(
            "SELECT value FROM b27_ticket_state WHERE plant_id=? AND ticket_id=? AND key='pause'",
            (self.plant_id, self.pause_scope(run_id))).fetchone())
        return json.loads(row[0]) if row else None

    def save_pause(self, run_id: str | None, record: dict[str, Any]) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b27_ticket_state(plant_id,ticket_id,key,value) VALUES (?,?,'pause',?) "
            "ON CONFLICT(plant_id,ticket_id,key) DO UPDATE SET value=excluded.value",
            (self.plant_id, self.pause_scope(run_id), json.dumps(record, ensure_ascii=False))))

    def active_pause(self, run_id: str | None) -> dict[str, Any] | None:
        """生效中的暂停：运行作用域优先，其次整厂；返回记录并带上 scope。"""
        for scope_run in ((run_id, None) if run_id else (None,)):
            record = self.pause_record(scope_run)
            if record is not None and not record.get("resumed_at"):
                return record | {"scope": "run" if scope_run else "plant", "run_id": scope_run}
        return None

    # ---------------------------------------------------------------- 执行幂等
    def execution(self, run_id: str, key: str) -> ExecutionRecord | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT key,run_id,ticket_id,proposal_id,round,action_index,entity_id,command,status,at,detail FROM b47_executions "
            "WHERE plant_id=? AND run_id=? AND key=?", (self.plant_id, run_id, key)).fetchone())
        if row is None:
            return None
        return ExecutionRecord(key=row[0], run_id=row[1], ticket_id=row[2], proposal_id=row[3], round=row[4],
                               action_index=row[5], entity_id=row[6], command=row[7], status=row[8],
                               at=datetime.fromisoformat(row[9]), detail=row[10] or "")

    def claim_execution(self, record: ExecutionRecord) -> bool:
        """第一次登记该幂等键返回 True；重复提交返回 False（不覆盖已有结果）。"""
        def write(con):
            exist = con.execute("SELECT 1 FROM b47_executions WHERE plant_id=? AND run_id=? AND key=?",
                                (self.plant_id, record.run_id, record.key)).fetchone()
            if exist:
                return False
            con.execute(
                "INSERT INTO b47_executions(plant_id,run_id,key,ticket_id,proposal_id,round,action_index,entity_id,command,"
                "status,at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.plant_id, record.run_id, record.key, record.ticket_id, record.proposal_id, record.round,
                 record.action_index, record.entity_id, record.command, record.status, record.at.isoformat(),
                 record.detail))
            return True
        return atomic(self.writer, write)

    def save_execution(self, record: ExecutionRecord) -> None:
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b47_executions(plant_id,run_id,key,ticket_id,proposal_id,round,action_index,entity_id,command,"
            "status,at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(plant_id,run_id,key) DO UPDATE SET "
            "status=excluded.status,at=excluded.at,detail=excluded.detail",
            (self.plant_id, record.run_id, record.key, record.ticket_id, record.proposal_id, record.round,
             record.action_index, record.entity_id, record.command, record.status, record.at.isoformat(), record.detail)))

    def executions(self, ticket_id: str | None = None, run_id: str | None = None) -> list[ExecutionRecord]:
        sql = "SELECT key,run_id,ticket_id,proposal_id,round,action_index,entity_id,command,status,at,detail FROM b47_executions WHERE plant_id=?"
        params: list[Any] = [self.plant_id]
        if ticket_id is not None:
            sql += " AND ticket_id=?"
            params.append(ticket_id)
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY at, key", params).fetchall())
        return [ExecutionRecord(key=r[0], run_id=r[1], ticket_id=r[2], proposal_id=r[3], round=r[4], action_index=r[5],
                                entity_id=r[6], command=r[7], status=r[8], at=datetime.fromisoformat(r[9]),
                                detail=r[10] or "") for r in rows]

    # ---------------------------------------------------------------- 抽查 / 纠错 / 先做后审队列
    def add_queue(self, item: QueueItem) -> QueueItem:
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b47_queues(plant_id,kind,item_id,ticket_id,run_id,at,status,record) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plant_id,kind,item_id) DO UPDATE SET status=excluded.status,record=excluded.record",
            (self.plant_id, item.kind, item.item_id, item.ticket_id, item.run_id, item.at.isoformat(), item.status,
             json.dumps(item.record, ensure_ascii=False, default=str))))
        return item

    def queue(self, kind: str | None = None, status: str | None = None) -> list[QueueItem]:
        sql = "SELECT kind,item_id,ticket_id,run_id,at,status,record FROM b47_queues WHERE plant_id=?"
        params: list[Any] = [self.plant_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY at, item_id", params).fetchall())
        return [QueueItem(kind=r[0], item_id=r[1], ticket_id=r[2], run_id=r[3], at=datetime.fromisoformat(r[4]),
                          status=r[5], record=json.loads(r[6])) for r in rows]

    def resolve_queue(self, kind: str, item_id: str, status: str, record: dict[str, Any] | None = None) -> None:
        def write(con):
            row = con.execute("SELECT record FROM b47_queues WHERE plant_id=? AND kind=? AND item_id=?",
                              (self.plant_id, kind, item_id)).fetchone()
            if row is None:
                return
            merged = json.loads(row[0]) | dict(record or {})
            con.execute("UPDATE b47_queues SET status=?, record=? WHERE plant_id=? AND kind=? AND item_id=?",
                        (status, json.dumps(merged, ensure_ascii=False, default=str), self.plant_id, kind, item_id))
        atomic(self.writer, write)

    # ---------------------------------------------------------------- 临时规则
    def save_temp_rule(self, stored: StoredTempRule) -> None:
        rule = stored.rule
        atomic(self.writer, lambda con: con.execute(
            "INSERT INTO b47_temp_rules(plant_id,rule_id,run_id,created_at,start_at,end_at,text,entity_ids,"
            "estimated_cost_cny,source_event_id,status,record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plant_id,rule_id) DO UPDATE SET run_id=excluded.run_id,status=excluded.status,record=excluded.record",
            (self.plant_id, rule.rule_id, stored.run_id, rule.created_at.isoformat(), rule.effective.start.isoformat(),
             rule.effective.end.isoformat(), rule.text, json.dumps(list(rule.entity_ids), ensure_ascii=False),
             rule.estimated_cost_cny, rule.source_event_id, stored.status, stored.model_dump_json())))

    def temp_rules(self, run_id: str | None = None, status: str | None = None) -> list[StoredTempRule]:
        sql = "SELECT record FROM b47_temp_rules WHERE plant_id=?"
        params: list[Any] = [self.plant_id]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY created_at, rule_id", params).fetchall())
        return [StoredTempRule.model_validate_json(r[0]) for r in rows]

    def expire_temp_rules(self, now: datetime) -> list[StoredTempRule]:
        expired = []
        for stored in self.temp_rules(status="active"):
            if stored.rule.effective.end <= now:
                stored = stored.model_copy(update={"status": "expired"})
                self.save_temp_rule(stored)
                expired.append(stored)
        return expired

    # ---------------------------------------------------------------- 自主等级与“见过的类别”
    def category(self, event_type: str, entity_type: str | None = None) -> str:
        return f"{event_type}|{entity_type}" if entity_type else event_type

    def mark_seen(self, category: str, at: datetime, level: str | None = None) -> None:
        def write(con):
            row = con.execute("SELECT level FROM b47_category_levels WHERE plant_id=? AND category=?",
                              (self.plant_id, category)).fetchone()
            if row is None:
                con.execute("INSERT INTO b47_category_levels(plant_id,category,level,seen_at,updated_at) VALUES (?,?,?,?,?)",
                            (self.plant_id, category, level or "L0", at.isoformat(), at.isoformat()))
            elif level is not None and row[0] != level:
                con.execute("UPDATE b47_category_levels SET level=?, updated_at=? WHERE plant_id=? AND category=?",
                            (level, at.isoformat(), self.plant_id, category))
        atomic(self.writer, write)

    def level(self, category: str) -> tuple[str, bool] | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT level FROM b47_category_levels WHERE plant_id=? AND category=?",
            (self.plant_id, category)).fetchone())
        return (row[0], True) if row else None

    def set_level(self, category: str, level: str, at: datetime) -> None:
        self.mark_seen(category, at, level)

    def legacy_downgrade(self, category: str) -> str | None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT level FROM b47_category_levels WHERE plant_id=? AND category=?",
            (self.plant_id, category)).fetchone())
        return row[0] if row else None

    def seed_run_caps(self, run_id: str, at: datetime, resolve=None) -> None:
        rows = read(self.writer, lambda con: con.execute(
            "SELECT category,level FROM b47_category_levels WHERE plant_id=? AND substr(category,1,1) <> '@'",
            (self.plant_id,)).fetchall())
        for category, level in rows:
            key = f'@run:{run_id}:{category}'
            if self.level(key) is None:
                self.set_level(key, resolve(category) if resolve else level, at)

    def copy_run_caps(self, parent: str, child: str, at: datetime) -> list[str]:
        prefix = f"@run:{parent}:"
        rows = read(self.writer, lambda con: con.execute(
            "SELECT category,level FROM b47_category_levels WHERE plant_id=? AND substr(category,1,?)=?",
            (self.plant_id, len(prefix), prefix)).fetchall())
        for category, level in rows:
            self.set_level(f"@run:{child}:" + category[len(prefix):], level, at)
        return [category[len(prefix):] for category, _ in rows]

    def publish_manifest_caps(self, parent: str, child: str, category: str | None,
                              action_id: str, at: datetime) -> None:
        marker, stamp = f'@authorization-action:{action_id}', at.isoformat()
        def write(con):
            if con.execute('SELECT 1 FROM b47_category_levels WHERE plant_id=? AND category=?',
                           (self.plant_id, marker)).fetchone():
                return
            for kind in ('release', 'policy'):
                prefix = f'@{kind}:{parent}:'
                rows = con.execute('SELECT category,level FROM b47_category_levels '
                                   'WHERE plant_id=? AND substr(category,1,?)=?',
                                   (self.plant_id, len(prefix), prefix)).fetchall()
                for key, level in rows:
                    new_key = f'@{kind}:{child}:' + key[len(prefix):]
                    con.execute('INSERT OR IGNORE INTO b47_category_levels '
                                '(plant_id,category,level,seen_at,updated_at) VALUES (?,?,?,?,?)',
                                (self.plant_id, new_key, level, stamp, stamp))
            if category:
                for kind in ('release', 'policy'):
                    key = f'@{kind}:{child}:{category}'
                    con.execute('INSERT INTO b47_category_levels (plant_id,category,level,seen_at,updated_at) '
                                'VALUES (?,?,?,?,?) ON CONFLICT(plant_id,category) '
                                'DO UPDATE SET level=excluded.level,updated_at=excluded.updated_at',
                                (self.plant_id, key, 'L3', stamp, stamp))
            con.execute('INSERT INTO b47_category_levels (plant_id,category,level,seen_at,updated_at) '
                        'VALUES (?,?,?,?,?)', (self.plant_id, marker, 'L3', stamp, stamp))
        atomic(self.writer, write)
