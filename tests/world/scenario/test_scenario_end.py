"""B17 场景结束钩子与 Registry.unregister_hook：跑到剧本结束只触发一次，钩子异常不影响 run_until。"""
from __future__ import annotations

from plantkg.core.registry import Registry
from plantkg.world.scenario.runner import SCENARIO_END_HOOK

from .conftest import scenario


def test_end_hook_fires_once_after_finished_and_swallows_errors(make):
    runner = make(scenario())
    seen = []

    def boom(r):
        seen.append(r.run_info.status)
        raise RuntimeError("收尾失败")

    runner.registry.register_hook(SCENARIO_END_HOOK, boom)
    trace = runner.run_until()
    assert trace is runner.trace and runner.run_info.status == "finished"
    assert seen == ["finished"]  # 先标 finished 再触发
    runner.run_until()  # 已结束后再调一次：不再触发
    assert seen == ["finished"]


def test_unregister_hook_ignores_missing_and_removes_empty_bucket():
    registry = Registry()

    def fn(r):
        return None

    registry.register_hook("demo", fn)
    registry.unregister_hook("demo", lambda r: None)  # 没登记过：忽略
    assert registry.hooks_for("demo") == (fn,)
    registry.unregister_hook("demo", fn)  # 移除后列表为空：清掉键
    assert registry.hooks_for("demo") == ()
    assert "demo" not in registry.hooks
