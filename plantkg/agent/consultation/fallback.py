"""会诊提案就绪的占位消费者（B29）：B28 裁决还没接上时，让演示链路能走到动作。

按 B17 直派器的既有规则逐份生效（硬约束拦截、置信度分级、安全动作转工单）；真登记了别的提案就绪处理器
（B28 的裁决）时自动让位，不在裁决之后重复执行。只处理协同组的场景运行；独立运行与没接场景的主路由不动作。
"""

from __future__ import annotations

from typing import Any

from plantkg.agent.router.router import PROPOSALS_READY_HOOK


class DirectApplyFallback:
    """PROPOSALS_READY 钩子；一个值守程序实例共用一个 Consultation。"""

    def __init__(self, consultation: Any) -> None:
        self.consultation = consultation

    def __call__(self, ticket_id: str, round_: int) -> None:
        found = self.consultation.find(ticket_id, round_)
        if found is None:
            return
        router, runner = found
        # 场景 Router 可使用独立注册表，按当前 Router 判断是否已有 B28 裁决处理器。
        if len(router.registry.hooks_for(PROPOSALS_READY_HOOK)) > 1:
            return
        if runner is None or getattr(runner, "dispatcher", None) is None:
            return
        ticket = router.store.ticket(ticket_id)
        if ticket is None or not ticket.event_ids:
            return
        event = router.store.event(ticket.event_ids[0])
        if event is None:
            return
        from plantkg.world.scenario.dispatch import DirectDispatcher, TicketState

        try:
            first = event["event"]
            trigger = router.store.round_trigger(ticket_id, round_) or ticket.opened_at
            st = runner.tickets.tickets.get(ticket_id) or TicketState(
                ticket=ticket, event=first.model_dump(mode="json"), origin_event_at=first.at, trigger_at=trigger,
                entity_id=first.entity_id)
            st.ticket, st.trigger_at, st.pending = ticket, trigger, []
            runner.tickets.tickets[ticket_id] = st  # 首次派发的临时状态也进场景视图，工单确认与纠错才找得到
            for wid, wo in list(runner.tickets.work_orders.items()):  # 与直派器 run_round 同规则：上一轮未确认的工单随新一轮作废
                if wo.ticket_id == ticket_id and wo.status == "proposed":
                    runner.tickets.work_orders[wid] = wo.model_copy(update={"status": "expired"})
                    runner.save_work_order(runner.tickets.work_orders[wid])
            for proposal in router.sink.list(ticket_id, round_):
                DirectDispatcher.deliver(runner.dispatcher, proposal, st)  # 基类方法：绕过协同组派发器的记录分支
            # 与直派器 run_round 同口径：有待人处理的事项（工单、纠错、待诊断）就不能算已解决
            status = "waiting" if any(p != "diagnosis" for p in st.pending) else "open" if st.pending else "resolved"
            st.update(status=status, closed_at=None)
            runner.dispatcher.save(st, trigger)
            # 路由在钩子返回后会按“有提案就已解决”写状态，这里先记下，由会诊在本轮完成后改回
            self.consultation.held_status[ticket_id] = status
        except Exception:  # noqa: BLE001 — 占位消费者不改变会诊结论与值守循环
            return
