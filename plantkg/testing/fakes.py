"""Fake 端口与 mock 数据：骨架阶段供 Mock profile 与测试使用，全部在内存里。

数据来自 contracts/examples/valid 的契约样例；这些是构造样例，不是平台实测（实测样例在 nexent_assets/probes/）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from plantkg.core import contracts as c
from plantkg.core.contracts import (
    Actor, Context, ErrorCode, Job, JobExecutorHealth, JobKind, JobQueueSnapshot, JobStatus, PlantkgError, RunSession, ToolResult,
)
from plantkg.core.ports import JobRunner
from plantkg.core.jobs import pending_result, submit_and_wait

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "valid"


@cache
def fixture(name: str) -> c.Record:
    """按样例文件名读出契约记录，如 fixture("plan")。"""
    sample = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return getattr(c, sample["model"]).model_validate(sample["data"])


class InMemoryRunSessionStore:
    """RunSessionStore 的内存实现（正式实现归 B04）。"""

    def __init__(self, clock=lambda: datetime.now(timezone.utc)) -> None:
        self._sessions: dict[str, RunSession] = {}
        self._run_bindings: dict[str, tuple[str, str]] = {}
        self._clock = clock

    def register(
        self, actor: Actor, plant_id: str, as_of: datetime, visible_docs: Sequence[str], ttl_s: int, agent_run_id: str, trace_id: str,
        industry_pack: str | None = None, origin: str = "gateway", *, run_id: str | None = None,
        version_manifest_id: str | None = None,
    ) -> RunSession:
        if bool(run_id) != bool(version_manifest_id):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "运行和清单须同时绑定")
        now = self._clock()
        session = RunSession(
            ctx=secrets.token_hex(16), actor=actor, plant_id=plant_id, industry_pack=industry_pack or "manufacturing", as_of=as_of,
            visible_document_paths=tuple(visible_docs), agent_run_id=agent_run_id, trace_id=trace_id,
            created_at=now, expires_at=now + timedelta(seconds=ttl_s), origin=origin,
        )
        self._sessions[session.ctx] = session
        if run_id and version_manifest_id:
            self._run_bindings[session.ctx] = (run_id, version_manifest_id)
        return session

    def resolve(self, ctx: str) -> RunSession | None:
        return self._sessions.get(ctx)

    def resolve_binding(self, ctx: str) -> tuple[str, str] | None:
        return self._run_bindings.get(ctx)

    def expire(self, ctx: str) -> None:
        self._sessions.pop(ctx, None)
        self._run_bindings.pop(ctx, None)


class InMemoryJobRunner:
    """JobRunner 的内存实现：提交即返回 running，下一次 get 即 succeeded（正式实现归 B04）。

    plant_id 应在装配时用 settings.plant_id 固定；未固定时只由首次 submit 绑定，只读请求不决定厂区。
    绑定后拒绝其他厂区，幂等键也按厂区分开。
    """

    def __init__(self, plant_id: str | None = None) -> None:
        self._plant_id = plant_id
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._results: dict[str, str] = {}

    def _check_plant(self, context: Context) -> None:
        if self._plant_id is not None and context.plant_id != self._plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能访问其他厂区的任务队列")

    def _write_plant(self, context: Context) -> str:
        """写操作用：未绑定厂区时由这次提交确定，绑定后只允许同一厂区。"""
        self._check_plant(context)
        if self._plant_id is None:
            self._plant_id = context.plant_id
        return self._plant_id

    def submit(self, kind: JobKind, payload: Mapping[str, Any], idempotency_key: str, context: Context) -> Job:
        """签名与 JobRunner 端口一致；Mock 结果引用取 payload["mock_result_ref"]。"""
        key = (self._write_plant(context), idempotency_key)
        result_ref = str(payload.get("mock_result_ref", f"job-result://{idempotency_key}"))
        if key in self._by_key:
            return self._jobs[self._by_key[key]]
        now = datetime.now(timezone.utc)
        job = Job(job_id="job-" + secrets.token_hex(6), kind=kind, idempotency_key=idempotency_key, status="running",
                  submitted_at=now, started_at=now, budget={"time_limit_s": 20}, progress=0.0, agent_run_id=context.agent_run_id,
                  run_id=context.run_id)
        self._jobs[job.job_id], self._by_key[key], self._results[job.job_id] = job, job.job_id, result_ref
        return job

    def get(self, job_id: str, wait_seconds: float = 0) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"任务 {job_id} 不存在")
        if job.status == "running":
            job = job.model_copy(update={"status": "succeeded", "progress": 1.0, "finished_at": datetime.now(timezone.utc),
                                         "result_ref": self._results[job_id]})
            self._jobs[job_id] = job
        return job

    def snapshot(self, context: Context, run_id: str | None = None, status: JobStatus | str | None = None,
                 limit: int = 50, offset: int = 0) -> JobQueueSnapshot:
        """按厂区只读：已绑定厂区时其他厂区抛 CROSS_PLANT；只读不绑定厂区。任务按提交时间排序后分页。"""
        self._check_plant(context)
        jobs = [job for job in self._jobs.values()
                if (run_id is None or job.run_id == run_id) and (status is None or job.status == status)]
        jobs.sort(key=lambda job: (job.submitted_at, job.job_id))
        return JobQueueSnapshot(jobs=tuple(jobs[offset:offset + limit]), total=len(jobs), limit=limit, offset=offset)

    def health(self, context: Context) -> JobExecutorHealth:
        """Fake 没有工作线程：任务在 get 时同步完成，running（待 get）数即队列深度。只读不绑定厂区。"""
        self._check_plant(context)
        return JobExecutorHealth(configured_workers=0, alive_workers=0, closed=False,
                                 queue_depth=sum(job.status == "running" for job in self._jobs.values()))


# ---------------------------------------------------------------- mock 工具（handler(context, **params) -> ToolResult）


def _visible_assets(context: Context) -> list[c.AssetVersion]:
    assets = [fixture("asset_version_policy")]
    return [a for a in assets if a.available_at <= context.as_of]


def list_assets(context: Context, asset_type: str | None = None, cursor: str | None = None, page_size: int | None = None) -> ToolResult:
    items = [a for a in _visible_assets(context) if asset_type in (None, a.asset_type)]
    data = {"items": [{"asset_version_id": a.asset_version_id, "asset_type": a.asset_type, "title": a.title,
                       "available_at": a.available_at.isoformat(), "nature": a.nature} for a in items]}
    return ToolResult(ok=True, business_status="done", data=data, page={"page_size": page_size or 50})


def get_plan(context: Context, plan_id: str, revision: int | None = None) -> ToolResult:
    plan = fixture("plan")
    if plan_id != plan.plan_id or revision not in (None, plan.revision):
        raise PlantkgError(ErrorCode.NOT_FOUND, f"计划 {plan_id} 不存在")
    data = {"plan_id": plan.plan_id, "revision": plan.revision, "status": plan.status, "solver_status": plan.solver_status,
            "actions": [{"action_id": a.action_id, "object": a.object, "quantity": a.quantity, "value": a.value, "unit": a.unit,
                         "start": a.interval.start.isoformat(), "refs": [r.id for r in a.refs]} for a in plan.actions],
            "objective_terms": plan.objective_terms}
    return ToolResult(ok=True, business_status="done", data=data, run_id=plan.run_id, version_manifest_id=plan.version_manifest_id)


def ledger(context: Context, run_id: str, kind: str, cursor: str | None = None, page_size: int | None = None) -> ToolResult:
    led = fixture("ledger_execution")
    if run_id != led.run_id or kind != led.kind:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {run_id} 没有 {kind} 账本")
    data = {"ledger_id": led.ledger_id, "balanced": led.balanced, "total_cost_cny": led.total_cost_cny,
            "peak_import_kw": led.peak_import_kw, "entries": len(led.entries)}
    return ToolResult(ok=True, business_status="done", data=data, run_id=run_id, version_manifest_id=led.version_manifest_id,
                      page={"page_size": page_size or 50})


def make_run_day(jobs: Callable[[], JobRunner]):
    def run_day(context: Context, plan_date: str, policy: str | None = None) -> ToolResult:
        policy = policy or "B2"
        key = hashlib.sha256(f"{context.plant_id}|{plan_date}|{policy}|{context.as_of.isoformat()}".encode()).hexdigest()[:24]
        payload = {"plan_date": plan_date, "policy": policy, "mock_result_ref": "plan://plan-20260701@1"}
        return pending_result(submit_and_wait(jobs(), JobKind.run_day, payload, key, context))
    return run_day


def make_get_job(jobs: Callable[[], JobRunner]):
    def get_job(context: Context, job_id: str, wait: int | None = None) -> ToolResult:
        return pending_result(jobs().get(job_id, wait_seconds=min(wait or 0, c.JOB_WAIT_MAX_S)))
    return get_job


# ---------------------------------------------------------------- mock REST（provider(context, path, query) -> 记录）


def rest_plan(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> c.Plan:
    plan = fixture("plan")
    if path["plan_id"] != plan.plan_id:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"计划 {path['plan_id']} 不存在")
    return plan


def rest_assets(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> c.Page[c.AssetVersion]:
    return c.Page[c.AssetVersion](items=tuple(_visible_assets(context)), as_of=context.as_of)


def rest_run(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> c.RunInfo:
    run = fixture("run_info")
    if path["run_id"] != run.run_id:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {path['run_id']} 不存在")
    return run


def rest_ledger(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> c.Page[c.Ledger]:
    led = fixture("ledger_execution")
    items = (led,) if path["run_id"] == led.run_id and query.get("kind", led.kind) == led.kind else ()
    return c.Page[c.Ledger](items=items, as_of=context.as_of, version_manifest_id=led.version_manifest_id)


def make_rest_job(jobs: Callable[[], JobRunner]):
    def rest_job(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Job:
        return jobs().get(path["job_id"], wait_seconds=min(float(query.get("wait", 0)), c.JOB_WAIT_MAX_S))
    return rest_job
