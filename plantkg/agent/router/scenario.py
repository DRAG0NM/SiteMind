"""协同组派发器：场景运行的事件经值守层处理，事件单只由 RouterStore 写。

ScenarioRunner 推进世界、执行动作；本模块把事件交给 Router，并在 B29 会诊、B28 裁决、B47 分级执行之前补齐它们的位置：
- 会诊：Router.standin 同步询问专家（B29 登记 router.experts 后自动不走这里）；
- 裁决：没有 router.proposals_ready 处理器时，提案按直派器规则逐份生效，登记后交给处理器；
- 分级执行：没有 router.urgent 处理器时，紧急事件照常问专家，安全动作进操作工单。
TicketBook 在这里只是视图：放场景侧的首发时刻、待办与工单，事件单的状态、轮次、订阅与通道都以 RouterStore 为准。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from plantkg.agent.router.pack import _policy
from plantkg.agent.router.proposals import PREFIX, SqliteProposalSink
from plantkg.agent.router.router import (
    EVENT_DOMAINS, INVESTIGATE_KEY, PROPOSALS_READY_HOOK, ROUTER_HOOK, SCENARIO_BIND_HOOK, URGENT_HOOK, Router, RouterConfig,
)
from plantkg.agent.router.severity import load_severity_config
from plantkg.agent.router.store import RouterStore
from plantkg.agent.router.triage import TRIAGE_KEY, DeterministicInvestigation, DeterministicTriage
from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import DayEvent, ExpertProposal, Interval, RunBinding, RunInfo, Ticket
from plantkg.core.ports import AgentResultSource, Judge, ProposalSink
from plantkg.world.scenario.dispatch import DirectDispatcher, TicketState
from plantkg.world.scenario.runner import TrialRunner


class RouteJudge(FakeJudge):
    """没有接判别后端时的派域替身（synthetic）：概率按剧本路由表给，表里排前面的领域概率高，保证同一剧本可复现。"""

    def __init__(self, routes: dict[str, list[str]]) -> None:
        super().__init__()
        self.routes = routes

    def scores(self, state, instructions, definitions, order, index):
        routed = self.routes.get(str(state.get("content", "")).split("：")[0], ())
        if not routed:
            return super().scores(state, instructions, definitions, order, index)
        return {o: 2 ** (len(routed) - routed.index(o) + 2) if o in routed else 1 for o in order}


class CoordinatedDispatcher(DirectDispatcher):
    def __init__(self, runner: Any, routes: dict[str, list[str]] | None = None) -> None:
        super().__init__(runner, routes)
        self.events: dict[str, DayEvent] = {}
        self.reason = "event"
        self._router: Router | None = None
        self._mirrored_runs = 0
        # 事件单同步水位：只在 RouterStore 有写入时重读；每 15 分钟兜底一次（#perf）
        self._synced_ticket_revision = -1

    @property
    def router(self) -> Router:
        return self._router or self._build_router()

    def _build_router(self) -> Router:
        r = self.runner
        # 值守表按 plant_id 隔离：每条运行链用自己的作用域，同一个库重跑剧本不互相去重
        scope = f"{r.db.plant_id}#{r.run_info.run_id}"
        sink = SqliteProposalSink(r.db.writer, scope, run_id=r.run_info.run_id)
        self._router = Router(
            RouterStore(r.db.writer, scope), r.registry, plant_id=r.db.plant_id, run_id=r.run_info.run_id, sink=sink,
            knowledge_version=r.knowledge_version, state_version=lambda: r.state.state_version,
            judge=r.registry.ports.get(Judge) or RouteJudge(self.routes), config=RouterConfig(min_severity=1), mode=r.mode, wall=r.coordinator.wall,
            policy=_policy(r.policy, load_severity_config()), snapshot=lambda event=None: r.snapshot(), trial=self._trial)
        self._router.standin = self._round
        for key, source in ((TRIAGE_KEY, DeterministicTriage()), (INVESTIGATE_KEY, DeterministicInvestigation(self._router.sources))):
            if (AgentResultSource, "deterministic", key) not in r.registry.mode_ports:
                r.registry.register_mode(AgentResultSource, "deterministic", source, key=key)
        r.registry.ports[ProposalSink] = sink  # 当前运行的提案库；分叉时由 on_fork 换成子运行库
        r.registry.register_hook(ROUTER_HOOK, self._router)
        for bind in r.registry.hooks_for(SCENARIO_BIND_HOOK):
            bind(self._router, r)
        return self._router

    def _trial(self, snapshot: Any, candidate: Any, horizon: Interval) -> Any:
        """委托 TrialRunner：观测仿真器不进试算、horizon 截到剧本结束都由它统一处理（#186 收敛成一处）。"""
        return TrialRunner(self.runner.world).trial(snapshot, candidate, horizon)

    # ---- 事件入口
    def admit(self, event: DayEvent) -> None:
        r, router = self.runner, self.router
        self.events[event.event_id] = event
        handled = router.handle_event(event, flush=False)
        if handled.ticket_id:
            r.tickets.by_event[event.event_id] = handled.ticket_id
        r.note("admission", event.at, {"event_id": event.event_id, "result": handled.result, "ticket_id": handled.ticket_id,
                                       "channel": handled.channel, "experts": list(handled.experts)})
        if handled.result == "urgent" and not r.registry.hooks_for(URGENT_HOOK):
            ticket = router.store.ticket(handled.ticket_id)
            experts = self.routes.get(event.event_type) or EVENT_DOMAINS.get(event.event_type, ())
            ticket = ticket.model_copy(update={"experts": tuple(experts)})
            router.store.save_ticket(ticket, run_id=router.run_id)
            router._dispatch(ticket, event.at, reason="urgent")
        if handled.ticket_id:
            self._mirror(handled.ticket_id, event.at)
        self._mirror_runs()

    def step(self, now: datetime) -> None:
        self.router.step(now)
        self._mirror_runs()
        self._sync(now)

    # ---- 专家一轮（B29 之前的同步替身）
    def _round(self, router: Router, ticket: Ticket, binding: RunBinding) -> None:
        st = self._view(ticket)
        self.run_round(st, binding.trigger_at, self.reason)
        status = st.ticket.status
        router.complete_round(ticket.ticket_id, binding.round)
        if status != "resolved":  # 待人处理或受阻：complete_round 按“有提案”判的 resolved 不算数
            router.store.set_ticket_status(ticket.ticket_id, status)

    def adopt(self, outcome: Any) -> Any:
        """提案以落库为准：运行记录、动作来源与提案库用同一个编号。"""
        if outcome.proposal is None:
            return outcome
        proposal = self._persist(outcome.proposal)
        return replace(outcome, proposal=proposal, record=outcome.record.model_copy(update={"proposal_ids": (proposal.proposal_id,)}))

    def _persist(self, proposal: ExpertProposal) -> ExpertProposal:
        if (proposal.proposal_id or "").startswith(PREFIX):  # 已落库的不重新编号
            return proposal
        sink = self.router.sink
        return sink.get(sink.save(proposal, proposal.binding))

    def deliver(self, proposal: ExpertProposal, st: TicketState | None = None) -> int:
        proposal = self._persist(proposal)
        if self.runner.registry.hooks_for(PROPOSALS_READY_HOOK):
            self.runner.record_proposal(proposal)
            return 0
        return super().deliver(proposal, st)

    def save(self, st: TicketState, at: datetime) -> None:
        self.router.store.save_ticket(st.ticket, run_id=self.router.run_id)
        super().save(st, at)

    def close(self, event_id: str, at: datetime, summary: str) -> None:
        st = self.runner.tickets.resolve_event(event_id)
        if st is None:  # 未路由/噪声事件的纠错不中止运行
            self.runner.note("correct_ignored", at, {"event_id": event_id, "reason": "事件未路由到事件单"})
            return
        self.router.close(st.ticket.ticket_id, at, summary)
        super().close(event_id, at, summary)

    # ---- 分叉
    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        self.router.on_fork(parent_run, child_run, at)
        # 提案库按运行隔离：分叉后 live 提案写进子运行库，父运行库接在后面供召回，编号跨运行链不重复；
        # 在途的父运行调用按 binding.run_id 仍写回父运行库
        r = self.runner
        sink = SqliteProposalSink(r.db.writer, f"{r.db.plant_id}#{child_run.run_id}", parent=self.router.sink,
                                  run_id=child_run.run_id)
        self.router.sink = sink
        self.router.recall.sink = sink
        r.registry.ports[ProposalSink] = sink

    def redispatch(self, at: datetime) -> None:
        router = self.router
        store = router.store
        redo = {t.ticket_id for t in store.open_tickets()
                if store.channel_of(t.ticket_id) != "urgent" and (store.probe_of(t.ticket_id) or {}).get("state", "done") == "done"}
        self.retract(redo, at)
        for ticket_id in redo:  # 等人的事件单也按新知识重判；Router 只跳过待试探的
            store.set_ticket_status(ticket_id, "open")
        self.reason = "fork"
        try:
            dispatches = router.resume_after_fork(at, self.runner.knowledge_version)
        finally:
            self.reason = "event"
        for dispatch in dispatches:
            self._mirror(dispatch.ticket.ticket_id, at)

    # ---- 视图
    def _view(self, ticket: Ticket) -> TicketState:
        tickets = self.runner.tickets.tickets
        latest = self.events[ticket.event_ids[-1]]
        st = tickets.get(ticket.ticket_id)
        if st is None:
            first = self.events[ticket.event_ids[0]]
            st = tickets[ticket.ticket_id] = TicketState(ticket=ticket, event={}, origin_event_at=first.at, trigger_at=first.at,
                                                         entity_id=first.entity_id)
        st.ticket, st.event = ticket, latest.model_dump(mode="json")
        return st

    def _mirror(self, ticket_id: str, at: datetime) -> None:
        ticket = self.router.store.ticket(ticket_id)
        if ticket is not None:
            self.runner.save_ticket(self._view(ticket), at)

    def _sync(self, at: datetime) -> None:
        """下游异步改了事件单（complete_round 等）时，把场景视图与 trace 同步到 RouterStore。

        只在 RouterStore 有事件单写入或每 15 分钟兜底时重读；逐分钟逐张查询会占掉单遍耗时的大头（#perf）。
        """
        store = self.router.store
        revision = store.ticket_revision
        if revision == self._synced_ticket_revision and at.minute % 15 != 0:
            return
        views = self.runner.tickets.tickets
        for ticket_id in dict.fromkeys(self.runner.tickets.by_event.values()):
            ticket = store.ticket(ticket_id)
            if ticket is not None and (ticket_id not in views or views[ticket_id].ticket != ticket):
                self.runner.save_ticket(self._view(ticket), at)
        self._synced_ticket_revision = revision

    def _mirror_runs(self) -> None:
        runs = self.router.runs
        for record in runs[self._mirrored_runs:]:
            self.runner.record_agent_run(record)
        self._mirrored_runs = len(runs)
