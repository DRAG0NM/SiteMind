"""慢通道的注册表工具（D53/D54）：只读试算、向值班人员提问、升级工单。观测读数用 B22 的 sensor_query。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from plantkg.core.contracts import (
    Actor, Context, ErrorCode, Interval, PendingActionKind, PlantkgError, SimAction, ToolResult, tool_error,
)
from plantkg.core.ports import PendingActionStore, RUN_BINDING_HOOK

log = logging.getLogger("plantkg.router")


def make_tools(router: Any) -> dict[str, Callable[..., ToolResult]]:
    def done(data: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, business_status="done", data=data)

    def ticket_of(context: Any, ticket_id: str | None) -> tuple[str | None, str]:
        """运行绑定优先；没有绑定时用模型给的编号，只有一张打开的事件单才替它补上，绝不猜。"""
        hooks = router.registry.hooks_for(RUN_BINDING_HOOK) if context is not None else ()
        bound = hooks[-1](context) if hooks else None
        if bound is not None:
            return bound[0].ticket_id, ""
        if ticket_id:
            ticket = router.store.ticket(ticket_id)
            if ticket is None or ticket.status in ("resolved", "closed"):
                return None, f"事件单 {ticket_id} 不存在或已结束"
            return ticket_id, ""
        tickets = router.store.open_tickets()
        if len(tickets) == 1:
            return tickets[0].ticket_id, ""
        return None, f"有 {len(tickets)} 张打开的事件单，请用 ticket_id 指明" if tickets else "没有打开的事件单"

    def brief(result: Any) -> Any:
        """B17 TrialResult 压成结论：逐分钟账本太长，工具返回有 8KB 上限。"""
        if not hasattr(result, "violations"):
            return result
        return {"feasible": result.feasible, "violations": [v.model_dump(mode="json") for v in result.violations[:8]],
                "violation_count": len(result.violations), "soft_cost": result.soft_cost.model_dump(mode="json")}

    def ask_operator(context: Any, question: str, ticket_id: str | None = None) -> ToolResult:
        """向值班人员提一个问题；答案经 B35 回答卡片回写（router.answer_question），问完继续本轮。"""
        tid, why = ticket_of(context, ticket_id)
        if tid is None:
            return tool_error(ErrorCode.PRECONDITION, why)
        question_id = router.store.ask(tid, question, router._wall())
        action_id = propose_question(router, tid, question_id, question)
        data = {"question_id": question_id, "ticket_id": tid, "status": "pending", "question": question}
        if action_id:
            data["action_id"] = action_id
        return done(data)

    def escalate(context: Any, reason: str, ticket_id: str | None = None) -> ToolResult:
        """把事件单升级为操作工单：只升不降，经 urgent 钩子交 B47。"""
        tid, why = ticket_of(context, ticket_id)
        if tid is None:
            return tool_error(ErrorCode.PRECONDITION, why)
        handled = router.escalate(tid, reason)
        return done({"ticket_id": tid, "state": "urgent", "handled": handled.result})

    def trial(context: Any, actions: list[dict[str, Any]], horizon_min: int | None = None,
              ticket_id: str | None = None) -> ToolResult:
        """B17 TrialRunner 的只读试算：trial(snapshot, candidate, horizon)，不改真实运行。"""
        runner = router._maybe(router.trial_runner, "trial_runner", "router.trial")
        snapshot = router._maybe(router.snapshot, "snapshot", "router.snapshot")
        if runner is None or snapshot is None:
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "试算未接入：需要装配层注入 B17 TrialRunner 与快照")
        tid = ticket_of(context, ticket_id)[0]
        snap = snapshot(router._ticket_event(tid) if tid else None)
        at = getattr(snap, "as_of", None) or router._wall()
        horizon = Interval(start=at, end=at + timedelta(minutes=int(horizon_min or router.config.probe_window_min)))
        candidate = [SimAction.model_validate(a) for a in actions]
        return done({"ticket_id": tid, "result": brief(runner(snap, candidate, horizon))})

    return {"ask_operator": ask_operator, "escalate": escalate, "trial": trial}


def propose_question(router: Any, ticket_id: str, question_id: str, question: str) -> str | None:
    """把提问登记成 question 待确认动作，B35 回答卡从 /actions 取。

    没有待确认动作存储时返回 None，提问本身照常生效；登记失败只记 debug 日志，不能挡住提问。
    """
    store = router.registry.ports.get(PendingActionStore)
    if store is None:
        return None
    context = Context(plant_id=router.plant_id, industry_pack="manufacturing",
                      actor=Actor(actor_id="b27-router", roles=("operator",)), channel="offline",
                      trace_id=f"b27-{question_id}", as_of=router._wall(), run_id=router.run_id,
                      idempotency_key=f"b27-{question_id}")
    # target_ref 必须有；答案由 ActionDecisionRequest.reason 承载，确认时回写 router.answer_question
    payload = {"target_ref": question_id, "question_id": question_id, "question": question, "ticket_id": ticket_id}
    try:
        return store.propose(PendingActionKind.question, payload, context).action_id
    except PlantkgError as exc:
        log.debug("问题 %s 未能登记待确认动作：%s（%s）", question_id, exc.code.value, exc.message, exc_info=True)
    except ValidationError:
        log.debug("问题 %s 的待确认动作不符合契约", question_id, exc_info=True)
    return None


class QuestionActionHandler:
    """回答卡：确认 question 待确认动作 = 把 reason 作为答案回写给提问题的事件单。"""

    kind = "question"

    def __init__(self, router: Any) -> None:
        self.router = router

    def apply(self, action: Any, actor: Actor) -> ToolResult:
        payload = action.payload or {}
        question_id = str(payload.get("question_id") or "")
        if not question_id:
            return tool_error(ErrorCode.PRECONDITION, "question 动作缺少 question_id，答案无处回写")
        self.router.answer_question(question_id, action.reason or "")
        return ToolResult(ok=True, business_status="done",
                          data={"question_id": question_id, "ticket_id": payload.get("ticket_id"), "status": "answered"})

