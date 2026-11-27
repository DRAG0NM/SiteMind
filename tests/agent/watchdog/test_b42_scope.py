"""B42 验收：#114 运行作用域、#151 受阻原因、#152 恢复结论持久化（默认装配 + HTTP 路由）。"""
from __future__ import annotations

from datetime import datetime, timezone

from starlette.testclient import TestClient

from plantkg.agent.router import ROUTER_HOOK
from plantkg.agent.router.store import RouterStore
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    AgentResultKind, AgentRunRecord, AgentTiming, AgentToolCall, RunBinding, RunMode, Ticket,
)


def _services(tmp_path):
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    # 装配一建好 watchdog 线程就会先巡检一次（不等 60 秒）；测试用同步入口，先停掉线程避免两个巡检抢写同一结论
    for close in services.registry.hooks_for("watchdog.close"):
        close()
    return services


def _client(services):
    from plantkg.api import create_app

    return TestClient(create_app(services))


def _check(services):
    """默认装配登记的同步巡检入口：不等后台 60 秒轮询。"""
    services.registry.hooks_for("watchdog.check")[-1]()


def _failed_run(ticket_id, run_id, at, *, forbidden=True):
    binding = RunBinding(run_id=run_id, knowledge_version=1, ticket_id=ticket_id, round=1, state_version=0, trigger_at=at)
    timing = AgentTiming(origin_event_at=at, trigger_at=at, effective_at=at, requested_at=at, returned_at=at)
    calls = ()
    if forbidden:
        calls = (AgentToolCall(call_id="call-1", agent_name="plantkg_main", tool="x", started_at=at, duration_ms=1,
                               business_status="failed", summary="PLANTKG_E_FORBIDDEN: 越权"),)
    return AgentRunRecord(agent_run_id=f"ar-{run_id}", kind=AgentResultKind.investigate, agent_name="plantkg_main",
                          run_mode=RunMode.live, source="deterministic", binding=binding, timing=timing,
                          status="failed", tool_calls=calls)


def test_default_assembly_watchdog_blocks_ticket_in_shared_scenario_scope(tmp_path):
    """#114：协同组运行的事件单写在 plant#run 作用域，巡检要按该作用域读并写回，工作台能看到。"""
    services = _services(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        ticket_id = "ticket://TK-0001"
        scope = f"{services.settings.plant_id}#run-scope-1"
        store = RouterStore(services.storage.writer, scope)
        store.save_ticket(Ticket(ticket_id=ticket_id, status="open", opened_at=at, round=1), run_id="run-scope-1")
        services.nexent.store.save(_failed_run(ticket_id, "run-scope-1", at))

        _check(services)

        assert store.ticket(ticket_id).status == "blocked"  # 旧实现按 settings.plant_id 写回，匹配不到 plant#run
        client = _client(services)
        items = client.get("/api/v1/watchdog/blocked").json()["items"]
        assert any(item["ticket_id"] == ticket_id and item["ticket_status"] == "blocked" for item in items)
        answer = client.get("/api/v1/watchdog/explain", params={"question": "TK-0001 为什么受阻"}).json()["answer"]
        assert "权限边界拒绝" in answer
        _check(services)  # 重复巡检不能把首轮证据降级成“事件单已受阻”的泛化原因
        again = client.get("/api/v1/watchdog/explain", params={"question": "TK-0001 为什么受阻"}).json()["answer"]
        assert "权限边界拒绝" in again
    finally:
        services.close()


def test_blocked_ticket_first_inspection_reuses_run_and_health_evidence(tmp_path, monkeypatch):
    """#151：B28/B47 先置 blocked 时，首轮巡检用最近失败运行与依赖健康写原因，没有证据才写未知。"""
    monkeypatch.setenv("PLANTKG_MODEL_HEALTH_URL", "x://model")
    services = _services(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        ticket_id = "ticket://TK-0002"
        router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        router.store.save_ticket(Ticket(ticket_id=ticket_id, status="blocked", opened_at=at, round=1), run_id="run-blocked")
        services.nexent.store.save(_failed_run(ticket_id, "run-blocked", at, forbidden=False))

        _check(services)

        client = _client(services)
        answer = client.get("/api/v1/watchdog/explain", params={"question": "TK-0002 为什么受阻"}).json()["answer"]
        assert "ar-run-blocked" in answer and "model 检查失败" in answer
        assert "原受阻原因未知" not in answer
    finally:
        services.close()


def test_recovery_conclusion_survives_consecutive_inspections(tmp_path, monkeypatch):
    """#152：重查恢复后事件单回到 open，后续巡检沿用恢复结论，不写回“原因未知”。"""
    import plantkg.agent.watchdog as watchdog_module

    at = datetime.now(timezone.utc)
    services = _services(tmp_path)
    try:
        ticket_id = "ticket://TK-0003"
        router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        router.store.save_ticket(Ticket(ticket_id=ticket_id, status="open", opened_at=at, round=1), run_id="run-recovery")
        services.nexent.store.save(_failed_run(ticket_id, "run-recovery", at, forbidden=False))
        responses = iter([("failed", "synthetic 断开"), ("ok", "已恢复"), ("ok", "已恢复"), ("ok", "已恢复")])
        monkeypatch.setenv("PLANTKG_MODEL_HEALTH_URL", "x://model")
        monkeypatch.setattr(watchdog_module, "http_probe", lambda url: next(responses))

        _check(services)  # 首轮：依赖失败并重查恢复，写入恢复结论
        client = _client(services)
        assert "已恢复" in client.get("/api/v1/watchdog/explain", params={"question": "TK-0003 为什么受阻"}).json()["answer"]
        for _ in range(2):  # 恢复后连续两轮仍显示已恢复
            _check(services)
            answer = client.get("/api/v1/watchdog/explain", params={"question": "TK-0003 为什么受阻"}).json()["answer"]
            assert "已恢复" in answer and "原因未知" not in answer
    finally:
        services.close()

