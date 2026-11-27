"""#124 JobRunner submit/close 竞态：关闭窗口内已接受的任务不会排在停止标记之后永久 pending。"""

from __future__ import annotations

import threading

import pytest

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.core.jobs import SqliteJobRunner
from tests.versions.conftest import PLANT, context


class BlockingQueue:
    """第一次 put 卡在入队前，模拟 submit 通过关闭检查后、入队前被 close 抢占。"""

    def __init__(self, inner) -> None:
        self.inner, self.entered, self.gate = inner, threading.Event(), threading.Event()
        self._first = True

    def put(self, item) -> None:
        if self._first:
            self._first = False
            self.entered.set()
            assert self.gate.wait(5)
        self.inner.put(item)

    def get(self):
        return self.inner.get()


def test_close_during_submit_fails_accepted_job_and_repeated_close_is_noop(writer):
    runner = SqliteJobRunner(writer, PLANT, {}, workers=0)  # 没有工作线程：任务只能由 close 收尾
    runner.register("run_week", lambda payload, ctx, job_id: "x")
    queue = BlockingQueue(runner._queue)
    runner._queue = queue
    submitted: dict = {}

    def submit() -> None:
        submitted["job"] = runner.submit("run_week", {}, "close-race", context())
    thread = threading.Thread(target=submit)
    thread.start()
    assert queue.entered.wait(5)  # submit 已通过关闭检查，卡在入队前

    closing = threading.Thread(target=runner.close)
    closing.start()
    closing.join(0.2)
    assert closing.is_alive()  # 修前：close 不等待入队，直接放停止标记后返回

    queue.gate.set()
    thread.join(5)
    closing.join(5)

    job = submitted["job"]
    finished = runner.get(job.job_id, wait_seconds=0.2)  # 立即返回，不等到超时
    assert finished.status == "failed" and finished.error.code == ErrorCode.DEPENDENCY_UNAVAILABLE.value
    health = runner.health(context())
    assert (health.closed, health.queue_depth) == (True, 0)

    runner.close()  # 重复 close 幂等：不重复放停止标记、不改已有状态
    assert runner.health(context()).queue_depth == 0
    with pytest.raises(PlantkgError) as closed:
        runner.submit("run_week", {}, "after-close", context())
    assert closed.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
