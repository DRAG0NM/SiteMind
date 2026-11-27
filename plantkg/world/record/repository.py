"""B16 short-transaction persistence; not an Executor or input authority."""
import json
import sqlite3
from datetime import datetime, timedelta

from plantkg.core.contracts import ErrorCode, ExecutionResult, PlantkgError
from plantkg.core.ledger import validate_balance
from plantkg.core.versions import atomic, canonical


class ExecutionRepository:
    def __init__(self, writer, clock, gate):
        self.writer, self.clock, self.gate = writer, clock, gate

    def find_retry(self, context, key, request_digest):
        """Return an exact committed retry before checking current eligibility."""
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "执行需要可信写上下文")
        with self.writer.read_only() as con:
            run = self.clock._run(con, context)
            row = con.execute("SELECT request_digest,result FROM b16_executions WHERE plant_id=? AND run_id=? AND idempotency_key=?",
                              (run.plant_id, run.run_id, key)).fetchone()
            if not row:
                return None
            if row[0] != request_digest:
                raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "幂等键对应不同请求")
            return ExecutionResult.model_validate_json(row[1])

    def last_projection(self, context, seq):
        """Internal committed projection for the current cursor only."""
        with self.writer.read_only() as con:
            run = self.clock._run(con, context)
            current_seq, _ = self.clock._latest(con, run)
            if current_seq != seq:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放游标已变化")
            row = con.execute("SELECT seq_after,projection FROM b16_executions WHERE plant_id=? AND run_id=? ORDER BY seq_after DESC LIMIT 1",
                              (run.plant_id, run.run_id)).fetchone()
            if row is None:
                return None
            if row[0] != seq:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "执行投影与回放游标不连续")
            return json.loads(row[1])

    @staticmethod
    def visible_projection(con, run, seq, cutoff):
        row = con.execute("SELECT projection FROM b16_executions WHERE plant_id=? AND run_id=? AND manifest_id=? AND seq_after<=? AND visible_after<=? ORDER BY seq_after DESC LIMIT 1",
                          (run.plant_id, run.run_id, run.version_manifest_id, seq, cutoff.isoformat())).fetchone()
        if not row:
            return {}
        projection = json.loads(row[0])
        result = {}
        if projection.get("storage"):
            result["storage"] = {key: projection["storage"][key] for key in ("energy_kwh", "capacity_kwh")
                                  if key in projection["storage"]}
        if projection.get("production"):
            operations = projection["production"]
            result["production"] = {"operations": operations,
                                    "pending_jobs": sorted(key for key, value in operations.items() if value.get("status") == "pending")}
        return result

    def commit(self, context, plan, expected_seq, request_digest, snapshot_digest, result, projection, visible_after):
        """Commit a prepared result together with B07 CAS and B15 recheck.

        All inputs are internal, trusted-service objects; preparation and source
        validation must occur before this method and outside the write lock.
        """
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "执行提交需要可信写上下文")
        if not isinstance(result, ExecutionResult) or not result.ledger.balanced or validate_balance(result.ledger):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行账本未通过校验")
        if (result.run_id, result.plan_id, result.plan_revision, result.ledger.version_manifest_id) != (
                context.run_id, plan.plan_id, plan.revision, context.version_manifest_id):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行结果身份不一致")
        if type(expected_seq) is not int or result.event_seq != expected_seq + 1:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "执行序号不一致")
        entries = result.ledger.entries
        if len(entries) != 1 or entries[0].interval.end - entries[0].interval.start != timedelta(minutes=15):
            raise PlantkgError(ErrorCode.PRECONDITION, "每次只提交一个完整时段")
        slot = entries[0].interval
        if (not isinstance(visible_after, datetime) or visible_after.utcoffset() != timedelta(0)
                or visible_after < slot.end):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行结果可见时间不能早于槽结束")
        payload = canonical(result.model_dump(mode="json"))
        state = canonical(projection)
        def write(con):
            run = self.clock._run(con, context)
            old = con.execute("SELECT request_digest,result FROM b16_executions WHERE plant_id=? AND run_id=? AND idempotency_key=?",
                              (run.plant_id, run.run_id, result.idempotency_key)).fetchone()
            if old:
                if old[0] != request_digest:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "幂等键对应不同请求")
                return ExecutionResult.model_validate_json(old[1]), False
            _, current = self.clock._latest(con, run)
            if current != slot.start:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "候选时段不是当前时钟")
            stored = self.gate.require_executable_in_transaction(con, plan.plan_id, plan.revision,
                                                                  run.run_id, run.version_manifest_id, current)
            if stored != plan:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划内容与已批准版本不一致")
            _, checksum = self.gate.require_execution_snapshot_in_transaction(con, plan.plan_id, plan.revision,
                                                                                run.run_id, run.version_manifest_id, current)
            if checksum != snapshot_digest:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "执行依据已变化")
            try:
                con.execute("INSERT INTO b16_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    run.plant_id, run.run_id, run.version_manifest_id, result.idempotency_key,
                    request_digest, expected_seq, result.event_seq, slot.start.isoformat(), slot.end.isoformat(),
                    visible_after.isoformat(), checksum, payload, state))
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_CONSTRAINT_UNIQUE,
                                                               sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY):
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "该时段已执行") from exc
                raise
            self.clock.advance_in_transaction(con, context, expected_seq, slot.end)
            return result, True
        result, advanced = atomic(self.writer, write)
        if advanced:
            self.clock.notify_advanced(context, slot.end)
        return result

    def read(self, context, seq):
        if context.plant_id != self.clock.runs.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        with self.writer.read_only() as con:
            run = self.clock._run(con, context)
            _, current = self.clock._latest(con, run)
            if context.as_of > current:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "知识截止晚于回放游标")
            row = con.execute("SELECT visible_after,result,projection FROM b16_executions WHERE plant_id=? AND run_id=? AND manifest_id=? AND seq_after=?",
                              (run.plant_id, run.run_id, run.version_manifest_id, seq)).fetchone()
            if not row:
                raise PlantkgError(ErrorCode.NOT_FOUND, "执行结果不存在")
            if datetime.fromisoformat(row[0]) > context.as_of:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "执行结果尚不可见")
            return ExecutionResult.model_validate_json(row[1]), json.loads(row[2])

    def read_offline(self, context, seq):
        """Internal ex-post reader, never registered as an online API or tool."""
        if not context.can_write or context.channel != "offline":
            raise PlantkgError(ErrorCode.FORBIDDEN, "事后读取需要可信离线上下文")
        with self.writer.read_only() as con:
            run = self.clock._run(con, context)
            _, _, inputs = self.clock.runs._load(con, run.run_id)
            if inputs.get("execution_input_mode") != "offline_scenario":
                raise PlantkgError(ErrorCode.PRECONDITION, "运行未启用事后场景")
            try:
                cutoff = datetime.fromisoformat(inputs["execution_truth_as_of"])
                if cutoff.utcoffset() != timedelta(0):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise PlantkgError(ErrorCode.PRECONDITION, "缺少有效的事后场景截止时间") from None
            row = con.execute("SELECT visible_after,result,projection FROM b16_executions WHERE plant_id=? AND run_id=? AND manifest_id=? AND seq_after=?",
                              (run.plant_id, run.run_id, run.version_manifest_id, seq)).fetchone()
            if not row:
                raise PlantkgError(ErrorCode.NOT_FOUND, "执行结果不存在")
            if datetime.fromisoformat(row[0]) > cutoff:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "事后来源截止不足")
            return ExecutionResult.model_validate_json(row[1]), json.loads(row[2])
