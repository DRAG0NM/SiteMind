"""B27 值守层的夹具：临时 SQLite + 注册表 + 装配好的 Router。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from plantkg.agent.router import Router, RouterStore, SqliteProposalSink, install
from plantkg.agent.router.severity import SeverityEstimator
from plantkg.core.registry import Registry
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from tests.agent.router.helpers import PLANT, SEVERITY, CountingJudge


@dataclass
class Env:
    writer: Any
    registry: Registry
    router: Router
    sink: SqliteProposalSink
    store: RouterStore
    judge: CountingJudge


@pytest.fixture(autouse=True)
def _no_nexent_key(monkeypatch):
    """开发机跑过 live-check 后环境里常有 NEXENT_API_KEY；默认装配测试固定清掉，避免装配被切成 live。"""
    monkeypatch.delenv("NEXENT_API_KEY", raising=False)


@pytest.fixture
def env(tmp_path):
    writer = SQLiteWriter(tmp_path / "router.db")
    MigrationRunner(writer).apply()
    registry = Registry()
    registry.live_fallback = True
    judge = CountingJudge()
    router = install(registry, writer, PLANT, run_id="run-1", knowledge_version=3, judge=judge, mode="evaluation",
                     state_version=7, severity=SeverityEstimator(SEVERITY))
    try:
        yield Env(writer=writer, registry=registry, router=router, sink=router.sink, store=router.store, judge=judge)
    finally:
        writer.close()
