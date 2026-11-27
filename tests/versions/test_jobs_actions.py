import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import Actor, ErrorCode, Job, PlantkgError, ToolResult
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.core.jobs import SqliteJobRunner
from plantkg.core.security import SessionCtxResolver
from plantkg.core.sessions import MemoryRunSessionStore
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, VIEWER, context


# ---------------------------------------------------------------- 后台任务


@pytest.mark.parametrize("real_first", [False, True])
def test_real_job_handler_replaces_fallback_in_either_order(writer, real_first):
    runner = SqliteJobRunner(writer, PLANT, {})
    fallback = lambda payload, ctx, job_id: "fallback"
    real = lambda payload, ctx, job_id: "real"
    try:
        if real_first:
            runner.register("counterfactual_score", real)
            runner.register("counterfactual_score", fallback, fallback=True)
        else:
            runner.register("counterfactual_score", fallback, fallback=True)
            runner.register("counterfactual_score", real)
        job = runner.submit("counterfactual_score", {}, "score-key", context())
        assert runner.get(job.job_id, wait_seconds=5).result_ref == "real"
        with pytest.raises(ValueError):
            runner.register("counterfactual_score", real)
    finally:
        runner.close()


def test_job_survives_caller_timeout_and_is_deduplicated(writer):
    release, calls = threading.Event(), []

    def solve(payload, ctx, job_id):
        calls.append(job_id)
        release.wait(5)
        return f"plan://p-{payload['day']}@1"
    runner = SqliteJobRunner(writer, PLANT, {"run_day": 20})
    runner.register("run_day", solve)
    try:
        job = runner.submit("run_day", {"day": "2026-07-01"}, "key-1", context())
        assert job.status == "pending" and job.budget.time_limit_s == 20
        assert runner.get(job.job_id, wait_seconds=0.2).status in ("pending", "running")  # 调用方等到超时仍未完成
        assert runner.submit("run_day", {"day": "2026-07-01"}, "key-1", context()).job_id == job.job_id
        with pytest.raises(PlantkgError) as exc:
            runner.submit("run_day", {"day": "2026-07-02"}, "key-1", context())
        assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
        release.set()
        done = runner.get(job.job_id, wait_seconds=5)
        assert done.status == "succeeded" and done.result_ref == "plan://p-2026-07-01@1" and done.finished_at
        assert calls == [job.job_id]
    finally:
        release.set()
        runner.close()


def test_get_wait_is_capped_and_failures_are_recorded(writer):
    runner = SqliteJobRunner(writer, PLANT, {})
    runner.register("backtest", lambda p, c, j: (_ for _ in ()).throw(PlantkgError(ErrorCode.INFEASIBLE, "无可行解")))
    runner.register("ingest", lambda p, c, j: time.sleep(60) or "x")
    try:
        failed = runner.get(runner.submit("backtest", {}, "k-fail", context()).job_id, wait_seconds=5)
        assert failed.status == "failed" and failed.error.code == ErrorCode.INFEASIBLE.value and not failed.error.retryable
        slow = runner.submit("ingest", {}, "k-slow", context())
        started = time.monotonic()
        assert runner.get(slow.job_id, wait_seconds=0.3).status in ("pending", "running")
        assert time.monotonic() - started < 2
        with pytest.raises(PlantkgError) as missing:
            runner.submit("knowledge_eval", {}, "k-none", context())
        assert missing.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
        with pytest.raises(PlantkgError):
            runner.submit("backtest", {}, "k-anon", context(actor=None, channel="none", agent_run_id=None))
    finally:
        runner.close()


def test_restart_marks_interrupted_jobs_failed(writer):
    runner = SqliteJobRunner(writer, PLANT, {}, workers=0)  # 不启动工作线程，任务停在 pending
    runner.register("run_week", lambda p, c, j: "x")
    job = runner.submit("run_week", {}, "k-restart", context())
    restarted = SqliteJobRunner(writer, PLANT, {}, workers=0)
    interrupted = restarted.get(job.job_id)
    assert interrupted.status == "failed" and interrupted.error.retryable


def test_snapshot_filters_pages_and_health_reports_workers(writer):
    """#69 验收冒烟：快照按 run_id/状态过滤并分页，健康报线程数与队列深度，越厂区只读被拒。"""
    ticks = iter(AS_OF + timedelta(seconds=i) for i in range(1, 10))
    runner = SqliteJobRunner(writer, PLANT, {}, workers=0, clock=lambda: next(ticks))  # 任务停在 pending
    runner.register("run_week", lambda p, c, j: "x")
    try:
        first = runner.submit("run_week", {"n": 1}, "snap-1", context(run_id="run-a"))
        second = runner.submit("run_week", {"n": 2}, "snap-2", context(run_id="run-a"))
        other = runner.submit("run_week", {"n": 3}, "snap-3", context(run_id="run-b"))
        assert runner.snapshot(context(), run_id="run-a").jobs == (first, second)  # 按提交时间排序
        page = runner.snapshot(context(), run_id="run-a", status="pending", limit=1, offset=1)
        assert [job.job_id for job in page.jobs] == [second.job_id] and page.total == 2 and page.offset == 1
        assert [job.job_id for job in runner.snapshot(context(), run_id="run-b").jobs] == [other.job_id]
        assert runner.snapshot(context(), status="running").jobs == ()
        assert runner.snapshot(context()).total == 3
        health = runner.health(context())
        assert (health.configured_workers, health.alive_workers, health.queue_depth, health.closed) == (0, 0, 3, False)
        for read in (lambda: runner.snapshot(context(plant_id="other-plant")), lambda: runner.health(context(plant_id="other-plant"))):
            with pytest.raises(PlantkgError) as exc:
                read()
            assert exc.value.code == ErrorCode.CROSS_PLANT
    finally:
        runner.close()


# ---------------------------------------------------------------- #69 二修


def test_health_ignores_stop_marker_while_job_runs(writer):
    """#69 二修：close() 放入的 None 停止标记不算队列深度，值守端不误判积压。"""
    started, release = threading.Event(), threading.Event()

    def slow(payload, ctx, job_id):
        started.set()
        release.wait(5)
        return "x"

    runner = SqliteJobRunner(writer, PLANT, {}, workers=1)
    runner.register("run_week", slow)
    try:
        job = runner.submit("run_week", {}, "depth-run", context())
        assert started.wait(5)  # 一个线程正在执行，队列里没有等待任务
        runner.close()
        health = runner.health(context())
        assert (health.closed, health.queue_depth, health.alive_workers) == (True, 0, 1)  # 修前 qsize=1
    finally:
        release.set()
        runner.get(job.job_id, wait_seconds=5)


def test_health_counts_queued_jobs_after_close(writer):
    """#124：close 把已接受未开始的任务标 failed 并通知等待者，队列深度归零，不再排到停止标记之后。"""
    started, release = threading.Event(), threading.Event()

    def slow(payload, ctx, job_id):
        started.set()
        release.wait(5)
        return "x"

    runner = SqliteJobRunner(writer, PLANT, {}, workers=1)
    runner.register("run_week", slow)
    try:
        first = runner.submit("run_week", {}, "depth-a", context())
        assert started.wait(5)
        second = runner.submit("run_week", {}, "depth-b", context())  # 已在队列，未开始
        runner.close()
        health = runner.health(context())
        assert (health.closed, health.queue_depth) == (True, 0)  # 修前 qsize=2：任务 + 停止标记
        assert runner.get(second.job_id, wait_seconds=0.2).status == "failed"
    finally:
        release.set()
        runner.get(first.job_id, wait_seconds=5)


def test_snapshot_filters_and_pages_large_history_in_sql(writer, monkeypatch):
    """#69 二修：200+ 条历史任务下由 SQL 过滤/计数/排序/分页，只反序列化当前页。"""
    from plantkg.core import jobs as jobs_module

    ticks = iter(AS_OF + timedelta(seconds=i) for i in range(300))
    runner = SqliteJobRunner(writer, PLANT, {}, workers=0, clock=lambda: next(ticks))  # 任务停在 pending
    runner.register("run_week", lambda p, c, j: "x")
    calls = []

    class CountingJob(Job):
        @classmethod
        def model_validate_json(cls, *args, **kwargs):
            calls.append(1)
            return super().model_validate_json(*args, **kwargs)

    try:
        bulk = [runner.submit("run_week", {"n": n}, f"bulk-{n}", context(run_id="run-a" if n < 200 else "run-b"))
                for n in range(205)]
        monkeypatch.setattr(jobs_module, "Job", CountingJob)
        page = runner.snapshot(context(), limit=10, offset=195)
        assert page.total == 205
        assert [job.job_id for job in page.jobs] == [job.job_id for job in bulk[195:205]]
        assert len(calls) == 10  # 修前：205 条全部反序列化后再分页
        calls.clear()
        page = runner.snapshot(context(), run_id="run-b", status="pending", limit=2, offset=3)
        assert page.total == 5
        assert [job.job_id for job in page.jobs] == [job.job_id for job in bulk[203:205]]
        assert len(calls) == 2
        assert runner.snapshot(context(), run_id="run-a").total == 200
        assert runner.snapshot(context(), status="succeeded").total == 0
    finally:
        runner.close()


def test_snapshot_orders_whole_and_fractional_seconds_by_time(writer):
    """#69 三修：整秒与带小数秒的 UTC 文本不能按原文排序；快照按实际时间升序并分页。"""
    stamps = iter([
        datetime(2026, 6, 30, 10, 0, 0, 500000, tzinfo=timezone.utc),  # 原文 ".500000Z"，会排到整秒 "...Z" 前
        datetime(2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 30, 10, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 30, 10, 0, 1, 250000, tzinfo=timezone.utc),
    ])
    runner = SqliteJobRunner(writer, PLANT, {}, workers=0, clock=lambda: next(stamps))
    runner.register("run_week", lambda p, c, j: "x")
    try:
        jobs = [runner.submit("run_week", {"n": n}, f"order-{n}", context()) for n in range(4)]
        expected = [jobs[1].job_id, jobs[0].job_id, jobs[2].job_id, jobs[3].job_id]
        assert [job.job_id for job in runner.snapshot(context()).jobs] == expected  # 修前 jobs[0] 排第一
        page = runner.snapshot(context(), limit=2, offset=1)
        assert [job.job_id for job in page.jobs] == expected[1:3] and page.total == 4
    finally:
        runner.close()


# ---------------------------------------------------------------- 待确认动作


class Handler:
    kind = "publish"

    def __init__(self):
        self.applied = []

    def apply(self, action, actor):
        self.applied.append((action.action_id, action.status, actor.actor_id))
        return ToolResult(ok=True, business_status="done", data={"published": action.target_ref})


@pytest.fixture
def clock():
    now = [AS_OF]
    return now


@pytest.fixture
def actions(writer, clock):
    handler = Handler()
    store = SqlitePendingActionStore(writer, PLANT, {"publish": handler}, SqliteEvidenceSink(writer, PLANT), ttl_s=600, clock=lambda: clock[0])
    store.handler = handler
    return store


def test_unconfirmed_action_has_no_effect_and_reviewer_confirms_once(actions):
    action = actions.propose("publish", {"target_ref": "cand-1"}, context(idempotency_key="prop-1"))
    assert action.status == "proposed" and action.revision == 0 and action.proposed_by.agent_run_id == "ar-1"
    assert actions.propose("publish", {"target_ref": "cand-1"}, context(idempotency_key="prop-1")) == action
    assert actions.handler.applied == []
    with pytest.raises(PlantkgError) as forbidden:
        actions.confirm(action.action_id, VIEWER, "我想确认", 0, "d-1")
    assert forbidden.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(PlantkgError) as stale:
        actions.confirm(action.action_id, REVIEWER, "确认", 5, "d-2")
    assert stale.value.code == ErrorCode.REVISION_CONFLICT
    confirmed = actions.confirm(action.action_id, REVIEWER, "回测通过", 0, "d-3")
    assert confirmed.status == "confirmed" and confirmed.revision == 1 and confirmed.decided_by == REVIEWER.actor_id
    assert actions.confirm(action.action_id, REVIEWER, "回测通过", 0, "d-3") == confirmed  # 同键同内容
    with pytest.raises(PlantkgError) as mismatch:
        actions.confirm(action.action_id, REVIEWER, "换个理由", 0, "d-3")
    assert mismatch.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(PlantkgError) as again:
        actions.confirm(action.action_id, REVIEWER, "再确认", 1, "d-4")
    assert again.value.code == ErrorCode.STATE_CONFLICT
    assert actions.handler.applied == [(action.action_id, "confirmed", REVIEWER.actor_id)]
    assert actions.result(action.action_id).data == {"published": "cand-1"}


def test_expired_rejected_and_failed_handler_leave_state(actions, clock, writer):
    expiring = actions.propose("publish", {"target_ref": "cand-2"}, context(idempotency_key="prop-2"))
    clock[0] = AS_OF + timedelta(seconds=601)
    with pytest.raises(PlantkgError) as expired:
        actions.confirm(expiring.action_id, REVIEWER, "迟了", 0, "d-5")
    assert expired.value.code == ErrorCode.ACTION_EXPIRED and actions.get(expiring.action_id).status == "expired"
    rejected_one = actions.propose("publish", {"target_ref": "cand-3"}, context(idempotency_key="prop-3"))
    rejected = actions.reject(rejected_one.action_id, REVIEWER, "回测不达标", 0, "d-6")
    assert rejected.status == "rejected" and actions.handler.applied == []
    failing = SqlitePendingActionStore(writer, PLANT, {"rollback": type("H", (), {"apply": lambda self, a, u: ToolResult(
        ok=False, business_status="failed", error={"code": "PLANTKG_E_STATE_CONFLICT", "message": "活动版本已变化"})})()}, clock=lambda: clock[0])
    pending = failing.propose("rollback", {"target_ref": "vm-1"}, context(idempotency_key="prop-4"))
    with pytest.raises(PlantkgError) as conflict:
        failing.confirm(pending.action_id, REVIEWER, "回滚", 0, "d-7")
    assert conflict.value.code == ErrorCode.STATE_CONFLICT and failing.get(pending.action_id).status == "proposed"
    no_handler = failing.propose("plan_confirmation", {"target_ref": "plan-1@1"}, context(idempotency_key="prop-5"))
    with pytest.raises(PlantkgError) as missing:
        failing.confirm(no_handler.action_id, REVIEWER, "确认", 0, "d-8")
    assert missing.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    items, _ = actions.list(status="proposed")  # 两个存储共用一张表：只剩执行失败与缺执行函数的两条
    assert {a.idempotency_key for a in items} == {"prop-4", "prop-5"}
    assert {a.action_id for a in actions.list(kind="publish")[0]} >= {expiring.action_id, rejected.action_id}


def test_missing_run_clock_does_not_block_other_actions(writer):
    available = [True]
    def action_clock(kind, run_id):
        if kind == "plan_confirmation" and not available[0]:
            raise PlantkgError(ErrorCode.NOT_FOUND, "run missing")
        return AS_OF

    store = SqlitePendingActionStore(writer, PLANT, {"publish": Handler()}, action_clock=action_clock)
    broken = store.propose("plan_confirmation", {"target_ref": "plan@1"}, context(idempotency_key="broken"))
    normal = store.propose("publish", {"target_ref": "candidate"}, context(idempotency_key="normal"))
    available[0] = False
    assert {a.action_id for a in store.list()[0]} == {broken.action_id, normal.action_id}
    assert store.confirm(normal.action_id, REVIEWER, "ok", 0, "normal-confirm").status == "confirmed"
    with pytest.raises(PlantkgError) as exc:
        store.confirm(broken.action_id, REVIEWER, "ok", 0, "broken-confirm")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_concurrent_confirms_apply_once(actions):
    action = actions.propose("publish", {"target_ref": "cand-9"}, context(idempotency_key="prop-9"))
    barrier, outcomes = threading.Barrier(6), []

    def confirm(i):
        barrier.wait()
        try:
            outcomes.append(actions.confirm(action.action_id, REVIEWER, "并发确认", 0, f"race-{i}").status)
        except PlantkgError as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=confirm, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("confirmed") == 1 and len(actions.handler.applied) == 1


# ---------------------------------------------------------------- 运行会话


def test_session_register_resolve_expire():
    now = [AS_OF]
    store = MemoryRunSessionStore("manufacturing", clock=lambda: now[0])
    resolver = SessionCtxResolver(store, clock=lambda: now[0])
    actor = Actor(actor_id="u-1", roles=("operator",))
    s = store.register(actor, PLANT, AS_OF, ("docs/a.pdf",), 60, "ar-9", "tr-9")
    assert len(s.ctx) == 32 and store.resolve(s.ctx) == s
    assert resolver.classify(s.ctx)[0].agent_run_id == "ar-9"
    now[0] = AS_OF + timedelta(seconds=61)
    assert resolver.classify(s.ctx)[1] == ErrorCode.CTX_EXPIRED
    store.expire(s.ctx)
    assert resolver.classify(s.ctx)[1] == ErrorCode.UNKNOWN_CTX
    a = store.register(actor, PLANT, AS_OF, (), 60, "ar-10", "tr-10")
    b = store.register(actor, PLANT, AS_OF, (), 60, "ar-10", "tr-11")
    assert a.ctx != b.ctx and store.expire_agent_run("ar-10") == 2 and store.resolve(a.ctx) is None
    with pytest.raises(PlantkgError):
        store.register(actor, PLANT, AS_OF, (), 0, "ar-11", "tr-12")


# ---------------------------------------------------------------- 对抗审查回归


def test_action_cannot_expire_while_handler_runs(writer, clock):
    store = None
    observed = []

    class Slow:
        def apply(self, action, actor):
            clock[0] = AS_OF + timedelta(seconds=601)  # 执行中到了过期时间
            t = threading.Thread(target=lambda: observed.append(store.get(action.action_id).status))
            t.start()
            t.join(0.3)
            assert t.is_alive()  # 并发读取等确认完成，不会把动作标成过期
            self.thread = t
            return ToolResult(ok=True, business_status="done")
    slow = Slow()
    store = SqlitePendingActionStore(writer, PLANT, {"publish": slow}, ttl_s=600, clock=lambda: clock[0])
    action = store.propose("publish", {"target_ref": "cand-x"}, context(idempotency_key="prop-x"))
    assert store.confirm(action.action_id, REVIEWER, "确认", 0, "d-x").status == "confirmed"
    slow.thread.join(5)
    assert observed == ["confirmed"] and store.get(action.action_id).status == "confirmed"


def test_b04_writes_refuse_outer_transaction_and_jobs_survive_bad_input(writer, versions):
    with pytest.raises(RuntimeError):
        with writer.transaction():
            versions.prepare({"rules": {"ref": "t:r", "content_hash": "sha256:" + "0" * 64}}, "外层事务")
    assert versions.prepare({"rules": {"ref": "t:r", "content_hash": "sha256:" + "0" * 64}}, "之后可用").status == "prepared"
    runner = SqliteJobRunner(writer, PLANT, {})
    runner.register("ingest", lambda p, c, j: (_ for _ in ()).throw(SystemExit(3)))
    runner.register("backtest", lambda p, c, j: "report://1")
    try:
        crashed = runner.get(runner.submit("ingest", {}, "k-exit", context()).job_id, wait_seconds=5)
        assert crashed.status == "failed"
        assert runner.get(runner.submit("backtest", {}, "k-after", context()).job_id, wait_seconds=5).status == "succeeded"  # 工作线程还在
        for bad in ({"time_limit_s": "x"}, {"time_limit_s": -1}, {"v": float("nan")}):
            with pytest.raises(PlantkgError) as exc:
                runner.submit("backtest", bad, "k-bad-" + str(len(str(bad))), context())
            assert exc.value.code == ErrorCode.INVALID_REQUEST
        with pytest.raises(PlantkgError):
            runner.get("job-x", wait_seconds=float("nan"))
    finally:
        runner.close()
    with pytest.raises(PlantkgError):
        runner.submit("backtest", {}, "k-closed", context())


# ---------------------------------------------------------------- 二次自审回归


def test_second_review_edges(writer, versions):
    from plantkg.core.actions import OperatorAuth
    times = iter([AS_OF, AS_OF + timedelta(seconds=599), AS_OF + timedelta(seconds=601)])
    store = SqlitePendingActionStore(writer, PLANT, {"publish": Handler()}, ttl_s=600, clock=lambda: next(times))
    action = store.propose("publish", {"target_ref": "cand-e"}, context(idempotency_key="prop-e"))
    with pytest.raises(PlantkgError) as expired:  # 扫描时未到期、决定时刚好到期
        store.reject(action.action_id, REVIEWER, "迟了", 0, "d-e")
    assert expired.value.code == ErrorCode.ACTION_EXPIRED
    with pytest.raises(PlantkgError) as bad_payload:
        store.propose("publish", {"target_ref": "x", "when": AS_OF}, context(idempotency_key="prop-bad"))
    assert bad_payload.value.code == ErrorCode.INVALID_REQUEST

    class BrokenSink:
        def append(self, event):
            raise OSError("证据库故障")
    lenient = SqlitePendingActionStore(writer, PLANT, {"publish": Handler()}, BrokenSink())
    proposed = lenient.propose("publish", {"target_ref": "cand-f"}, context(idempotency_key="prop-f"))
    assert lenient.confirm(proposed.action_id, REVIEWER, "确认", 0, "d-f").status == "confirmed"

    with pytest.raises(PlantkgError) as bad_component:
        versions.prepare({"not_a_component": {"ref": "t:x", "content_hash": "sha256:" + "0" * 64}}, "错组件名")
    assert bad_component.value.code == ErrorCode.INVALID_REQUEST
    with pytest.raises(ValueError):
        OperatorAuth("u-reviewer")

    seen = []
    runner = SqliteJobRunner(writer, PLANT, {"run_day": 20})
    runner.register("run_day", lambda p, c, j: seen.append(p["time_limit_s"]) or "plan://x@1")
    try:
        runner.get(runner.submit("run_day", {}, "k-budget", context()).job_id, wait_seconds=5)
        assert seen == [20]
    finally:
        runner.close()


# ---------------------------------------------------------------- 长任务 40 秒截止（09 D21）


def test_submit_and_wait_fast_job_returns_result(writer):
    from plantkg.core.jobs import pending_result, submit_and_wait
    runner = SqliteJobRunner(writer, PLANT, {})
    runner.register("run_day", lambda p, c, j: "plan://fast@1")
    try:
        job = submit_and_wait(runner, "run_day", {"day": "d1"}, "k-fast", context())
        result = pending_result(job)
        assert job.status == "succeeded" and result.business_status == "done" and result.job_id == job.job_id
        assert result.data["result_ref"] == "plan://fast@1"
    finally:
        runner.close()


def test_submit_and_wait_slow_job_returns_pending_then_done(writer):
    from plantkg.core.jobs import pending_result, submit_and_wait
    runner = SqliteJobRunner(writer, PLANT, {})
    runner.register("run_day", lambda p, c, j: time.sleep(1) or "plan://slow@1")
    try:
        job = submit_and_wait(runner, "run_day", {"day": "d2"}, "k-slow", context(), wait_s=0.2)
        result = pending_result(job)
        assert result.business_status == "pending" and result.job_id == job.job_id and "get_job" in result.notice
        done = pending_result(runner.get(job.job_id, wait_seconds=5))
        assert done.business_status == "done" and done.data["result_ref"] == "plan://slow@1"
    finally:
        runner.close()


def test_pending_result_failed_job_carries_error(writer):
    from plantkg.core.jobs import pending_result, submit_and_wait
    runner = SqliteJobRunner(writer, PLANT, {})
    runner.register("run_day", lambda p, c, j: (_ for _ in ()).throw(PlantkgError(ErrorCode.INFEASIBLE, "无可行解")))
    try:
        result = pending_result(submit_and_wait(runner, "run_day", {}, "k-bad", context()))
        assert not result.ok and result.business_status == "failed" and result.error.code == ErrorCode.INFEASIBLE.value
    finally:
        runner.close()


def test_assembly_with_storage_has_real_get_job(tmp_path):
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    runtime = tmp_path / "runtime"
    settings = load_settings(profile="dev", overrides={
        "packs": "[pv, storage, tariff, production]", "storage.enabled": True, "storage.sqlite": str(runtime / "catalog.db"),
        "storage.graph": str(runtime / "graph"), "storage.series": str(runtime / "series")})
    services = build_services(settings)  # dev 会补 Fake，但真实 get_job 必须顶掉占位
    try:
        assert "get_job" in services.registry.tools and "get_job" not in services.registry.fake_bindings()
    finally:
        services.close()
