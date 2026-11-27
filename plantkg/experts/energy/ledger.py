"""B19 计划账本 provider：补上 `world/record/providers.py` 里返回 `DEPENDENCY_UNAVAILABLE` 的
`kind="plan"` 查询。

计划账本由 run_day / 规划用例按 B08 口径算好后写入 `b19_plan_ledgers`（不可变：同一计划修订只写一次），
查询按运行固定身份与清单过滤，分页游标绑定（厂区、运行、清单、kind、截止时刻），与执行账本同构。
没有规划账本时返回空页，不再冒充“依赖未接入”。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

from plantkg.core.contracts import Context, ErrorCode, Ledger, Page, PlantkgError
from plantkg.core.versions import atomic, read


class PlanLedgerStore:
    def __init__(self, writer, plant_id: str) -> None:
        self.writer, self.plant_id = writer, plant_id

    def save(self, context: Context, ledger: Ledger) -> str:
        if not context.can_write or context.run_id != ledger.run_id:
            raise PlantkgError(ErrorCode.NO_CTX, "计划账本保存需要与运行一致的可信上下文")

        def write(con):
            old = con.execute("SELECT ledger_id, record FROM b19_plan_ledgers WHERE plant_id=? AND run_id=? AND plan_id=?",
                              (self.plant_id, ledger.run_id, ledger.plan_id)).fetchone()
            record = ledger.model_dump_json()
            if old:
                if old[0] != ledger.ledger_id or old[1] != record:
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "同一计划已有不同的计划账本")
                return ledger.ledger_id
            con.execute("INSERT INTO b19_plan_ledgers(plant_id, run_id, manifest_id, plan_id, revision, ledger_id, record) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (self.plant_id, ledger.run_id, ledger.version_manifest_id, ledger.plan_id, ledger.plan_revision,
                         ledger.ledger_id, record))
            return ledger.ledger_id
        return atomic(self.writer, write)

    def load(self, run_id: str, plan_id: str | None = None) -> list[Ledger]:
        sql = "SELECT record FROM b19_plan_ledgers WHERE plant_id=? AND run_id=?"
        params: list = [self.plant_id, run_id]
        if plan_id:
            sql += " AND plan_id=?"
            params.append(plan_id)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY plan_id", params).fetchall())
        return [Ledger.model_validate_json(row[0]) for row in rows]

    def query(self, context: Context, run_id: str, cursor: str | None, page_size: int):
        if context.run_id != run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "会话必须绑定所查询的运行及清单")
        key = json.dumps([self.plant_id, run_id, context.version_manifest_id, "plan", context.as_of.isoformat(), "plan_asc_v1"],
                         separators=(",", ":"))
        binding = hashlib.sha256(key.encode()).hexdigest()
        after = ""
        if cursor is not None:
            try:
                payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                after = payload["after"]
                if payload["binding"] != binding or not isinstance(after, str):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, binascii.Error):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "分页游标无效或与查询不匹配") from None
        rows = read(self.writer, lambda con: con.execute(
            "SELECT plan_id, record FROM b19_plan_ledgers WHERE plant_id=? AND run_id=? AND manifest_id=? AND plan_id>? "
            "ORDER BY plan_id LIMIT ?",
            (self.plant_id, run_id, context.version_manifest_id, after, page_size + 1)).fetchall())
        items = tuple(Ledger.model_validate_json(row[1]) for row in rows[:page_size])
        next_cursor = None
        if len(rows) > page_size:
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                {"binding": binding, "after": rows[page_size - 1][0]}, separators=(",", ":")).encode()).decode()
        return Page[Ledger](items=items, next_cursor=next_cursor, as_of=context.as_of,
                            version_manifest_id=context.version_manifest_id)
