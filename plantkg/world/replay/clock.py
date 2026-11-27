"""Persistent replay revisions. The revision is independent of B04 run events."""
from datetime import datetime, timedelta, timezone
import hashlib

from plantkg.core.contracts import ErrorCode, PlantkgError, StateSnapshot
from plantkg.core.versions import atomic, read

STEP = timedelta(minutes=15)
REPLAY_ADVANCED_HOOK = "replay.advanced"  # fn(context, instant)：已提交的回放时钟推进


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要带时区的时间")
    return value.astimezone(timezone.utc)


class ReplayClock:
    def __init__(self, writer, runs, registry=None):
        self.writer, self.runs, self.registry = writer, runs, registry
        self.projection_reader = None

    def notify_advanced(self, context, instant):
        if self.registry is None:
            return
        for hook in self.registry.hooks_for(REPLAY_ADVANCED_HOOK):
            try:
                hook(context, instant)
            except Exception:  # noqa: BLE001 — 预演或观察者失败不能撤销已提交的时钟
                continue

    def _run(self, con, context):
        if not context.run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "需要绑定运行和固定清单")
        if context.plant_id != self.runs.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        run = self.runs._load(con, context.run_id)[0]
        if run.version_manifest_id != context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "运行清单不匹配")
        return run

    def _latest(self, con, run):
        row = con.execute("SELECT seq, instant, manifest_id FROM replay_clock_history WHERE plant_id=? AND run_id=? ORDER BY seq DESC LIMIT 1", (run.plant_id, run.run_id)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.PRECONDITION, "回放时钟尚未初始化")
        if row[2] != run.version_manifest_id:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放时钟清单不匹配")
        return row[0], datetime.fromisoformat(row[1])

    def initialize(self, context):
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "初始化需要可信写上下文")
        def write(con):
            run = self._run(con, context)
            if run.forked_from:
                raise PlantkgError(ErrorCode.PRECONDITION, "分叉运行必须原子初始化")
            if run.status not in ("created", "running"):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "运行已结束")
            con.execute("INSERT OR IGNORE INTO replay_clock_history VALUES (?,?,?,?,?)", (run.plant_id, run.run_id, 0, run.clock.start.isoformat(), run.version_manifest_id))
            return self._latest(con, run)
        return atomic(self.writer, write)

    def current(self, context):
        def fetch(con):
            run = self._run(con, context)
            seq, instant = self._latest(con, run)
            if context.as_of > instant:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "知识截止晚于回放游标")
            return run, seq, instant
        return read(self.writer, fetch)

    def current_for_run(self, run_id):
        """Resolve the persisted cursor for a trusted internal run identifier."""
        def fetch(con):
            run = self.runs._load(con, run_id)[0]
            return self._latest(con, run)[1]
        return read(self.writer, fetch)

    def at(self, context, instant):
        instant = _utc(instant)
        def fetch(con):
            run = self._run(con, context)
            _, current = self._latest(con, run)
            if context.as_of > current or instant > current:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "请求超出已提交游标")
            if instant < run.clock.start or instant > run.clock.end or (instant - run.clock.start) % STEP:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "查看时点不在运行时段边界")
            row = con.execute("SELECT seq FROM replay_clock_history WHERE plant_id=? AND run_id=? AND instant<=? ORDER BY seq DESC LIMIT 1", (run.plant_id, run.run_id, instant.isoformat())).fetchone()
            if row is None:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "时点早于运行起点")
            cutoff = min(context.as_of, instant)
            key = f"{run.run_id}|{run.version_manifest_id}|{row[0]}|{instant.isoformat()}|{cutoff.isoformat()}"
            states = self.projection_reader(con, run, row[0], cutoff) if self.projection_reader else {}
            return StateSnapshot(run_id=run.run_id, snapshot_id="snapshot-" + hashlib.sha256(key.encode()).hexdigest(), event_seq=row[0], as_of=cutoff, version_manifest_id=run.version_manifest_id, pack_states=states)
        return read(self.writer, fetch)

    def advance(self, context, expected_seq, next_instant):
        next_instant = _utc(next_instant)
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "推进需要可信写上下文")
        def write(con):
            run = self._run(con, context)
            seq, current = self._latest(con, run)
            if context.as_of > current:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "知识截止晚于回放游标")
            if run.status not in ("created", "running") or expected_seq != seq:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "运行状态或时钟版本冲突")
            if next_instant <= current or next_instant > run.clock.end or (next_instant - run.clock.start) % STEP:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "推进时点不合法")
            con.execute("INSERT INTO replay_clock_history VALUES (?,?,?,?,?)", (run.plant_id, run.run_id, seq + 1, next_instant.isoformat(), run.version_manifest_id))
        atomic(self.writer, write)
        self.notify_advanced(context, next_instant)
        return self.at(context, next_instant)

    def advance_in_transaction(self, con, context, expected_seq, next_instant):
        """Advance exactly one slot on the caller's existing write transaction."""
        if not con.in_transaction:
            raise PlantkgError(ErrorCode.PRECONDITION, "需要调用方事务")
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "推进需要可信写上下文")
        next_instant = _utc(next_instant)
        run = self._run(con, context)
        seq, current = self._latest(con, run)
        if context.as_of > current:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "知识截止晚于回放游标")
        if run.status not in ("created", "running") or type(expected_seq) is not int or expected_seq != seq:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "运行状态或时钟版本冲突")
        if next_instant != current + STEP or next_instant > run.clock.end or (next_instant - run.clock.start) % STEP:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "只能推进一个完整槽")
        con.execute("INSERT INTO replay_clock_history VALUES (?,?,?,?,?)", (run.plant_id, run.run_id, seq + 1, next_instant.isoformat(), run.version_manifest_id))
        return seq + 1, next_instant

    def fork_run_at(self, context, instant):
        instant = _utc(instant)
        run, _, current = self.current(context)
        if instant > current or instant < run.clock.start or (instant - run.clock.start) % STEP:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "分叉时点不合法")
        frozen = self.runs.run_inputs(run.run_id)
        def hook(con, child):
            parent = self._run(con, context)
            _, now = self._latest(con, parent)
            if instant > now:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "父运行尚未到达分叉点")
            con.execute("INSERT INTO replay_clock_history VALUES (?,?,?,?,?)", (child.plant_id, child.run_id, 0, instant.isoformat(), child.version_manifest_id))
        child_context = context.model_copy(update={"as_of": min(context.as_of, instant)})
        return self.runs.create_run(child_context, run.kind, run.clock, forked_from=run.run_id, seed=frozen["seed"], inputs=frozen["inputs"], transaction_hook=hook)
