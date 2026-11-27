"""Persistent B15 plan history and short, atomic compare-and-swap operations.

All public reads deserialize fresh copies. B04 actions live in their own module;
proposal intents make the cross-module step recoverable without a cross-module FK.
"""
from __future__ import annotations

import json
import hashlib
from typing import Any, Mapping

from plantkg.core.contracts import Approval, ErrorCode, GateDecision, PLAN_TRANSITIONS, Plan, PlantkgError, can_transition
from plantkg.core.versions import atomic, canonical, digest, read


class PlanRepository:
    def __init__(self, writer: Any, plant_id: str):
        self.writer, self.plant_id = writer, plant_id

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return canonical(value)
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "计划材料必须是有限的 JSON 数据") from None

    @staticmethod
    def _plan_json(plan: Plan) -> str:
        return canonical(plan.model_dump(mode="json"))

    def register(self, plan: Plan, metadata: Mapping[str, Any]) -> Plan:
        """Register an immutable draft and its immutable run/material metadata."""
        if plan.status != "draft":
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "只能登记 draft 计划")
        record, meta = self._plan_json(plan), self._json(dict(metadata))
        def write(con):
            old = con.execute("SELECT revision, metadata FROM b15_plan_heads WHERE plant_id=? AND plan_id=?",
                              (self.plant_id, plan.plan_id)).fetchone()
            if old:
                row = con.execute("SELECT record FROM b15_plan_revisions WHERE plant_id=? AND plan_id=? AND revision=?",
                                  (self.plant_id, plan.plan_id, plan.revision)).fetchone()
                if not row or row[0] != record or old[1] != meta:
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划身份已登记且内容或材料不同")
                return Plan.model_validate_json(row[0])
            con.execute("INSERT INTO b15_plan_heads VALUES (?,?,?,?)", (self.plant_id, plan.plan_id, plan.revision, meta))
            con.execute("INSERT INTO b15_plan_revisions VALUES (?,?,?,?)", (self.plant_id, plan.plan_id, plan.revision, record))
            return Plan.model_validate_json(record)
        return atomic(self.writer, write)

    def _load(self, con: Any, plan_id: str, revision: int | None = None) -> Plan:
        if revision is None:
            head = con.execute("SELECT revision FROM b15_plan_heads WHERE plant_id=? AND plan_id=?", (self.plant_id, plan_id)).fetchone()
            if not head:
                raise PlantkgError(ErrorCode.NOT_FOUND, "计划不存在")
            revision = head[0]
        row = con.execute("SELECT record FROM b15_plan_revisions WHERE plant_id=? AND plan_id=? AND revision=?",
                          (self.plant_id, plan_id, revision)).fetchone()
        if not row:
            raise PlantkgError(ErrorCode.NOT_FOUND, "计划修订不存在")
        return Plan.model_validate_json(row[0])

    def load(self, plan_id: str, revision: int | None = None) -> Plan:
        return read(self.writer, lambda con: self._load(con, plan_id, revision))

    def preview_inputs(self, run_id: str, manifest_id: str) -> list[dict[str, Any]]:
        def query(con):
            rows = con.execute(
                "SELECT p.record,a.record FROM b15_plan_heads h "
                "JOIN b15_plan_revisions p ON p.plant_id=h.plant_id AND p.plan_id=h.plan_id AND p.revision=h.revision "
                "JOIN b15_approvals a ON a.plant_id=h.plant_id AND a.plan_id=h.plan_id AND a.revision=h.revision "
                "WHERE h.plant_id=? AND json_extract(p.record,'$.run_id')=? "
                "AND json_extract(p.record,'$.version_manifest_id')=? "
                "AND json_extract(p.record,'$.status') IN ('confirmed','auto_confirmed')",
                (self.plant_id, run_id, manifest_id)).fetchall()
            found = []
            for plan_record, approval_record in rows:
                plan = Plan.model_validate_json(plan_record)
                approval = Approval.model_validate_json(approval_record)
                try:
                    snapshot, _ = self.execution_snapshot_in_transaction(con, plan, approval)
                except PlantkgError:
                    continue
                found.append({"plan": plan, "config": snapshot["config"]})
            return found
        return read(self.writer, query)

    def metadata(self, plan_id: str) -> dict[str, Any]:
        def query(con):
            row = con.execute("SELECT metadata FROM b15_plan_heads WHERE plant_id=? AND plan_id=?", (self.plant_id, plan_id)).fetchone()
            if row is None:
                raise PlantkgError(ErrorCode.NOT_FOUND, "计划不存在")
            return json.loads(row[0])
        return read(self.writer, query)

    def reserve_intent(self, plan_id: str, expected_revision: int, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Persist exact proposal materials before calling B04; identical calls recover."""
        record = self._json(dict(intent))
        def write(con):
            old = con.execute("SELECT record FROM b15_proposal_intents WHERE plant_id=? AND plan_id=? AND revision=?",
                              (self.plant_id, plan_id, expected_revision)).fetchone()
            if old:
                if old[0] != record:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一计划修订的提议材料不一致")
                return json.loads(old[0])
            plan = self._load(con, plan_id)
            if plan.revision != expected_revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "计划修订已变化", actual_revision=plan.revision)
            con.execute("INSERT INTO b15_proposal_intents VALUES (?,?,?,?)", (self.plant_id, plan_id, expected_revision, record))
            return json.loads(record)
        return atomic(self.writer, write)

    def intent(self, plan_id: str, revision: int) -> dict[str, Any] | None:
        def query(con):
            row = con.execute("SELECT record FROM b15_proposal_intents WHERE plant_id=? AND plan_id=? AND revision=?",
                              (self.plant_id, plan_id, revision)).fetchone()
            return json.loads(row[0]) if row else None
        return read(self.writer, query)

    def transition(self, plan_id: str, expected_revision: int, target: str, *,
                   decision: GateDecision | None = None, policy_snapshot: Mapping[str, Any] | None = None,
                   approval: Approval | None = None, action_id: str | None = None,
                   execution_basis: Mapping[str, Any] | None = None) -> Plan:
        """CAS a legal transition, recording its facts in the same short transaction."""
        snapshot = self._json(dict(policy_snapshot or {}))
        def write(con):
            current = self._load(con, plan_id)
            if current.revision != expected_revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "计划修订已变化", expected_revision=expected_revision, actual_revision=current.revision)
            if not can_transition(PLAN_TRANSITIONS, current.status, target):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"不能从 {current.status} 转为 {target}")
            new = Plan.model_validate(current.model_dump() | {"revision": current.revision + 1, "status": target})
            if decision is not None and (decision.plan_id != plan_id or decision.revision != new.revision or decision.outcome != target):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "门控决定必须对应目标计划修订和状态")
            if decision is not None and decision.outcome == "review_required" and decision.pending_action_id != action_id:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "人工门控决定必须关联同一动作")
            if approval is not None and (approval.plan_id != plan_id or approval.revision != new.revision):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "批准必须对应目标计划修订")
            if approval is not None and ((target == "confirmed" and approval.mode != "manual") or (target == "auto_confirmed" and approval.mode != "auto") or target not in ("confirmed", "auto_confirmed")):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "批准模式与目标状态不匹配")
            if target in ("confirmed", "auto_confirmed") and approval is None:
                raise PlantkgError(ErrorCode.PRECONDITION, "已确认计划必须同时保存批准")
            if target == "auto_confirmed" and decision is None:
                raise PlantkgError(ErrorCode.PRECONDITION, "自动批准必须同时记录门控决定")
            if target == "confirmed":
                link = con.execute("SELECT action_id FROM b15_action_links WHERE plant_id=? AND plan_id=? AND revision=?",
                                   (self.plant_id, plan_id, current.revision)).fetchone()
                if not link or approval.action_id != link[0]:
                    raise PlantkgError(ErrorCode.PRECONDITION, "人工批准与待确认动作不匹配")
            if target == "review_required" and (decision is None or not action_id):
                raise PlantkgError(ErrorCode.PRECONDITION, "转人工必须同时记录决定与动作")
            con.execute("INSERT INTO b15_plan_revisions VALUES (?,?,?,?)", (self.plant_id, plan_id, new.revision, self._plan_json(new)))
            changed = con.execute("UPDATE b15_plan_heads SET revision=? WHERE plant_id=? AND plan_id=? AND revision=?",
                                  (new.revision, self.plant_id, plan_id, expected_revision)).rowcount
            if changed != 1:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "计划修订已变化")
            if decision is not None:
                con.execute("INSERT INTO b15_decisions VALUES (?,?,?,?,?)", (self.plant_id, plan_id, new.revision,
                            canonical(decision.model_dump(mode="json")), snapshot))
            if approval is not None:
                con.execute("INSERT INTO b15_approvals VALUES (?,?,?,?,?)", (self.plant_id, plan_id, new.revision,
                            approval.action_id, canonical(approval.model_dump(mode="json"))))
                if execution_basis is not None:
                    from .execution_basis import snapshot_record
                    if (execution_basis.get("plant_id"), execution_basis.get("run_id"),
                        execution_basis.get("manifest_id"), execution_basis.get("plan_id")) != (
                            self.plant_id, new.run_id, new.version_manifest_id, new.plan_id):
                        raise PlantkgError(ErrorCode.PRECONDITION, "执行依据身份不一致")
                    record, checksum = snapshot_record(execution_basis, new, approval)
                    con.execute("INSERT INTO b15_execution_snapshots VALUES (?,?,?,?,?,?,?)", (
                        self.plant_id, plan_id, new.revision, new.run_id, new.version_manifest_id, checksum, record))
            if action_id is not None:
                con.execute("INSERT INTO b15_action_links VALUES (?,?,?,?)", (self.plant_id, plan_id, new.revision, action_id))
            return new
        return atomic(self.writer, write)

    def execution_snapshot_in_transaction(self, con, plan, approval):
        """Read and integrity-check the exact snapshot on the caller connection."""
        row = con.execute("SELECT run_id, manifest_id, sha256, record FROM b15_execution_snapshots WHERE plant_id=? AND plan_id=? AND revision=?",
                          (self.plant_id, plan.plan_id, plan.revision)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.PRECONDITION, "批准缺少执行依据快照")
        try:
            body = json.loads(row[3])
            if (canonical(body) != row[3] or digest(body) != row[2] or
                    (row[0], row[1]) != (plan.run_id, plan.version_manifest_id) or
                    (body["plant_id"], body["plan_id"], body["plan_revision"], body["run_id"], body["manifest_id"]) !=
                    (self.plant_id, plan.plan_id, plan.revision, plan.run_id, plan.version_manifest_id) or
                    body["approval"] != approval.model_dump(mode="json") or
                    body["approval_sha256"] != digest(approval.model_dump(mode="json"))):
                raise ValueError("snapshot mismatch")
            receipt = con.execute("SELECT sha256,record FROM b09_solve_receipts WHERE plant_id=? AND run_id=? AND manifest_id=? AND plan_id=?",
                                  (self.plant_id, plan.run_id, plan.version_manifest_id, plan.plan_id)).fetchone()
            if (not receipt or receipt[0] != body["receipt_sha256"] or
                    hashlib.sha256(receipt[1].encode("utf-8")).hexdigest() != receipt[0]):
                raise ValueError("receipt mismatch")
        except (ValueError, TypeError, KeyError):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行依据快照损坏或身份不一致") from None
        return body, row[2]

    def decision(self, plan_id: str, revision: int | None = None) -> GateDecision | None:
        row = self._fact("b15_decisions", plan_id, revision, "record")
        return GateDecision.model_validate_json(row) if row is not None else None

    def policy_snapshot(self, plan_id: str, revision: int | None = None) -> dict[str, Any] | None:
        row = self._fact("b15_decisions", plan_id, revision, "policy_snapshot")
        return json.loads(row) if row is not None else None

    def approval(self, plan_id: str, revision: int | None = None) -> Approval | None:
        row = self._fact("b15_approvals", plan_id, revision, "record")
        return Approval.model_validate_json(row) if row is not None else None

    def action_for(self, plan_id: str, revision: int) -> str | None:
        return self._fact("b15_action_links", plan_id, revision, "action_id")

    def _fact(self, table: str, plan_id: str, revision: int | None, column: str) -> str | None:
        def query(con):
            sql = f"SELECT {column} FROM {table} WHERE plant_id=? AND plan_id=?"
            args: tuple[Any, ...] = (self.plant_id, plan_id)
            if revision is not None:
                sql += " AND revision=?"
                args += (revision,)
            row = con.execute(sql + " ORDER BY revision DESC LIMIT 1", args).fetchone()
            return row[0] if row else None
        return read(self.writer, query)

    def save_result(self, namespace: str, key: str, request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        """Store exact request and result, detecting key reuse with different content."""
        req, res = self._json(dict(request)), self._json(dict(result))
        def write(con):
            row = con.execute("SELECT request,result FROM b15_idempotency WHERE plant_id=? AND namespace=? AND key=?",
                              (self.plant_id, namespace, key)).fetchone()
            if row:
                if row[0] != req or row[1] != res:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "幂等键对应不同请求或结果")
                return json.loads(row[1])
            con.execute("INSERT INTO b15_idempotency VALUES (?,?,?,?,?)", (self.plant_id, namespace, key, req, res))
            return json.loads(res)
        return atomic(self.writer, write)

    def result(self, namespace: str, key: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
        req = self._json(dict(request))
        def query(con):
            row = con.execute("SELECT request,result FROM b15_idempotency WHERE plant_id=? AND namespace=? AND key=?",
                              (self.plant_id, namespace, key)).fetchone()
            if row and row[0] != req:
                raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "幂等键对应不同请求")
            return json.loads(row[1]) if row else None
        return read(self.writer, query)
