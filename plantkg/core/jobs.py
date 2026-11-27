"""后台任务（B04）：求解、回测、一周回放在服务进程的后台线程执行，不随 MCP 调用取消（09 D21）。

同一 idempotency_key 只建一个任务（内容不同拒绝）；get 在服务端最多等 40 秒，完成即返回。
任务状态落 SQLite；服务重启时遗留的 pending/running 任务记为 failed（可重试，须换幂等键重新提交）。
执行函数按 JobKind 登记：handler(payload, context, job_id) -> result_ref，可抛 PlantkgError；
payload["time_limit_s"] 是本任务的时限（调用方给的或按任务类型的默认预算），执行函数自己遵守。
"""

from __future__ import annotations

import json
import logging
import math
import queue
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from plantkg.core.contracts import (
    JOB_TRANSITIONS,
    JOB_WAIT_MAX_S,
    RETRYABLE_ERRORS,
    Context,
    ErrorCode,
    Job,
    JobExecutorHealth,
    JobKind,
    JobQueueSnapshot,
    JobStatus,
    PlantkgError,
    ToolError,
    ToolResult,
    can_transition,
)
from plantkg.core.versions import atomic, digest, read, utcnow

log = logging.getLogger("plantkg.jobs")
JobHandler = Callable[[Mapping[str, Any], Context, str], str]
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _error(code: ErrorCode, message: str) -> ToolError:
    return ToolError(code=code.value, message=message, retryable=code.value in RETRYABLE_ERRORS)


class SqliteJobRunner:
    """JobRunner 端口实现。budgets：JobKind → 默认 time_limit_s（payload 里的 time_limit_s 优先）。"""

    def __init__(self, writer: Any, plant_id: str, budgets: Mapping[str, float], workers: int = 2,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.writer, self.plant_id, self._budgets, self._clock = writer, plant_id, dict(budgets), clock
        self._handlers: dict[str, JobHandler] = {}
        self._fallback_handlers: set[str] = set()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[str] = set()  # 已入队、尚未被工作线程取走的任务 id；close 把它们标 failed 并通知等待者
        self._waiting_lock = threading.Lock()
        self._lifecycle = threading.RLock()  # submit 的“检查 + 建任务 + 入队”与 close 串行，关闭窗口内不留永不执行的任务
        self._changed = threading.Condition()
        self._closed = False
        self._recover()
        # daemon：长求解不阻塞解释器退出；退出时未完成的任务在下次启动记为 failed
        self._threads = [threading.Thread(target=self._work, name=f"plantkg-job-{i}", daemon=True) for i in range(workers)]
        for t in self._threads:
            t.start()

    def register(self, kind: JobKind | str, handler: JobHandler, *, fallback: bool = False) -> None:
        kind = JobKind(kind).value
        if kind in self._handlers:
            if fallback:
                return
            if kind not in self._fallback_handlers:
                raise ValueError(f"任务类型 {kind} 的执行函数重复登记")
            self._fallback_handlers.remove(kind)
        elif fallback:
            self._fallback_handlers.add(kind)
        self._handlers[kind] = handler

    def submit(self, kind: JobKind | str, payload: Mapping[str, Any], idempotency_key: str, context: Context) -> Job:
        kind = JobKind(kind).value
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "后台任务需要可信写上下文")
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能为其他厂区提交任务")
        try:
            payload = json.loads(json.dumps(dict(payload), allow_nan=False))  # 深拷贝：提交后调用方再改不影响任务
            request_hash = digest({"kind": kind, "payload": payload})
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "任务参数必须是 JSON 对象") from None
        limit = payload.get("time_limit_s", self._budgets.get(kind, 300))
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "time_limit_s 须为正数")
        payload["time_limit_s"] = limit  # 执行函数从 payload 读实际时限（幂等散列按调用方原始内容算）

        def write(con):
            row = con.execute("SELECT request_hash, record FROM jobs WHERE plant_id=? AND idempotency_key=?", (self.plant_id, idempotency_key)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一幂等键对应了不同的任务")
                return Job.model_validate_json(row[1]), False
            if kind not in self._handlers:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"任务类型 {kind} 尚无执行函数")
            job = Job(job_id="job-" + secrets.token_hex(8), kind=kind, idempotency_key=idempotency_key, status="pending",
                      submitted_at=self._clock(), budget={"time_limit_s": limit}, run_id=context.run_id, agent_run_id=context.agent_run_id)
            con.execute("INSERT INTO jobs(plant_id,job_id,idempotency_key,request_hash,status,record) VALUES (?,?,?,?,?,?)",
                        (self.plant_id, job.job_id, idempotency_key, request_hash, job.status, job.model_dump_json()))
            return job, True
        with self._lifecycle:  # 检查、建任务、入队与 close 同一把锁：不会排在停止标记之后
            if self._closed:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "任务队列已关闭")
            job, created = atomic(self.writer, write)
            if created:
                with self._waiting_lock:
                    self._queued.add(job.job_id)
                self._queue.put((job.job_id, kind, payload, context))
        return job

    def get(self, job_id: str, wait_seconds: float = 0) -> Job:
        wait_seconds = float(wait_seconds)
        if not math.isfinite(wait_seconds):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "wait 须为 0～40 的秒数")
        deadline = min(max(wait_seconds, 0.0), JOB_WAIT_MAX_S)
        job = self._load(job_id)
        if deadline and job.status not in TERMINAL:
            with self._changed:
                self._changed.wait_for(lambda: self._load(job_id).status in TERMINAL, timeout=deadline)
            job = self._load(job_id)
        return job

    def snapshot(self, context: Context, run_id: str | None = None, status: JobStatus | str | None = None,
                 limit: int = 50, offset: int = 0) -> JobQueueSnapshot:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能读取其他厂区的任务队列")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "limit 须为 1～200 的整数")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "offset 须为非负整数")
        wanted = None
        if status is not None:
            try:
                wanted = JobStatus(status).value
            except ValueError:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知任务状态 {status}") from None
        conditions, params = ["plant_id=?"], [self.plant_id]
        if run_id is not None:
            conditions.append("json_extract(record,'$.run_id')=?")
            params.append(run_id)
        if wanted is not None:
            conditions.append("status=?")
            params.append(wanted)
        where = " AND ".join(conditions)
        # submitted_at 是 UTC ISO 文本：整秒为 "...:SSZ"（20 字符），带微秒为 "...:SS.ffffffZ"（27 字符）。
        # 按原文排序会把带小数的排在整秒前（'.' < 'Z'），先补成固定 6 位小数再按文本升序。
        submitted_at = "json_extract(record,'$.submitted_at')"
        order_key = (f"CASE WHEN length({submitted_at}) = 20 THEN substr({submitted_at}, 1, 19) || '.000000' "
                     f"ELSE substr({submitted_at}, 1, 26) END")

        def query(con):
            con.execute("BEGIN")  # 同一读事务：total 与本页来自同一快照
            total = con.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0]
            rows = con.execute(
                f"SELECT record FROM jobs WHERE {where} ORDER BY {order_key}, job_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return total, rows
        total, rows = read(self.writer, query)
        return JobQueueSnapshot(jobs=tuple(Job.model_validate_json(record) for (record,) in rows),
                                total=total, limit=limit, offset=offset)

    def health(self, context: Context) -> JobExecutorHealth:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能读取其他厂区的执行器健康")
        with self._waiting_lock:
            waiting = len(self._queued)
        return JobExecutorHealth(configured_workers=len(self._threads), alive_workers=sum(t.is_alive() for t in self._threads),
                                 queue_depth=waiting, closed=self._closed)

    def close(self) -> None:
        """停止接收新任务；已接受但未开始的任务标 failed（DEPENDENCY_UNAVAILABLE）并唤醒等待者，正在执行的任务继续。

        重复调用幂等。关闭窗口内 submit 与 close 由同一把锁串行，任务不会再排到停止标记之后。#124
        """
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            with self._waiting_lock:
                queued, self._queued = sorted(self._queued), set()
            for job_id in queued:
                try:
                    self._move(job_id, "failed", expected="pending", finished_at=self._clock(),
                               error=_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "任务队列已关闭，任务未开始执行；请重新提交"))
                except PlantkgError:  # 已被工作线程取走开始执行，或已结束
                    continue
                except Exception:  # noqa: BLE001
                    log.exception("任务 %s 关闭时无法标失败", job_id)
            for _ in self._threads:
                self._queue.put(None)

    # ---- 内部
    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, kind, payload, context = item
            with self._waiting_lock:
                self._queued.discard(job_id)
            try:
                if self._load(job_id).status in TERMINAL:  # close() 已标 failed：不要再执行
                    continue
                self._move(job_id, "running", expected="pending", started_at=self._clock())
            except Exception:  # noqa: BLE001 — 写者已关闭等：任务留在 pending，下次启动记 failed
                log.exception("任务 %s 无法启动", job_id)
                continue
            try:
                result_ref = self._handlers[kind](payload, context, job_id)
                if not isinstance(result_ref, str) or not result_ref:
                    raise PlantkgError(ErrorCode.INTERNAL, "执行函数没有返回结果引用")
                update = {"result_ref": result_ref, "progress": 1.0}
                target = "succeeded"
            except PlantkgError as exc:
                target, update = "failed", {"error": _error(exc.code, exc.message)}
            except BaseException:  # noqa: BLE001 — 后台边界兜底（含 SystemExit），不回显内部细节，不让工作线程退出
                log.exception("任务 %s 内部错误", job_id)
                target, update = "failed", {"error": _error(ErrorCode.INTERNAL, "任务内部错误")}
            try:
                self._move(job_id, target, finished_at=self._clock(), **update)
            except Exception:  # noqa: BLE001
                log.exception("任务 %s 结果无法写入", job_id)

    def _move(self, job_id: str, target: str, expected: str | None = None, **fields: Any) -> Job:
        def write(con):
            job = self._read(con, job_id)
            if expected is not None and job.status != expected:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"任务 {job_id} 已是 {job.status}，不是 {expected}")
            if not can_transition(JOB_TRANSITIONS, job.status, target):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"任务 {job_id} 不能从 {job.status} 变为 {target}")
            updated = Job.model_validate(job.model_dump() | {"status": target, **fields})
            con.execute("UPDATE jobs SET status=?, record=? WHERE plant_id=? AND job_id=?", (target, updated.model_dump_json(), self.plant_id, job_id))
            return updated
        job = atomic(self.writer, write)
        with self._changed:
            self._changed.notify_all()
        return job

    def _recover(self) -> None:
        def write(con):
            rows = con.execute("SELECT record FROM jobs WHERE plant_id=? AND status IN ('pending','running')", (self.plant_id,)).fetchall()
            for (record,) in rows:
                job = Job.model_validate_json(record)
                failed = Job.model_validate(job.model_dump() | {"status": "failed", "finished_at": self._clock(),
                                                                "error": _error(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务重启，任务中断；请换幂等键重新提交")})
                con.execute("UPDATE jobs SET status='failed', record=? WHERE plant_id=? AND job_id=?", (failed.model_dump_json(), self.plant_id, job.job_id))
        atomic(self.writer, write)

    def _load(self, job_id: str) -> Job:
        return read(self.writer, lambda con: self._read(con, job_id))

    def _read(self, con: Any, job_id: str) -> Job:
        row = con.execute("SELECT record FROM jobs WHERE plant_id=? AND job_id=?", (self.plant_id, job_id)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"任务 {job_id} 不存在")
        return Job.model_validate_json(row[0])


def rest_providers(runner: SqliteJobRunner) -> dict[str, Callable[..., Any]]:
    def job(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Job:
        try:
            wait = float(query.get("wait") or 0)
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "wait 须为 0～40 的秒数") from None
        return runner.get(path["job_id"], wait_seconds=wait)
    return {"/jobs/{job_id}": job}


def submit_and_wait(runner: Any, kind: JobKind | str, payload: Mapping[str, Any], idempotency_key: str, context: Context,
                    wait_s: float = JOB_WAIT_MAX_S) -> Job:
    """长任务工具的统一入口（09 D21）：提交后最多等 wait_s 秒，返回最新任务状态；未完成的任务在后台继续。"""
    job = runner.submit(kind, payload, idempotency_key, context)
    return runner.get(job.job_id, wait_s)


def pending_result(job: Job) -> ToolResult:
    """任务状态 → 工具返回：完成带 result_ref，失败带任务错误，未完成为 pending + job_id。"""
    data = {"status": job.status, "result_ref": job.result_ref, "progress": job.progress}
    if job.status == "succeeded":
        return ToolResult(ok=True, business_status="done", job_id=job.job_id, data=data)
    if job.status in TERMINAL:
        error = job.error or _error(ErrorCode.INTERNAL, f"任务已{job.status}")
        return ToolResult(ok=False, business_status="failed", job_id=job.job_id, error=error)
    return ToolResult(ok=True, business_status="pending", job_id=job.job_id, data=data,
                      notice="求解在后台继续；用同一 job_id 调 plantkg_get_job")


def register_tools(registry: Any, runner: Any) -> None:
    """登记 MCP 工具 get_job（与 rest_providers 对称）；真实实现顶掉 Fake 占位。"""
    def get_job(context: Context, job_id: str, wait: int | None = None) -> ToolResult:
        return pending_result(runner.get(job_id, min(wait or 0, JOB_WAIT_MAX_S)))
    registry.register_tool("get_job", get_job)
