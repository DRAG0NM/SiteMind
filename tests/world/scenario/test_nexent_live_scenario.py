"""场景 live 入口冒烟：假 live 结果源经入口装配的 sink，把提案写进场景路由的提案库。"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from plantkg.agent.consultation import SERVICE_HOOK
from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import AgentResultKind, AgentTiming, DayEvent, ExpertProposal, PlantkgError, ProposalCost
from plantkg.core.ports import AgentResultSource, ProposalSink
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.integrations.nexent.scenario import register_live_sources
from plantkg.world.scenario.runner import pack_registry
from plantkg.world.scenario.__main__ import main as scenario_main
from plantkg.world.scenario.script import ROOT, load_scenario
from scripts.nexent_live_scenario import build_runner, passed, start_server, wait_replies

EXPERTS = ("energy", "hvac", "elevator", "equipment")


class CoverAll:
    def covers(self, event) -> bool:
        return True


class OriginalSink:
    """原提案库替身：场景运行已绑定时，live 提案不应写到它。"""

    def __init__(self) -> None:
        self.saved: list = []

    def save(self, proposal, binding) -> str:
        self.saved.append(proposal)
        return f"orig://{len(self.saved)}"

    def attach_reply(self, proposal_id, reply):
        raise AssertionError("场景提案不应写到原提案库")

    def get(self, proposal_id):
        raise AssertionError("场景提案不应写到原提案库")

    def list(self, ticket_id, round_=None):
        return []


class FakeLive:
    """假 live 结果源：像 B23 一样调专家 propose 业务函数，并经入口装配的 sink 落库。"""

    source = "nexent"

    def __init__(self, registry) -> None:
        self.registry = registry

    def get(self, kind, binding, payload):
        expert = payload["expert"]
        hooks = self.registry.hooks_for(f"expert_propose:{expert}")
        if hooks:
            draft = hooks[-1](SimpleNamespace(run_id=binding.run_id), binding)
        elif expert == "hvac":  # 暖通的业务函数直接注册成工具，没有 expert_propose 钩子
            from plantkg.experts.hvac.expert import HvacExpert
            event, situation = self.registry.hooks_for("hvac_situation")[-1](None, binding)
            draft = HvacExpert().propose(event, situation, ticket_id=binding.ticket_id)
        else:  # 其余领域在会诊里都有钩子；没有时给一份空动作提案，测试只关心 sink 分发
            draft = ExpertProposal(expert=expert, ticket_id=binding.ticket_id, cost=ProposalCost(cny=0.0), confidence=0.8,
                                   diagnosis="测试提案")
        timing = AgentTiming(origin_event_at=binding.trigger_at, trigger_at=binding.trigger_at,
                             effective_at=datetime.fromisoformat(payload["effective_at"]), requested_at=binding.trigger_at)
        draft = draft.model_copy(update={"binding": binding, "timing": timing, "ticket_id": binding.ticket_id})
        proposal_id = self.registry.resolve(ProposalSink).save(ExpertProposal.model_validate(draft.model_dump()), binding)
        return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal_id], "final_answer": ""}


def test_live_expert_proposals_land_in_scenario_router_sink(tmp_path):
    plugins = pack_registry()
    original = OriginalSink()
    writer = SQLiteWriter(tmp_path / "scenario.db")
    MigrationRunner(writer).apply()
    try:
        scenario = load_scenario(str(ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"))
        plugins.register_mode(AgentResultSource, "live", FakeLive(plugins), key="expert")
        from plantkg.world.scenario.runner import SCENARIO_HOOK

        plugins.register_hook(SCENARIO_HOOK, register_live_sources(plugins))
        runner = build_runner(scenario, writer, plugins, original_sink=original)
        runner.start()
        router = runner.dispatcher.router
        router.judge = FakeJudge()
        router.locator = lambda event: EXPERTS
        router.rule_matcher = CoverAll()
        event = DayEvent(event_id="EV-LIVE-T", at=scenario.instant("08:05"), event_type="load_spike", source="script",
                         entity_id=runner.world.index.air_eid, severity=3, summary="合成突增", payload={}, nature="synthetic")
        handled = router.handle_event(event, flush=False)
        assert handled.channel == "fast" and handled.ticket_id
        for consultation in plugins.hooks_for(SERVICE_HOOK):  # 回答在后台挂接
            consultation.wait_replies(10.0)
        proposals = router.sink.list(handled.ticket_id, 1)
        assert proposals, "live 提案没有进场景路由的提案库"
        assert all(p.binding is not None and p.binding.run_id == router.run_id for p in proposals)
        assert original.saved == []
        runs = [r for r in router.runs if r.kind == AgentResultKind.expert and r.binding is not None
                and r.binding.ticket_id == handled.ticket_id]
        assert runs and all(r.source == "nexent" for r in runs)
        assert router.store.ticket(handled.ticket_id).status != "running"
    finally:
        writer.close()


def test_scenario_live_cli_uses_nexent_live_entry(monkeypatch, tmp_path):
    from scripts import nexent_live_scenario

    called = []
    monkeypatch.setattr(nexent_live_scenario, "main", lambda argv: called.append(argv) or 0)
    db, out = str(tmp_path / "scenario.db"), str(tmp_path / "trace.json")
    assert scenario_main(["run", "configs/scenarios/factory_demo_day.yaml", "--mode", "live", "--group", "coordinated",
                          "--until", "09:00", "--db", db, "--out", out]) == 0
    assert called == [["configs/scenarios/factory_demo_day.yaml", "--until", "09:00", "--db", db, "--out", out]]


GOOD_EXPERT = {"expert": "energy", "source": "nexent", "coverage_gap": False, "status": "finished", "proposals": 1, "actions": 2}


def _report(ticket_status: str = "resolved", expert_status: str = "finished", **extra) -> dict:
    expert = GOOD_EXPERT | {"status": expert_status}
    return {"tickets": [{"status": ticket_status, "experts": [expert], "proposals": 1}]} | extra


def test_passed_rejects_blocked_tickets_failed_experts_and_pending_replies():
    assert passed(_report())
    assert not passed(_report(ticket_status="blocked"))
    assert not passed(_report(expert_status="failed"))
    assert not passed(_report(replies_pending=True))


def test_start_server_reports_early_exit_and_timeout():
    with pytest.raises(PlantkgError, match="启动失败"):
        start_server(SimpleNamespace(started=False, run=lambda: None), timeout=5)
    release = threading.Event()
    with pytest.raises(PlantkgError, match="没有启动"):
        start_server(SimpleNamespace(started=False, run=release.wait), timeout=0.2)
    release.set()


def test_wait_replies_asks_every_consultation_and_reports_timeout():
    asked = []

    def consultation(result):
        return SimpleNamespace(wait_replies=lambda timeout: asked.append(timeout) or result)

    assert not wait_replies([consultation(False), consultation(True)], 5.0)
    assert asked == [5.0, 5.0]
