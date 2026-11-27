"""B28 arbitration hooks: safety veto, simulation-scored combinations, and diagnosis decisions."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.agent.router.router import PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, URGENT_HOOK
from plantkg.core.contracts import (
    AgentResultKind,
    DayDecision,
    DayEvent,
    DecisionConflict,
    DecisionConversion,
    DecisionResult,
    DeliveryOutcome,
    ExpertProposal,
    Interval,
    PlantkgError,
    SimAction,
    WorkOrder,
)
from plantkg.core.ports import AgentResultSource

ARBITRATION_KEY = "arbitration"
CONSULTATION_HOOK = "consultation.service"
EXECUTION_HOOK = "router.execution"  # B47 分级执行：裁决选中的动作先过它的策略再执行
PRIORITY_HOOK = "router.priority"  # B25 图谱优先级：按（区域, 时刻）返回当时顺序与知识版本
DEFAULT_PRIORITY = ("safety", "production", "comfort", "cost")

# 第 3 层裁决的输出契约：由 B28 定义，写进每次 query；主 Agent 只回这一个 JSON 对象
ARBITRATION_FORMAT = (
    "只输出一个 JSON 对象：{\"selected_proposal_id\": 候选提案里的 proposal_id，无法裁决时写 null，"
    "\"summary\": 一到三句中文裁决依据，\"question\": null 或要问值班人员的一个具体问题}。"
    "selected_proposal_id 必须是下面候选提案里的 proposal_id；回答里不要写其他文字。"
)


def compose_query(ticket_id: str, round_: int, at: datetime, event: DayEvent | None,
                  proposals: Sequence[ExpertProposal], priority: Sequence[str], conflict: str) -> str:
    """第 3 层 query：事件、分歧摘要、双方 proposal_id/诊断/证据缺口、优先级与 as_of，格式与 B29 调查 query 对齐。"""
    lines = [f"- {p.expert}（{p.proposal_id}）：{p.diagnosis}"
             + (f"；证据：{', '.join(p.evidence_refs)}" if p.evidence_refs else "；证据缺口：没有引用依据")
             for p in proposals]
    event_text = (f"事件：{event.event_type}，严重度 {event.severity}，{event.at.isoformat()}，"
                  f"对象 {event.entity_id}：{event.summary}。" if event is not None else "")
    return (f"事件单 {ticket_id} 第 {round_} 轮专家诊断分歧，交第 3 层裁决；当前时刻 {at.isoformat()}。\n"
            f"{event_text}\n分歧摘要：{conflict}\n候选提案：\n" + "\n".join(lines)
            + f"\n优先级配置：{' > '.join(priority)}\n" + ARBITRATION_FORMAT)


def parse_decision(result: Mapping[str, Any]) -> dict[str, Any]:
    """把裁决来源的返回统一成 {selected_proposal_id, summary, question}；不符合契约抛 ValueError，由调用方降级。

    live 来源只给 {status, agent_run_id, terminal_reason, final_answer}，字段在 final_answer 的 JSON 里；
    确定性来源没有 final_answer，直接就是解析后的格式。
    """
    if "final_answer" not in result:
        return dict(result)
    if result.get("status") != "finished":
        raise ValueError(f"裁决运行未完成：{result.get('status')}（{result.get('terminal_reason')}）")
    try:
        answer = json.loads(str(result.get("final_answer") or ""))
    except json.JSONDecodeError:
        raise ValueError("裁决回答里没有 JSON 对象") from None
    if not isinstance(answer, dict):
        raise ValueError("裁决回答不是 JSON 对象")
    selected, summary, question = (answer.get(k) for k in ("selected_proposal_id", "summary", "question"))
    if ((selected is not None and not isinstance(selected, str)) or not (isinstance(summary, str) and summary.strip())
            or (question is not None and not isinstance(question, str))):
        raise ValueError("裁决回答不符合输出契约")
    return dict(result) | {"selected_proposal_id": selected, "summary": summary.strip(), "question": question}


class ArbitrationService:
    # 裁决钩子在 live 下提交后台任务、立即返回 None；值守层据此不抢先写 resolved（#168 F1）
    async_decisions = True

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self._pending_layer3: set[tuple[str, int]] = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arbitration")
        self._jobs: dict[tuple[str, int, int], Future] = {}
        self._jobs_lock = Lock()

    def on_proposals_ready(self, ticket_id: str, round_: int) -> DayDecision | None:
        found = self._find(ticket_id, round_)
        # Offline runs apply decisions at the current simulation instant; only live waits must leave the router loop.
        if found is not None and found[0].mode != "live":
            return self._process_proposals_ready(ticket_id, round_)
        self._submit(ticket_id, round_, 2, self._process_proposals_ready)
        return None

    def on_replies_ready(self, ticket_id: str, round_: int) -> DayDecision | None:
        found = self._find(ticket_id, round_)
        if found is not None and found[0].mode != "live":
            return self._process_replies_ready(ticket_id, round_)
        self._submit(ticket_id, round_, 3, self._process_replies_ready)
        return None

    def wait_for_decision(self, ticket_id: str, round_: int, timeout: float | None = None) -> DayDecision | None:
        """Wait for a submitted decision; intended for demos and tests, not the router loop."""
        key = (ticket_id, round_)
        with self._jobs_lock:
            layer2 = self._jobs.get((*key, 2))
            layer3 = self._jobs.get((*key, 3))
        job = layer2 or layer3
        if job is None:
            return None
        started = monotonic()
        try:
            result = job.result(timeout)
            with self._jobs_lock:
                layer3 = self._jobs.get((*key, 3))
            if job is layer2 and layer3 is not None:
                remaining = None if timeout is None else max(0.0, timeout - (monotonic() - started))
                return layer3.result(remaining)
            return result
        except TimeoutError:
            return None

    def _submit(self, ticket_id: str, round_: int, layer: int, handler: Any) -> Future:
        key = (ticket_id, round_, layer)
        with self._jobs_lock:
            current = self._jobs.get(key)
            if current is not None:
                return current
            job = self._executor.submit(handler, ticket_id, round_)
            self._jobs[key] = job
            return job

    def _process_proposals_ready(self, ticket_id: str, round_: int) -> DayDecision | None:
        found = self._find(ticket_id, round_)
        if found is None:
            return None
        router, runner = found
        ticket = router.store.ticket(ticket_id)
        proposals = tuple(sorted(router.sink.list(ticket_id, round_), key=lambda p: p.proposal_id or ""))
        if ticket is None or not proposals or ticket.round != round_:
            return None
        if runner is not None:
            self._expire_pending_work_orders(runner, ticket_id)
            recorded = {p.proposal_id for p in runner.trace.proposals}
            for proposal in proposals:
                if proposal.proposal_id not in recorded:
                    runner.record_proposal(proposal)

        event_row = router.store.event(ticket.event_ids[-1]) if ticket.event_ids else None
        event = event_row["event"] if event_row else None
        event_safety = bool((event.payload if event else {}).get("safety_tags"))
        vetoed = tuple(p for p in proposals if p.safety_tags or event_safety)
        remaining = tuple(p for p in proposals if p not in vetoed)
        safety_decision = None
        if vetoed:
            denied_actions = tuple(a for p in vetoed for a in p.actions)
            self._escalate(router, runner, ticket_id, "action_safety", vetoed, denied_actions=denied_actions)
            safety_decision = self._record(router, runner, ticket_id, round_, 1, vetoed,
                                           DecisionConversion(policy_version=router.policy.policy_version, scores={},
                                                              notes=("安全标记触发否决；被否决动作经 router.urgent 交 B47",)),
                                           DecisionResult(selected_proposal_ids=tuple(p.proposal_id for p in vetoed if p.proposal_id),
                                                          outcome="work_order", summary="安全标记动作已否决并转人工复核"),
                                           "安全标记提案及其动作")
            if not remaining:
                self._hold(ticket_id, "waiting")
                return safety_decision

        if any(a.readiness == "needs_diagnosis" for p in remaining for a in p.actions):
            self._pending_layer3.add((ticket_id, round_))
            if self._all_replies_received(router, remaining):
                self.on_replies_ready(ticket_id, round_)

        if runner is None:
            return self._record(router, runner, ticket_id, round_, 2, proposals,
                                DecisionConversion(policy_version=router.policy.policy_version, scores={},
                                                   notes=("场景执行器未绑定，未执行动作",)),
                                DecisionResult(selected_proposal_ids=(), outcome="blocked", summary="场景执行器未绑定"),
                                "提案待裁决")

        state = self._ticket_state(runner, router, ticket_id, event)
        dependent = any(a.readiness == "needs_diagnosis" for p in remaining for a in p.actions)
        if dependent and "diagnosis" not in state.pending:
            state.pending.append("diagnosis")
            self._hold(ticket_id, "open")
        human = [p for p in remaining if any(a.readiness == "needs_human" for a in p.actions)]
        for proposal in human:
            actions = tuple(a for a in proposal.actions if a.readiness == "needs_human")
            self._deliver(runner, proposal.model_copy(update={"actions": actions}), state)
        if human:
            self._hold(ticket_id, "waiting")

        ready = [p for p in remaining if any(a.readiness == "ready" for a in p.actions)]
        if not ready:
            if human or vetoed:
                state.pending.append("work_order")
                self._hold(ticket_id, "waiting")
                outcome, summary = "work_order", "需人工确认的动作已交操作工单"
            elif dependent:
                state.pending.append("diagnosis")
                self._hold(ticket_id, "open")
                outcome, summary = "blocked", "等待根因诊断；依赖动作保持待决"
            else:
                outcome, summary = "executed", "没有需要自动执行的动作"
                self._settle_executed(ticket_id, runner)
            return self._record(router, runner, ticket_id, round_, 2, proposals,
                                DecisionConversion(policy_version=router.policy.policy_version, scores={}),
                                DecisionResult(selected_proposal_ids=(), outcome=outcome, summary=summary),
                                self._conflict_summary(proposals))

        selected, conversion = self.arbitrate(ready, policy=router.policy, snapshot=runner.snapshot(),
                                              trial_runner=router.trial_runner)
        if selected:
            score = conversion.scores["+".join(p.proposal_id or p.expert for p in selected)]
            selected_ids = tuple(p.proposal_id for p in selected if p.proposal_id)
            receipts = []
            for proposal in selected:
                actions = tuple(a for a in proposal.actions if a.readiness == "ready")
                receipts.append(self._deliver(runner, proposal.model_copy(update={"actions": actions}), state))
            outcome, reason = self._combined_outcome(receipts)
            summary = f"仿真加权得分 {score:.2f}，选中 {', '.join(selected_ids)}"
            if reason:
                summary += f"；{reason}"
            if outcome == "blocked":
                self._hold(ticket_id, "blocked")
            elif outcome in ("work_order", "to_human"):
                self._hold(ticket_id, "waiting")
            else:
                self._settle_executed(ticket_id, runner)
        else:
            selected_ids = ()
            self._escalate(router, runner, ticket_id, "arbitration_infeasible", ready)
            outcome, summary = "work_order", "全部 ready 动作组合不可行，已升级操作工单"
            self._hold(ticket_id, "blocked")

        decision = self._record(router, runner, ticket_id, round_, 2, proposals, conversion,
                                DecisionResult(selected_proposal_ids=selected_ids, outcome=outcome, summary=summary),
                                self._conflict_summary(proposals))
        return decision

    def _process_replies_ready(self, ticket_id: str, round_: int) -> DayDecision | None:
        found = self._find(ticket_id, round_)
        if found is None:
            return None
        router, runner = found
        ticket = router.store.ticket(ticket_id)
        proposals = tuple(sorted(router.sink.list(ticket_id, round_), key=lambda p: p.proposal_id or ""))
        if ticket is None or ticket.round != round_ or not proposals:
            return None
        diagnoses = {p.diagnosis for p in proposals if p.diagnosis}
        dependent = any(a.readiness == "needs_diagnosis" for p in proposals for a in p.actions)
        if len(diagnoses) < 2 and not dependent:
            return None
        self._pending_layer3.discard((ticket_id, round_))
        binding = next((p.binding for p in proposals if p.binding is not None), None)
        if binding is None:
            return None
        replied = getattr(router.sink, "replied", None)
        eligible = tuple(p for p in proposals if p.proposal_id and
                         (router.mode != "live" or (callable(replied) and replied(p.proposal_id))) and
                         self._reply_status(router, p) == "done" and
                         self._reply_diagnosis(router, p))
        if dependent and not eligible:
            self._hold(ticket_id, "blocked")
            unresolved = tuple(p for p in proposals if any(a.readiness == "needs_diagnosis" for a in p.actions))
            self._escalate(router, runner, ticket_id, "diagnosis_unresolved", unresolved,
                           denied_actions=tuple(a for p in unresolved for a in p.actions
                                                if a.readiness == "needs_diagnosis"))
            return self._record(router, runner, ticket_id, round_, 3, proposals,
                                DecisionConversion(policy_version=router.policy.policy_version, scores={},
                                                   notes=("没有有效专家诊断，依赖动作保持待决",)),
                                DecisionResult(selected_proposal_ids=(), outcome="blocked", summary="没有有效根因结论，转人工处理"),
                                "专家回答未形成可用诊断")
        event_row = router.store.event(ticket.event_ids[-1]) if ticket.event_ids else None
        event = event_row["event"] if event_row else None
        at = event.at if event is not None else binding.trigger_at
        priority, knowledge_version = self._priority(router, runner, ticket, proposals)
        conflict = self._conflict_summary(proposals)
        payload = {"ticket_id": ticket_id, "round": round_, "as_of": at.isoformat(),
                   "proposals": [p.model_dump(mode="json") for p in eligible],
                   "priority": list(priority), "knowledge_version": knowledge_version, "conflict": conflict,
                   "query": compose_query(ticket_id, round_, at, event, eligible, priority, conflict)}
        if event is not None:
            payload["event"] = event.model_dump(mode="json")
        try:
            source = router.registry.resolve_mode(AgentResultSource, router.mode, ARBITRATION_KEY)
            result = parse_decision(source.get(AgentResultKind.arbitration, binding, payload))
            if result.get("selected_proposal_id") is not None and \
                    result["selected_proposal_id"] not in {p.proposal_id for p in eligible}:
                raise ValueError(f"选中的提案 {result['selected_proposal_id']} 不在本轮候选里")
        except PlantkgError as exc:  # 裁决来源不可用：按确定性优先级降级，并写明来源与原因
            fallback = self._deterministic(payload, router.policy)
            result = fallback | {"summary": f"主 Agent 裁决不可用（{str(exc)[:160]}），按确定性优先级降级；"
                                            f"{fallback.get('summary') or ''}"}
        except ValueError as exc:  # 回答缺 JSON/字段不符/选中未知提案：回退第 2 层结果（依赖动作保持待决）
            result = {"summary": f"主 Agent 裁决回答不可用（{str(exc)[:160]}），回退第 2 层结果"}
        selected_id = result.get("selected_proposal_id")
        selected = next((p for p in eligible if p.proposal_id == selected_id), None)
        summary = str(result.get("summary") or (f"主 Agent 选中 {selected.expert}" if selected else "缺少有效诊断，保持待决"))
        ids = (selected.proposal_id,) if selected and selected.proposal_id else ()
        outcome = "work_order" if selected else "blocked"
        if result.get("question"):
            ask = router.registry.tools.get("ask_operator")
            if ask is not None:
                ask.handler(None, str(result["question"]), ticket_id)
            outcome, summary = "to_human", f"现场信息不足，已向值班人员提问：{result['question']}"
            self._hold(ticket_id, "waiting")
        elif dependent and selected is not None and selected.diagnosis:
            actions = tuple(a.model_copy(update={"readiness": "ready", "depends_on": ()})
                            for a in selected.actions if a.readiness == "needs_diagnosis")
            if actions and runner is not None:
                # 第 3 层按 layer3 预算重算生效时刻，不再拿专家请求时的 layer12 timing 判 stale（#142）
                coordinator = getattr(runner, "coordinator", None)
                if selected.binding is not None and selected.timing is not None and coordinator is not None:
                    effective3 = coordinator.effective_at(selected.binding.trigger_at, layer=3)
                    selected = selected.model_copy(update={"timing": selected.timing.model_copy(update={"effective_at": effective3})})
                receipt = self._deliver(runner, selected.model_copy(update={"actions": actions}),
                                        self._ticket_state(runner, router, ticket_id, event))
                mapped, reason = self._decision_outcome(receipt)
                state = runner.tickets.tickets[ticket_id]
                state.pending = [p for p in state.pending if p != "diagnosis"]
                if mapped == "blocked":
                    outcome, summary = "blocked", f"依赖动作退回：{reason or '执行前复核不可行'}"
                    self._hold(ticket_id, "blocked")
                else:
                    if mapped in ("work_order", "to_human"):
                        outcome = "work_order"
                        if "work_order" not in state.pending:
                            state.pending.append("work_order")
                        summary = f"{summary}；依赖动作交人工确认：{reason}" if reason else f"{summary}；依赖动作交人工确认"
                    else:
                        outcome = "executed"
                    status = "waiting" if state.pending else "resolved"
                    state.update(status=status)
                    router.store.set_ticket_status(ticket_id, status)
                    self._hold(ticket_id, status)
            else:
                outcome, summary = "blocked", "依赖动作没有可交付的执行内容，保持待决"
                self._hold(ticket_id, "blocked")
        elif dependent:
            if selected is not None:
                summary = "未形成有效根因结论，依赖动作保持待决"
            outcome = "blocked"
            if hasattr(router.store, "set_ticket_status"):
                router.store.set_ticket_status(ticket_id, "blocked")
            self._hold(ticket_id, "blocked")
        elif selected:
            # 非依赖动作的第 3 层只记录选择，不写 executed（#101）；需要处置的留人工复核
            outcome, summary = "work_order", f"{summary}；主 Agent 只记录选择，非依赖动作不自动执行，交人工复核"
            self._hold(ticket_id, "waiting")
        conversion = DecisionConversion(policy_version=router.policy.policy_version, scores={}, notes=(summary,))
        return self._record(router, runner, ticket_id, round_, 3, proposals, conversion,
                            DecisionResult(selected_proposal_ids=ids, outcome=outcome, summary=summary),
                            "量化不了的诊断分歧交主 Agent 裁决")

    def _find(self, ticket_id: str, round_: int) -> tuple[Any, Any] | None:
        for consultation in reversed(self.registry.hooks_for(CONSULTATION_HOOK)):
            found = consultation.find(ticket_id, round_)
            if found is not None:
                return found
        return None

    def _hold(self, ticket_id: str, status: str) -> None:
        found = self._find(ticket_id, None)
        if found is None:
            return
        router, runner = found
        if router.mode != "live":
            for consultation in self.registry.hooks_for(CONSULTATION_HOOK):
                consultation.held_status[ticket_id] = status
        if runner is not None:
            state = runner.tickets.tickets.get(ticket_id)
            if state is not None:
                state.update(status=status)
                save = getattr(runner.dispatcher, "save", None)
                if callable(save):
                    save(state, runner.now)
        if hasattr(router.store, "set_ticket_status"):
            router.store.set_ticket_status(ticket_id, status)

    @staticmethod
    def _expire_pending_work_orders(runner: Any, ticket_id: str) -> None:
        work_orders = getattr(getattr(runner, "tickets", None), "work_orders", {})
        for work_order_id, order in list(work_orders.items()):
            if order.ticket_id == ticket_id and order.status == "proposed":
                work_orders[work_order_id] = order.model_copy(update={"status": "expired"})
                runner.save_work_order(work_orders[work_order_id])
        state = getattr(getattr(runner, "tickets", None), "tickets", {}).get(ticket_id)
        if state is not None:  # 上一轮工单已作废：待人工事项随之清掉，本轮执行完成才能落到 resolved
            state.pending = [item for item in state.pending if item != "work_order"]

    def _settle_executed(self, ticket_id: str, runner: Any) -> None:
        """本轮没有待人工事项才落 resolved；async（live）下终态由裁决回写，complete_round 不再抢先写。"""
        state = getattr(getattr(runner, "tickets", None), "tickets", {}).get(ticket_id) if runner is not None else None
        if state is not None and not [item for item in state.pending if item != "diagnosis"]:
            self._hold(ticket_id, "resolved")

    def _escalate(self, router: Any, runner: Any, ticket_id: str, reason: str,
                  proposals: tuple[ExpertProposal, ...] | list[ExpertProposal], *, denied_actions: tuple[Any, ...] | None = None) -> None:
        actions = denied_actions if denied_actions is not None else tuple(a for p in proposals for a in p.actions)
        router.escalate(ticket_id, reason, denied_actions=actions)
        if router.registry.hooks_for(URGENT_HOOK) or runner is None or not callable(getattr(runner, "save_work_order", None)):
            return
        ticket = router.store.ticket(ticket_id)
        event_row = router.store.event(ticket.event_ids[-1]) if ticket and ticket.event_ids else None
        event = event_row["event"] if event_row else None
        state = self._ticket_state(runner, router, ticket_id, event)
        order = WorkOrder(work_order_id=runner.next_id("WO"), ticket_id=ticket_id, created_at=runner.now,
                          reason=f"{reason}；待人工复核提案 {', '.join(p.proposal_id or p.expert for p in proposals)}",
                          actions=actions, status="proposed")
        runner.tickets.work_orders[order.work_order_id] = order
        runner.save_work_order(order)
        if "work_order" not in state.pending:
            state.pending.append("work_order")
        state.update(status="waiting")
        router.store.set_ticket_status(ticket_id, "waiting")

    @staticmethod
    def _reply_status(router: Any, proposal: ExpertProposal) -> str | None:
        if proposal.proposal_id is None or proposal.binding is None:
            return None
        status = router.store.ticket_state(proposal.ticket_id,
                                           f"reply_status_{proposal.binding.round}_{proposal.proposal_id}")
        return status

    @staticmethod
    def _reply_diagnosis(router: Any, proposal: ExpertProposal) -> str | None:
        if proposal.proposal_id is None or proposal.binding is None:
            return None
        return router.store.ticket_state(proposal.ticket_id,
                                         f"reply_diagnosis_{proposal.binding.round}_{proposal.proposal_id}") or None

    @staticmethod
    def _conflict_summary(proposals: tuple[ExpertProposal, ...]) -> str:
        items = [f"{p.expert}: {p.diagnosis or '无诊断'}" for p in proposals]
        return "；".join(items) or "无有效提案"

    def step(self, run_id: str, now: Any) -> None:
        for ticket_id, round_ in tuple(self._pending_layer3):
            found = self._find(ticket_id, round_)
            if found is None:
                continue
            router, _ = found
            if router.run_id != run_id:
                continue
            proposals = tuple(router.sink.list(ticket_id, round_))
            trigger = router.store.round_trigger(ticket_id, round_)
            budget = router.policy.response_budget.layer3_min
            due = trigger is not None and now >= trigger + timedelta(minutes=budget)
            if self._all_replies_received(router, proposals) or due:
                self.on_replies_ready(ticket_id, round_)

    @staticmethod
    def _all_replies_received(router: Any, proposals: tuple[ExpertProposal, ...]) -> bool:
        replied = getattr(router.sink, "replied", None)
        return bool(proposals) and callable(replied) and all(p.proposal_id and replied(p.proposal_id) for p in proposals)

    @staticmethod
    def _sim_action(action: Any) -> SimAction:
        return SimAction(entity_id=action.entity_id, command=action.command, params=action.params, interval=action.interval)

    @staticmethod
    def _deliver(runner: Any, proposal: ExpertProposal, state: Any) -> int:
        registry = getattr(runner, "registry", None)
        if registry is not None:  # B31 预检与收尾覆盖两条交付路径，B47 接入后不再被短路（#105）
            for hook in registry.hooks_for("arbitration.evidence_preflight"):
                proposal = hook(runner, proposal, state)
        try:
            handlers = runner.registry.hooks_for(EXECUTION_HOOK)
            if handlers:  # B47 已接入：执行由分级执行按策略决定（幂等、抽查、工单）；未接时仍走直派器
                return handlers[-1](runner, proposal, state)
            from plantkg.world.scenario.dispatch import DirectDispatcher

            return DirectDispatcher.deliver(runner.dispatcher, proposal, state)
        finally:
            if registry is not None:
                for hook in registry.hooks_for("arbitration.evidence_finish"):
                    hook(runner, proposal)

    @staticmethod
    def _decision_outcome(receipt: Any) -> tuple[str, str]:
        """B47 执行回执 → DecisionResult.outcome（#168）：排队/工单→work_order，infeasible/stale→blocked。"""
        if isinstance(receipt, DeliveryOutcome):
            if receipt.outcome in ("queued_work_order", "queued_human"):
                return "work_order", receipt.reason
            if receipt.outcome in ("blocked_infeasible", "stale"):
                return "blocked", receipt.reason
            if receipt.outcome == "correction":
                return "to_human", receipt.reason
            return "executed", receipt.reason
        if isinstance(receipt, int):  # 未接 B47 时的直派器回执
            return ("executed" if receipt else "blocked"), ("" if receipt else "直派器没有执行动作")
        return "executed", ""

    def _combined_outcome(self, receipts: list[Any]) -> tuple[str, str]:
        mapped = [self._decision_outcome(receipt) for receipt in receipts]
        for wanted in ("blocked", "work_order", "to_human"):
            for outcome, reason in mapped:
                if outcome == wanted:
                    return outcome, reason
        return "executed", ""

    def arbitrate(self, proposals: tuple[ExpertProposal, ...] | list[ExpertProposal], *, policy: Any,
                  snapshot: Any, trial_runner: Any) -> tuple[tuple[ExpertProposal, ...], DecisionConversion]:
        candidates = sorted(proposals, key=lambda p: p.proposal_id or p.expert)
        ready = tuple(self._sim_action(a) for p in candidates for a in p.actions if a.readiness == "ready")
        if not ready:
            return (), DecisionConversion(policy_version=policy.policy_version, scores={}, infeasible=(), notes=())
        # 同一轮所有组合的统一比较窗口：全部候选 ready 动作的最早开始到最晚结束（试算器再截到剧本结束）。
        unified = Interval(start=min(a.interval.start for a in ready), end=max(a.interval.end for a in ready))
        groups: list[tuple[str, tuple[SimAction, ...], tuple[ExpertProposal, ...]]] = []
        for size in range(1, len(candidates) + 1):
            for group in itertools.combinations(candidates, size):
                actions = tuple(self._sim_action(a) for p in group for a in p.actions if a.readiness == "ready")
                if actions:
                    groups.append(("+".join(p.proposal_id or p.expert for p in group), actions, group))

        def evaluate(horizon_of: Callable[[tuple[SimAction, ...]], Interval] | None):
            """按给定窗口试算全部组合；horizon_of 为 None 时用统一比较窗口。"""
            scores: dict[str, float] = {}
            infeasible: list[str] = []
            # (剩余越界分钟, 代价, 基线越界分钟, 违规实体名, 组合)
            evaluated: list[tuple[int, float, int, str, tuple[ExpertProposal, ...]]] = []
            baseline = 0  # 试算空候选基线上的既有违规条数（#173 相对语义）
            for key, actions, group in groups:
                horizon = unified if horizon_of is None else horizon_of(actions)
                trial = trial_runner(snapshot, actions, horizon)
                existing = getattr(trial, "baseline_violations", ()) or ()
                baseline = max(baseline, len(existing))
                if not trial.feasible:
                    infeasible.append(key)
                    continue
                score = self._score(trial.soft_cost, policy.arbitration_weights)
                scores[key] = score
                remaining = int(getattr(trial, "remaining_violation_minutes", 0) or 0)
                evaluated.append((remaining, score, len({v.at for v in existing}), self._violation_label(existing), group))
            return scores, infeasible, evaluated, baseline

        def own_window(actions: tuple[SimAction, ...]) -> Interval:
            return Interval(start=min(a.interval.start for a in actions), end=max(a.interval.end for a in actions))

        # 候选窗口不一致时无条件按统一窗口复算：既有越界落在候选窗口空档时自身窗口看不见它，
        # 不能再以“自身窗口是否看到既有越界”作开关（#240 复审 P1、#258）。
        if any(own_window(actions) != unified for _, actions, _ in groups):
            scores, infeasible, evaluated, baseline_count = evaluate(None)
        else:
            scores, infeasible, evaluated, baseline_count = evaluate(own_window)
        # #201-5：先比动作后剩余越界分钟数，其次原代价，最后沿用组合内提案编号
        chosen = min(evaluated, key=lambda item: (item[0], item[1], tuple(p.proposal_id or "" for p in item[4]))) if evaluated else None
        selected = chosen[4] if chosen else ()
        notes: tuple[str, ...] = ()
        if baseline_count:
            note = f"环境既有越界 {baseline_count} 条（空候选基线），候选按相对违规判定可行"
            if chosen is not None and chosen[0] < chosen[2]:
                note += f"；优先减少越界：{chosen[3]}越界 {chosen[2]} 分钟 → {chosen[0]} 分钟"
            notes = (note,)
        conversion = DecisionConversion(policy_version=policy.policy_version, scores=scores, infeasible=tuple(infeasible), notes=notes)
        return selected, conversion

    @staticmethod
    def _violation_label(violations: Any) -> str:
        """违规实体名：取 entity_id 末段（区域/设备编号），多个用顿号连接；取不到写“既有”。"""
        names: list[str] = []
        for violation in violations:
            entity = str(getattr(violation, "entity_id", "") or "").rstrip("/").rsplit("/", 1)[-1]
            if entity and entity not in names:
                names.append(entity)
        return "、".join(names) if names else "既有"

    @staticmethod
    def _score(cost: Any, weights: dict[str, float]) -> float:
        return sum(float(weights.get(name, 0.0)) * float(getattr(cost, name))
                   for name in ("cny", "wait_min", "comfort_min"))

    @staticmethod
    def _ticket_state(runner: Any, router: Any, ticket_id: str, event: Any) -> Any:
        from plantkg.world.scenario.dispatch import TicketState

        ticket = router.store.ticket(ticket_id)
        state = runner.tickets.tickets.get(ticket_id)
        if state is None:
            state = TicketState(ticket=ticket, event=event.model_dump(mode="json") if event else {},
                                origin_event_at=ticket.opened_at, trigger_at=router.store.round_trigger(ticket_id, ticket.round) or ticket.opened_at,
                                entity_id=event.entity_id if event else None)
            runner.tickets.tickets[ticket_id] = state
        state.ticket = ticket
        return state

    def _record(self, router: Any, runner: Any, ticket_id: str, round_: int, layer: int,
                proposals: tuple[ExpertProposal, ...], conversion: DecisionConversion,
                result: DecisionResult, summary: str) -> DayDecision:
        """各层都用 round trigger（仿真时刻）；导出按 DayDecision.at 分日，第 3 层不再写实际决策时刻。"""
        trigger = router.store.round_trigger(ticket_id, round_) or router._wall()
        record = DayDecision(decision_id=f"decision-{ticket_id.rsplit('/', 1)[-1]}-{round_}-{layer}",
                             ticket_id=ticket_id, round=round_, layer=layer, at=trigger,
                             conflict=DecisionConflict(summary=summary,
                                                       proposal_ids=tuple(p.proposal_id for p in proposals if p.proposal_id)),
                             conversion=conversion, result=result)
        if runner is not None:
            runner.record_decision(record)
        return record

    @classmethod
    def _priority(cls, router: Any, runner: Any, ticket: Any, proposals: tuple[ExpertProposal, ...]) -> tuple[tuple[str, ...], Any]:
        """第 3 层优先级：PolicyConfig 显式配了非 default 的 profiles 才覆盖图谱；否则先问 B25 的
        router.priority 钩子（按区域与时刻），图谱缺失回退 PolicyConfig 的 default。返回（顺序, 知识版本）。"""
        profiles = router.policy.priority_profiles
        if any(name != "default" for name in profiles):  # 部署级显式配置覆盖图谱
            return (cls._priority_from_profiles(router, runner, ticket, proposals, profiles),
                    getattr(router, "knowledge_version", None))
        event = cls._ticket_event(router, ticket)
        zone = cls._event_zone(runner, event)
        at = event.at if event is not None else ticket.opened_at
        for hook in reversed(router.registry.hooks_for(PRIORITY_HOOK)):
            try:
                found = hook(zone, at) or {}
            except Exception:  # noqa: BLE001 — 图谱没就绪不挡裁决
                continue
            order = tuple(found.get("order") or ())
            if order:
                return order, found.get("knowledge_version")
        return tuple(profiles.get("default", DEFAULT_PRIORITY)), getattr(router, "knowledge_version", None)

    @staticmethod
    def _priority_from_profiles(router: Any, runner: Any, ticket: Any, proposals: tuple[ExpertProposal, ...],
                                profiles: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
        """部署级显式优先级配置：先按事件类型，再按厂区当地时段取 profile。"""
        event_row = router.store.event(ticket.event_ids[-1]) if ticket.event_ids else None
        event = event_row["event"] if event_row else None
        if event is not None and event.event_type in profiles:
            return profiles[event.event_type]
        try:
            tz = ZoneInfo(runner.world.site.plant_tz)
        except (AttributeError, KeyError, ValueError):
            tz = ZoneInfo("Asia/Shanghai")
        at = (event.at if event else ticket.opened_at).astimezone(tz)
        profile = "night" if at.hour >= 22 or at.hour < 6 else "morning_peak" if 7 <= at.hour < 10 else "production_hours"
        return profiles.get(profile, profiles.get("default", DEFAULT_PRIORITY))

    @staticmethod
    def _ticket_event(router: Any, ticket: Any) -> Any:
        event_row = router.store.event(ticket.event_ids[-1]) if ticket.event_ids else None
        return event_row["event"] if event_row else None

    @staticmethod
    def _event_zone(runner: Any, event: Any) -> str | None:
        entity = getattr(event, "entity_id", None)
        try:
            return runner.world.index.zone_of(entity) if entity else None
        except (AttributeError, KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _deterministic(payload: dict[str, Any], policy: Any) -> dict[str, Any]:
        priority = payload.get("priority") or policy.priority_profiles.get("default", ("safety", "production", "comfort", "cost"))
        rank = {expert: index for index, expert in enumerate(priority)}
        proposals = [p for p in payload["proposals"] if p.get("diagnosis")]
        if not proposals:
            return {"summary": "没有有效诊断"}
        category = {"energy": "cost", "hvac": "comfort", "elevator": "comfort",
                    "production": "production", "equipment": "production"}
        chosen = min(proposals, key=lambda p: (rank.get(p["expert"], rank.get(category.get(p["expert"], ""), len(rank))), p["expert"]))
        return {"selected_proposal_id": chosen.get("proposal_id"), "summary": f"按 {policy.policy_version} 优先级配置选择 {chosen['expert']}"}


def install(registry: Any) -> ArbitrationService:
    service = ArbitrationService(registry)
    if (AgentResultSource, "deterministic", ARBITRATION_KEY) not in registry.mode_ports:
        registry.register_mode(AgentResultSource, "deterministic", _DeterministicSource(service), key=ARBITRATION_KEY)
    registry.register_hook(PROPOSALS_READY_HOOK, service.on_proposals_ready)
    registry.register_hook(REPLIES_READY_HOOK, service.on_replies_ready)
    registry.register_hook("router.step", service.step)
    return service


def register(registry: Any, settings: Any = None) -> None:
    from plantkg.world.scenario.runner import SCENARIO_HOOK

    def scenario_plugin(scenario_registry: Any) -> None:
        for consultation in registry.hooks_for(CONSULTATION_HOOK):
            scenario_registry.register_hook(CONSULTATION_HOOK, consultation)
        for priority in registry.hooks_for(PRIORITY_HOOK):  # 场景注册表的第 3 层也要问图谱
            scenario_registry.register_hook(PRIORITY_HOOK, priority)
        install(scenario_registry)

    registry.register_hook(SCENARIO_HOOK, scenario_plugin)


class _DeterministicSource:
    def __init__(self, service: ArbitrationService) -> None:
        self.service = service

    def get(self, kind: AgentResultKind, binding: Any, payload: Any) -> dict[str, Any]:
        found = self.service._find(binding.ticket_id, binding.round)
        if found is None:
            return {"summary": "事件单已不在当前运行中"}
        return self.service._deterministic(payload, found[0].policy)
