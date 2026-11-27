"""默认装配的 live 会诊冒烟：不经演示包装器，B29 payload 的 effective_at 能让提案真的执行动作。

替身 live 源照 B23 网关口径取时序（payload 有 effective_at 就用，没有按 trigger_at 计）。
后半段 monkeypatch 去掉会诊 payload 的 effective_at 模拟 #60 修前：提案被判过期、只入历史、执行 0 动作。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from plantkg.agent.arbitration.service import ArbitrationService
from plantkg.agent.consultation import SERVICE_HOOK, Consultation
from plantkg.agent.router import PROPOSALS_READY_HOOK
from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import AgentTiming, DayEvent, ExpertProposal, Interval, ProposalCost, ProposedAction
from plantkg.core.ports import ProposalSink
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.scenario.runner import pack_registry
from plantkg.world.scenario.script import ROOT, load_scenario
from scripts.nexent_live_scenario import build_runner

EXPERTS = ("energy", "hvac", "elevator", "equipment")
EXPIRED_REASON = "不等于 trigger_at + 响应预算"


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


class GatewayLikeLive:
    """替身 live 源：按 B23 网关的 timing_from_payload 口径补时序，经已装配的提案 sink 落库。"""

    source = "nexent"

    def __init__(self, registry, readiness: str = "ready", reply_status: str = "done") -> None:
        self.registry, self.readiness, self.reply_status = registry, readiness, reply_status

    def get(self, kind, binding, payload):
        raw = payload.get("effective_at")
        effective = datetime.fromisoformat(raw) if isinstance(raw, str) else raw or binding.trigger_at
        expert = payload["expert"]
        actions = ()
        if expert == "energy":  # 一位专家给出可执行动作，其余交空提案，测试只关心这条动作能不能生效
            actions = (ProposedAction(entity_id=payload["ids"]["battery"], command="discharge",
                                      params={"power_kw": 400.0}, readiness=self.readiness,
                                      depends_on=("root_cause",) if self.readiness == "needs_diagnosis" else (),
                                      interval=Interval(start=effective, end=effective + timedelta(minutes=30))),)
        draft = ExpertProposal(expert=expert, ticket_id=binding.ticket_id, actions=actions, cost=ProposalCost(cny=-126.0),
                               confidence=0.82, diagnosis="替身 live 提案")
        timing = AgentTiming(origin_event_at=binding.trigger_at, trigger_at=binding.trigger_at,
                             effective_at=effective, requested_at=binding.trigger_at)
        proposal = draft.model_copy(update={"binding": binding, "timing": timing})
        proposal_id = self.registry.resolve(ProposalSink).save(ExpertProposal.model_validate(proposal.model_dump()), binding)
        reply = {"status": self.reply_status, "proposal_id": proposal_id, "diagnosis": "替身 live 根因结论"}
        return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal_id],
                "final_answer": json.dumps(reply, ensure_ascii=False)}


def _dispatch(router, plugins, event: DayEvent):
    handled = router.handle_event(event, flush=False)
    for consultation in plugins.hooks_for(SERVICE_HOOK):  # 回答在后台挂接
        consultation.wait_replies(10.0)
    return handled


def _event(event_id: str, at: datetime, entity_id: str) -> DayEvent:
    return DayEvent(event_id=event_id, at=at, event_type="load_spike", source="script", entity_id=entity_id,
                    severity=3, summary="合成突增", payload={}, nature="synthetic")


def test_default_live_dispatch_executes_actions_and_expires_without_effective_at(tmp_path, monkeypatch):
    plugins = pack_registry()
    original = OriginalSink()
    writer = SQLiteWriter(tmp_path / "scenario.db")
    MigrationRunner(writer).apply()
    try:
        scenario = load_scenario(str(ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"))
        runner = build_runner(scenario, writer, plugins, live_sources={"expert": GatewayLikeLive(plugins)}, original_sink=original)
        runner.start()
        router = runner.dispatcher.router
        router.judge = FakeJudge()
        router.locator = lambda event: EXPERTS
        router.rule_matcher = CoverAll()

        first = _dispatch(router, plugins, _event("EV-LIVE-OK", scenario.instant("08:05"), runner.world.index.air_eid))
        assert first.ticket_id
        proposal_id = next(p.proposal_id for p in router.sink.list(first.ticket_id, 1) if p.expert == "energy")
        assert router.store.ticket_state(first.ticket_id, f"reply_status_1_{proposal_id}") == "done"
        arbitration = next(handler.__self__ for handler in router.registry.hooks_for(PROPOSALS_READY_HOOK)
                           if isinstance(getattr(handler, "__self__", None), ArbitrationService))
        arbitration.wait_for_decision(first.ticket_id, router.store.ticket(first.ticket_id).round, timeout=10)  # live 裁决在后台
        applied = [a for a in runner.trace.actions if a.get("source", {}).get("ticket_id") == first.ticket_id]
        deadline = time.time() + 5.0  # 裁决在回复线程里跑，等本轮结果出现再断言（#173 后试算翻倍，原来的即时断言会竞态）
        while not applied and time.time() < deadline:
            time.sleep(0.05)
            applied = [a for a in runner.trace.actions if a.get("source", {}).get("ticket_id") == first.ticket_id]
        assert applied, "默认装配下 live 提案没有执行动作（被场景执行器判过期？）"
        notes = [n for n in runner.trace.notes if n.get("kind") == "history"]
        assert not [n for n in notes if EXPIRED_REASON in str(n.get("reason"))]
        assert original.saved == []

        # 模拟 #60 修前：会诊 payload 不带 effective_at，B23 按 trigger_at 计时，提案比响应预算早到一分钟
        payload_without_effective_at = Consultation._payload

        def without_effective_at(router_, runner_, event_, binding_):
            return {key: value for key, value in payload_without_effective_at(router_, runner_, event_, binding_).items()
                    if key != "effective_at"}

        monkeypatch.setattr(Consultation, "_payload", staticmethod(without_effective_at))

        second = _dispatch(router, plugins, _event("EV-LIVE-EXPIRED", scenario.instant("08:06"), runner.world.index.zone_eid["admin"]))
        assert second.ticket_id and second.ticket_id != first.ticket_id
        # 回复线程可能比裁决线程先完成：5 秒内轮询等 history note 落库再断言（同批 B 的写法）
        reasons: list[str] = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reasons = [str(n.get("reason")) for n in runner.trace.notes if n.get("kind") == "history"]
            if any(EXPIRED_REASON in reason for reason in reasons):
                break
            time.sleep(0.05)
        assert any(EXPIRED_REASON in reason for reason in reasons), f"没有判过期：{reasons}"
        expired = [a for a in runner.trace.actions if a.get("source", {}).get("ticket_id") == second.ticket_id]
        assert expired == [], "没有 effective_at 时提案不该执行动作"
    finally:
        writer.close()


def test_needs_human_proposal_keeps_ticket_waiting(tmp_path):
    """需要人工确认的提案生成待确认工单，事件单不能被标成已解决（PR #50 复审）。"""
    plugins = pack_registry()
    writer = SQLiteWriter(tmp_path / "scenario.db")
    MigrationRunner(writer).apply()
    try:
        scenario = load_scenario(str(ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"))
        runner = build_runner(scenario, writer, plugins, live_sources={"expert": GatewayLikeLive(plugins, "needs_human")},
                              original_sink=OriginalSink())
        runner.start()
        router = runner.dispatcher.router
        router.judge = FakeJudge()
        router.locator = lambda event: EXPERTS
        router.rule_matcher = CoverAll()

        handled = _dispatch(router, plugins, _event("EV-LIVE-HUMAN", scenario.instant("08:05"), runner.world.index.air_eid))
        arbitration = next(handler.__self__ for handler in router.registry.hooks_for(PROPOSALS_READY_HOOK)
                           if isinstance(getattr(handler, "__self__", None), ArbitrationService))
        arbitration.wait_for_decision(handled.ticket_id, router.store.ticket(handled.ticket_id).round, timeout=10)
        orders = [wo for wo in runner.tickets.work_orders.values() if wo.ticket_id == handled.ticket_id]
        assert orders and orders[0].status == "proposed"
        assert router.store.ticket(handled.ticket_id).status == "waiting"
        assert runner.tickets.tickets[handled.ticket_id].ticket.status == "waiting"
    finally:
        writer.close()


def test_waiting_reply_status_is_saved_and_does_not_unlock_dependent_action(tmp_path):
    plugins = pack_registry()
    writer = SQLiteWriter(tmp_path / "scenario.db")
    MigrationRunner(writer).apply()
    try:
        scenario = load_scenario(str(ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"))
        live = GatewayLikeLive(plugins, readiness="needs_diagnosis", reply_status="waiting")
        runner = build_runner(scenario, writer, plugins, live_sources={"expert": live}, original_sink=OriginalSink())
        runner.start()
        router = runner.dispatcher.router
        router.judge = FakeJudge()
        router.locator = lambda event: ("energy",)
        router.rule_matcher = CoverAll()

        handled = _dispatch(router, plugins, _event("EV-LIVE-WAITING", scenario.instant("08:05"), runner.world.index.air_eid))
        proposals = router.sink.list(handled.ticket_id, 1)
        proposal_item = next(p for p in proposals if p.actions)
        assert proposal_item.actions[0].readiness == "needs_diagnosis", [(p.expert, [a.readiness for a in p.actions]) for p in proposals]
        key = f"reply_status_1_{proposal_item.proposal_id}"
        assert router.store.ticket_state(handled.ticket_id, key) == "waiting"
        arbitration = next(handler.__self__ for handler in router.registry.hooks_for(PROPOSALS_READY_HOOK)
                           if isinstance(getattr(handler, "__self__", None), ArbitrationService))
        arbitration.on_replies_ready(handled.ticket_id, 1)
        arbitration.wait_for_decision(handled.ticket_id, 1, timeout=10)
        applied = [action for action in runner.trace.actions
                   if action.get("source", {}).get("ticket_id") == handled.ticket_id]
        assert applied == [], (applied, [(d.layer, d.result.selected_proposal_ids, d.result.outcome) for d in runner.trace.decisions])
        assert any(wo.ticket_id == handled.ticket_id for wo in runner.tickets.work_orders.values())
    finally:
        writer.close()


def test_next_round_expires_previous_work_order(tmp_path):
    """第一轮待确认工单在第二轮作废（与直派器同规则），事件单不会留着 proposed 工单却标已解决（PR #50 复审）。"""
    plugins = pack_registry()
    writer = SQLiteWriter(tmp_path / "scenario.db")
    MigrationRunner(writer).apply()
    try:
        scenario = load_scenario(str(ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"))
        live = GatewayLikeLive(plugins, "needs_human")
        runner = build_runner(scenario, writer, plugins, live_sources={"expert": live}, original_sink=OriginalSink())
        runner.start()
        router = runner.dispatcher.router
        router.judge = FakeJudge()
        router.locator = lambda event: EXPERTS
        router.rule_matcher = CoverAll()

        first = _dispatch(router, plugins, _event("EV-R1", scenario.instant("08:05"), runner.world.index.air_eid))
        arbitration = next(handler.__self__ for handler in router.registry.hooks_for(PROPOSALS_READY_HOOK)
                           if isinstance(getattr(handler, "__self__", None), ArbitrationService))
        arbitration.wait_for_decision(first.ticket_id, router.store.ticket(first.ticket_id).round, timeout=10)
        assert router.store.ticket(first.ticket_id).status == "waiting"
        live.readiness = "ready"
        second = _dispatch(router, plugins, _event("EV-R2", scenario.instant("08:07"), runner.world.index.air_eid))
        arbitration.wait_for_decision(second.ticket_id, router.store.ticket(second.ticket_id).round, timeout=10)
        assert second.ticket_id == first.ticket_id and router.store.ticket(first.ticket_id).round == 2
        orders = [wo.status for wo in runner.tickets.work_orders.values() if wo.ticket_id == first.ticket_id]
        assert orders == ["expired"]
        assert router.store.ticket(first.ticket_id).status == "resolved"
    finally:
        writer.close()
