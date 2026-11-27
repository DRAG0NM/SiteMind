"""#167 B24/B27：judge.backend=fallback 不提供派域分数时退化为确定性权重，值守循环不崩，日志写明原因。"""

from __future__ import annotations

import logging

from plantkg.agent.router import ROUTER_HOOK
from plantkg.backends.judge import FallbackJudge
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.ports import Judge
from plantkg.world.sensors import ObservationBus
from tests.agent.router.helpers import make_event

PLANT = "demo-electronics"


def entity(kind: str, code: str) -> str:
    return f"entity://{PLANT}/{kind}/{code}"


def test_fallback_judge_degrades_triage_instead_of_crashing(tmp_path, caplog):
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                        "storage.graph": str(tmp_path / "graph"),
                                        "storage.series": str(tmp_path / "series"),
                                        "judge.backend": "fallback"})
    with caplog.at_level(logging.WARNING, logger="plantkg"):
        services = build_services(settings)
        try:
            registry = services.registry
            assert isinstance(registry.resolve(Judge), FallbackJudge)
            router = registry.hooks_for(ROUTER_HOOK)[-1]
            handled = router.handle_event(make_event("EV-FB", 0, "unknown_combo", entity("air_compressor", "AC-2")))

            assert handled.channel == "slow"
            triage = router.store.triage(handled.ticket_id, 1)
            assert triage["source"] == "slow_pending" and triage["coverage_gap"] is True
        finally:
            ObservationBus.clear()
            services.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("不提供派域分数" in message for message in messages)  # 装配自检：切 fallback 即提示派域退化
    assert any("判别后端 fallback" in message for message in messages)  # 运行日志：记 backend 与异常
