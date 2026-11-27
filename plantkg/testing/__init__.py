"""测试替身与 Mock/dev 装配（B02）。实测样例在 nexent_assets/probes/，这里只有构造的 Fake。"""

from __future__ import annotations

from typing import Any

from plantkg.core.ports import JobRunner, RunSessionStore
from plantkg.core.registry import Registry
from plantkg.testing import fakes


def install_fakes(registry: Registry, settings: Any) -> None:
    """以 fallback 方式登记 Fake：已有真实实现的槽位跳过，之后登记的真实实现会替换 Fake。

    Fake 的任务工具（run_day、get_job、GET /jobs）每次调用时才从注册表取 JobRunner，
    所以提交与查询始终连同一个队列：不论 JobRunner 是 Fake 还是 B04 的真实实现、先登记还是后登记。
    """
    registry.register(RunSessionStore, fakes.InMemoryRunSessionStore(), fallback=True)
    registry.register(JobRunner, fakes.InMemoryJobRunner(getattr(settings, "plant_id", None)), fallback=True)

    def jobs() -> JobRunner:
        return registry.resolve(JobRunner)
    for name, handler in (
        ("list_assets", fakes.list_assets),
        ("get_plan", fakes.get_plan),
        ("ledger", fakes.ledger),
        ("run_day", fakes.make_run_day(jobs)),
        ("get_job", fakes.make_get_job(jobs)),
    ):
        registry.register_tool(name, handler, fallback=True)
    for path, provider in (
        ("/assets", fakes.rest_assets),
        ("/plans/{plan_id}", fakes.rest_plan),
        ("/runs/{run_id}", fakes.rest_run),
        ("/runs/{run_id}/ledger", fakes.rest_ledger),
        ("/jobs/{job_id}", fakes.make_rest_job(jobs)),
    ):
        registry.register_rest("GET", path, provider, fallback=True)
