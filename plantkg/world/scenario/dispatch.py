"""直派器与业务控制状态的最简实现（B17）。

- DirectDispatcher：事件类型 → 专家 → apply，供 G-A 关口与独立组使用（B27 之前的最简派发层）。
  程序准入（severity 0 的噪声不开单）、同一实体同类事件合批、安全否决（安全标记或困人一律转操作工单）、
  硬约束拦截（洁净车间、数据机房等不能停的区域不执行停机类动作）、置信度分级（极低转人工、中等抽查）。
- TicketBook / TempRuleBook：事件单、操作工单与临时规则的最简账簿，实现 ForkHook（分叉时把未关闭的交给子运行）。
  协同组由值守层接管派发（agent/router/scenario.py 继承 DirectDispatcher），事件单以 RouterStore 为准，TicketBook 只作视图；
  B47（临时规则与工单）接入后由它的 ForkHook 替换。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import (
    DayEvent, ExpertProposal, Interval, ProposedAction, RunBinding, RunInfo, SimAction, TempRule, Ticket, TicketSubscription, WorkOrder,
)

from .coordinator import ceil_minute
from .experts import DEFAULT_ROUTES, SAFETY_EVENTS

SHED_COMMANDS = {("set_mode", "off"), ("stop", None), ("shed", None), ("set_setpoint", None), ("set_mode", "precool")}
PROTECTED_FLAGS = {"no_shed", "hard_climate", "no_power_cut"}
CONFLICT_PRONE = {"power_quality_dip", "unknown_combo", "equipment_fault"}  # 多位专家给根因判断的事件
RECORD_ONLY_COMMANDS = frozenset({"log_maintenance"})  # 台账动作（#168）：只记维护记录，不调 runner.apply


def is_record_only(action: ProposedAction) -> bool:
    return action.command in RECORD_ONLY_COMMANDS


@dataclass
class TicketState:
    ticket: Ticket
    event: dict[str, Any]  # 首个事件（DayEvent JSON）
    origin_event_at: datetime
    trigger_at: datetime
    entity_id: str | None
    pending: list[str] = field(default_factory=list)  # 等人的事项：work_order / correction / diagnosis

    def update(self, **kw) -> None:
        self.ticket = self.ticket.model_copy(update=kw)


class TicketBook:
    """事件单与操作工单（B27/B47 的替身）。分叉时（on_fork）未关闭的事件单与工单原样交给子运行。"""

    def __init__(self) -> None:
        self.tickets: dict[str, TicketState] = {}
        self.work_orders: dict[str, WorkOrder] = {}
        self.by_event: dict[str, str] = {}
        self.opened = 0  # 编号跨分叉连续，不复用

    def open(self, event: DayEvent, experts: list[str]) -> TicketState:
        self.opened += 1
        tid = f"TK-{self.opened:03d}"
        subs = (TicketSubscription(entity_id=event.entity_id, event_types=(event.event_type,)),) if event.entity_id else ()
        ticket = Ticket(ticket_id=tid, opened_at=event.at, status="open", round=0, event_ids=(event.event_id,), experts=tuple(experts),
                        subscriptions=subs, summary=event.summary)
        state = TicketState(ticket=ticket, event=event.model_dump(mode="json"), origin_event_at=event.at, trigger_at=event.at,
                            entity_id=event.entity_id)
        self.tickets[tid] = state
        self.by_event[event.event_id] = tid
        return state

    def subscriber(self, event: DayEvent) -> TicketState | None:
        """已接手的事件单订阅同一实体的同类事件：后续事件直接送达，不再研判。"""
        for st in self.tickets.values():
            # 已解决的事件单在 15 分钟内仍接收同一实体的同类告警（合批，不重复派发）
            if st.ticket.status == "closed" or (st.ticket.status == "resolved" and event.at - st.trigger_at > timedelta(minutes=15)):
                continue
            if any(s.entity_id == event.entity_id and event.event_type in s.event_types for s in st.ticket.subscriptions):
                return st
        return None

    def resolve_event(self, event_id: str) -> TicketState | None:
        """事件 Key 到事件单视图（#137）：未路由/噪声事件按事件单里的事件列表兜底，查不到返回 None。"""
        ticket_id = self.by_event.get(event_id)
        if ticket_id is not None:
            return self.tickets.get(ticket_id)
        for st in self.tickets.values():
            if event_id in st.ticket.event_ids:
                self.by_event.setdefault(event_id, st.ticket.ticket_id)
                return st
        return None

    def unfinished(self) -> list[TicketState]:
        return [st for st in self.tickets.values() if st.ticket.status not in ("resolved", "closed")]

    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        """事件单与工单整体交给子运行：已解决的仍可被确认、纠错与合批引用；重新派发只针对 unfinished()。"""


class TempRuleBook:
    """临时规则（B47 的替身）：到期自动失效，不进长期知识；分叉时仍生效的规则交给子运行。"""

    def __init__(self) -> None:
        self.rules: dict[str, TempRule] = {}

    def add(self, rule: TempRule) -> None:
        self.rules[rule.rule_id] = rule

    def active(self, at: datetime) -> list[TempRule]:
        return [r for r in self.rules.values() if r.effective.contains(at)]

    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        self.rules = {k: r for k, r in self.rules.items() if r.effective.end > at}


class DirectDispatcher:
    """事件 → 专家 → apply。runner 提供：当前运行、状态版本、知识、apply、记录与协调器。"""

    def __init__(self, runner: Any, routes: dict[str, list[str]] | None = None) -> None:
        self.runner = runner
        self.routes = DEFAULT_ROUTES | (routes or {})

    # ---- 准入与开单
    def admit(self, event: DayEvent) -> None:
        r = self.runner
        if event.severity == 0:
            r.note("admission", event.at, {"event_id": event.event_id, "result": "noise_dropped"})
            return
        sub = r.tickets.subscriber(event) if event.entity_id else None
        if sub is not None:  # 合批：并入已有事件单，不再派发
            sub.update(event_ids=(*sub.ticket.event_ids, event.event_id))
            r.tickets.by_event[event.event_id] = sub.ticket.ticket_id
            r.note("admission", event.at, {"event_id": event.event_id, "result": "merged", "ticket_id": sub.ticket.ticket_id})
            self.save(sub, event.at)
            return
        experts = self.routes.get(event.event_type, [])
        if not experts:
            r.note("admission", event.at, {"event_id": event.event_id, "result": "no_route"})
            return
        st = r.tickets.open(event, experts)
        self.dispatch(st, event.at)

    # ---- 派发一轮
    def dispatch(self, st: TicketState, trigger_at: datetime, *, reason: str = "event") -> None:
        st.update(round=st.ticket.round + 1, status="open")
        self.run_round(st, trigger_at, reason)

    def run_round(self, st: TicketState, trigger_at: datetime, reason: str) -> None:
        """跑事件单的当前轮：问各位专家、落提案、定状态。轮次由调用方推进。"""
        r = self.runner
        st.trigger_at = trigger_at
        st.pending = []
        for wid, wo in list(r.tickets.work_orders.items()):  # 上一轮未确认的工单随新一轮作废
            if wo.ticket_id == st.ticket.ticket_id and wo.status == "proposed":
                r.tickets.work_orders[wid] = wo.model_copy(update={"status": "expired"})
                r.save_work_order(r.tickets.work_orders[wid])
        blocked = False
        diagnoses: dict[str, str] = {}
        for expert in st.ticket.experts:
            binding = RunBinding(run_id=r.run_info.run_id, knowledge_version=r.knowledge_version, ticket_id=st.ticket.ticket_id,
                                 round=st.ticket.round, state_version=r.state.state_version, trigger_at=trigger_at)
            payload = {"event": st.event, "knowledge": sorted(r.knowledge), "state": r.brief(), "ids": r.ids,
                       "reason": reason}
            outcome = self.adopt(r.coordinator.request(expert, binding, payload, st.origin_event_at))
            r.record_agent_run(outcome.record)
            if outcome.error:
                blocked = True
                r.note("agent_error", trigger_at, {"ticket_id": st.ticket.ticket_id, "expert": expert,
                                                    "agent_run_id": outcome.record.agent_run_id, "error": outcome.error})
            if outcome.proposal is not None:
                if outcome.proposal.diagnosis and outcome.proposal.confidence >= 0.5:
                    diagnoses[outcome.proposal.proposal_id] = f"{expert}：{outcome.proposal.diagnosis}"
                self.deliver(outcome.proposal, st)
        if len(set(diagnoses.values())) > 1 and st.event["event_type"] in CONFLICT_PRONE:
            # 根因判断冲突：直派器不裁决，原样呈现分歧，交第 3 层（B28）或值班人员
            r.note("conflict", trigger_at, {"ticket_id": st.ticket.ticket_id, "proposal_ids": sorted(diagnoses), "diagnoses": sorted(diagnoses.values())})
            st.pending.append("arbitration")
        status = "blocked" if blocked else "waiting" if st.pending and any(p != "diagnosis" for p in st.pending) else \
            "open" if "diagnosis" in st.pending else "resolved"
        st.update(status=status, closed_at=None)
        self.save(st, trigger_at)

    def deliver(self, proposal: ExpertProposal, st: TicketState | None = None,
                protection: tuple[ProposedAction, ...] = ()) -> int:
        """把一份提案落到仿真里；迟到或过期的只入历史。返回执行的动作数。外部（B23 异步结果）也可直接调。

        protection 里的动作按预案保护动作落账（source.b47=protection），供撤销/取消时做二次确认。
        """
        r = self.runner
        b, timing = proposal.binding, proposal.timing
        st = st or r.tickets.tickets.get(proposal.ticket_id)
        r.record_proposal(proposal)
        effective = ceil_minute(timing.effective_at)
        stale = r.coordinator.stale_reason(b, run_id=r.run_info.run_id, round_=st.ticket.round if st else -1, state_version=r.state.state_version,
                                           tolerance=r.world.scenario.params.state_version_tolerance, now=r.now, effective_at=effective)
        expected = r.coordinator.effective_at(b.trigger_at)
        expected_layer3 = r.coordinator.effective_at(b.trigger_at, layer=3)
        if not stale and (timing.trigger_at != b.trigger_at or timing.effective_at not in (expected, expected_layer3)):
            stale = (f"生效时刻 {timing.effective_at.isoformat()} 不等于 trigger_at + "
                     f"响应预算 {expected.isoformat()}（第 1/2 层）或 {expected_layer3.isoformat()}（第 3 层）")
        if stale or st is None:
            r.note("history", r.now, {"proposal_id": proposal.proposal_id, "expert": proposal.expert, "reason": stale or "事件单不存在"})
            return 0
        policy = r.coordinator.policy
        if proposal.confidence < policy.confidence_low:
            st.pending.append("correction")
            r.note("to_human", effective, {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id,
                                           "confidence": proposal.confidence, "diagnosis": proposal.diagnosis})
            return 0
        if proposal.confidence < policy.confidence_high:
            r.note("sampling", effective, {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id, "confidence": proposal.confidence})
        safety = bool(proposal.safety_tags) or st.event["event_type"] in SAFETY_EVENTS
        human: list[ProposedAction] = []
        executed = 0
        for a in proposal.actions:
            zone_flags = set(r.world.zone_flags(a.entity_id))
            mode = a.params.get("mode")
            if ((a.command, mode) in SHED_COMMANDS or (a.command, None) in SHED_COMMANDS) and zone_flags & PROTECTED_FLAGS:
                r.note("rejected", effective, {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id, "entity_id": a.entity_id,
                                               "command": a.command, "reason": f"硬约束：区域标记 {sorted(zone_flags & PROTECTED_FLAGS)} 不能停或改设定"})
                continue
            if a.readiness == "needs_diagnosis":
                st.pending.append("diagnosis")
                continue
            if a.readiness == "needs_human" or safety or "safety" in zone_flags:
                human.append(a)  # 涉及安全或需人工：不自动执行，生成操作工单；确认只执行工单里批准的这些动作
                continue
            if is_record_only(a):  # 台账动作（#168）：只记维护记录，不当作设备动作下发
                r.note("maintenance_log", effective, {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id,
                                                       "entity_id": a.entity_id, "command": a.command, "params": a.params})
                executed += 1
                continue
            if self.already_applied(st.ticket.ticket_id, a):  # 分叉后重派：同一事件单已执行过的同一动作不重放
                r.note("skipped_duplicate", effective, {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id,
                                                        "entity_id": a.entity_id, "command": a.command})
                continue
            start = max(effective, ceil_minute(a.interval.start))
            source = {"ticket_id": st.ticket.ticket_id, "proposal_id": proposal.proposal_id, "expert": proposal.expert,
                      "round": st.ticket.round, "origin_event_at": st.origin_event_at.isoformat()}
            if a in protection:
                source["b47"] = "protection"
            r.apply(SimAction(entity_id=a.entity_id, command=a.command, params=a.params,
                              interval=Interval(start=start, end=start + (a.interval.end - a.interval.start))),
                    source=source)
            executed += 1
        if human:
            wo = WorkOrder(work_order_id=r.next_id("WO"), ticket_id=st.ticket.ticket_id, created_at=effective,
                           reason=proposal.diagnosis or "需要人工确认", actions=tuple(human), status="proposed")
            r.tickets.work_orders[wo.work_order_id] = wo
            r.save_work_order(wo)
            st.pending.append("work_order")
        return executed

    def already_applied(self, ticket_id: str, a: ProposedAction) -> bool:
        """当前运行链里（分叉时已撤掉未开始的）该事件单是否已有同对象、同命令、同参数的动作。"""
        return any(x["source"].get("ticket_id") == ticket_id and x["action"]["entity_id"] == a.entity_id
                   and x["action"]["command"] == a.command and x["action"]["params"] == a.params
                   for x in self.runner.state.sims["actions"])

    # ---- 值班人员确认
    def confirm(self, event_id: str, at: datetime) -> None:
        r = self.runner
        st = r.tickets.resolve_event(event_id)
        if st is None:  # 未路由/噪声事件没有事件单：确认不中止运行，只记一条忽略
            r.note("confirm_ignored", at, {"event_id": event_id, "reason": "事件未路由到事件单"})
            return
        tid = st.ticket.ticket_id
        proposed = [wid for wid, wo in r.tickets.work_orders.items() if wo.ticket_id == tid and wo.status == "proposed"]
        if not proposed:  # 重复确认或没有待确认工单：不再派发，避免动作重复执行
            r.note("confirm_ignored", at, {"ticket_id": tid, "event_id": event_id, "reason": "没有待确认的工单"})
            return
        # 确认时刻是本次执行的新 trigger_at，生效时刻按响应预算推后；工单里更晚的计划时间与动作时长保留
        st.trigger_at = at
        effective = r.coordinator.effective_at(at)
        start0 = ceil_minute(effective)
        for wid in proposed:  # 只执行工单里已批准的具体动作，不重新派发；执行前再过一次硬约束
            wo = r.tickets.work_orders[wid] = r.tickets.work_orders[wid].model_copy(update={"status": "confirmed"})
            r.save_work_order(wo)
            for a in wo.actions:
                if is_record_only(a):  # 台账动作（#168）：计量/传感器检修只落维护记录，不调 apply
                    r.note("maintenance_log", at, {"ticket_id": tid, "work_order_id": wid, "entity_id": a.entity_id,
                                                   "command": a.command, "params": a.params})
                    continue
                zone_flags = set(r.world.zone_flags(a.entity_id))
                mode = a.params.get("mode")
                if ((a.command, mode) in SHED_COMMANDS or (a.command, None) in SHED_COMMANDS) and zone_flags & PROTECTED_FLAGS:
                    r.note("rejected", at, {"ticket_id": tid, "work_order_id": wid, "entity_id": a.entity_id, "command": a.command,
                                            "reason": f"硬约束：区域标记 {sorted(zone_flags & PROTECTED_FLAGS)} 不能停或改设定"})
                    continue
                start = max(start0, ceil_minute(a.interval.start))
                r.apply(SimAction(entity_id=a.entity_id, command=a.command, params=a.params,
                                  interval=Interval(start=start, end=start + (a.interval.end - a.interval.start))),
                        source={"ticket_id": tid, "work_order_id": wid, "confirmed_at": at.isoformat(), "trigger_at": at.isoformat(),
                                "effective_at": effective.isoformat(),
                                "round": st.ticket.round, "origin_event_at": st.origin_event_at.isoformat()})
        st.pending = [p for p in st.pending if p != "work_order"]
        status = "waiting" if any(p != "diagnosis" for p in st.pending) else "open" if st.pending else "resolved"
        st.update(status=status, closed_at=None)
        self.save(st, at)

    def close(self, event_id: str, at: datetime, summary: str) -> None:
        r = self.runner
        st = r.tickets.resolve_event(event_id)
        if st is None:  # 未路由/噪声事件的纠错不中止运行
            r.note("correct_ignored", at, {"event_id": event_id, "reason": "事件未路由到事件单"})
            return
        st.update(status="closed", closed_at=at, summary=summary)
        self.save(st, at)

    # ---- 运行接缝：值守层接管派发时覆盖这几处
    def adopt(self, outcome: Any) -> Any:
        """专家结果进入运行记录之前的处理；直派器原样使用。"""
        return outcome

    def save(self, st: TicketState, at: datetime) -> None:
        self.runner.save_ticket(st, at)

    def step(self, now: datetime) -> None:
        """每个仿真分钟开始时调用；直派器没有定时工作。"""

    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        """直派器不持有运行绑定，事件单由 TicketBook 交接。"""

    def retract(self, ticket_ids: set[str], at: datetime) -> None:
        """父运行的在途结果作废：这些事件单尚未开始生效的动作撤掉，由子运行按新知识重新派发（trigger_at = forked_at）。"""
        state = self.runner.state
        state.sims["actions"] = [x for x in state.sims["actions"] if not (
            x["source"].get("ticket_id") in ticket_ids and SimAction.model_validate(x["action"]).interval.start >= at)]

    # ---- 人工干预（#245）：按 apply 时登记的动作编号取消或撤销；动作的生效区间就是它的“到期恢复”逻辑
    def applied(self, action_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """（运行记录里的动作记录, 仿真动作表里的条目）；取消后条目已摘掉，只剩记录。"""
        r = self.runner
        rec = next((x for x in r.trace.actions if x.get("action_id") == action_id), None)
        item = next((x for x in r.state.sims.get("actions", ()) if x.get("action_id") == action_id), None)
        return rec, item

    @staticmethod
    def action_stage(rec: dict[str, Any], now: datetime) -> str:
        """scheduled（下一分钟起才生效）/ active / ended / cancelled / revoked；与 active_actions 同一口径。"""
        if rec.get("intervention"):
            return rec["intervention"]["kind"]
        interval = rec["action"]["interval"]
        if datetime.fromisoformat(interval["end"]) <= now:
            return "ended"
        return "scheduled" if datetime.fromisoformat(interval["start"]) >= now else "active"

    def cancel_action(self, action_id: str, intervention: dict[str, Any]) -> dict[str, Any]:
        """取消还没生效的动作：从仿真动作表摘掉，与 retract 同一语义，只是按动作编号。"""
        rec, item = self.applied(action_id)
        if item is not None:
            self.runner.state.sims["actions"].remove(item)
        return self._intervene(rec, intervention, executed=False)

    def revoke_action(self, action_id: str, at: datetime, intervention: dict[str, Any]) -> dict[str, Any]:
        """撤销已生效的动作：把生效结束时刻提前到 at（当前分钟），下一分钟起设备回到没有这条动作的状态。"""
        rec, item = self.applied(action_id)
        for interval in ((item["action"]["interval"],) if item is not None else ()) + (rec["action"]["interval"],):
            interval["end"] = at.isoformat()
        return self._intervene(rec, intervention, executed=True)

    def _intervene(self, rec: dict[str, Any], intervention: dict[str, Any], *, executed: bool) -> dict[str, Any]:
        r = self.runner
        rec["intervention"] = intervention
        rec["executed"] = executed
        r.state.state_version += 1
        at = datetime.fromisoformat(intervention["at"])
        r.note(f"action_{intervention['kind']}", at, {"action_id": rec["action_id"], **intervention})
        r.db.store.write_records(r.run_info.run_id, [("action", rec["action_id"], datetime.fromisoformat(
            rec["action"]["interval"]["start"]), rec)])
        return rec

    def redispatch(self, at: datetime) -> None:
        """分叉后重新派发未关闭的事件单。"""
        r = self.runner
        unfinished = list(r.tickets.unfinished())
        self.retract({st.ticket.ticket_id for st in unfinished}, at)
        for st in unfinished:
            self.save(st, at)
            self.dispatch(st, at, reason="fork")


def temp_rule_from(payload: dict[str, Any], at: datetime, scenario: Any, index: Any, event_id: str, rule_id: str) -> tuple[TempRule, list[SimAction]]:
    """值班指令 → 临时规则 + 设备动作。payload: text, zone, setpoint_c, start, end（剧本时刻写法）。"""
    start, end = scenario.instant(payload["start"]), scenario.instant(payload["end"])
    zid = index.eid(f"zone:{payload['zone']}")
    rule = TempRule(rule_id=rule_id, text=payload["text"], effective=Interval(start=start, end=end),
                    entity_ids=(zid,), created_at=at, source_event_id=event_id)
    action = SimAction(entity_id=zid, command="set_setpoint", params={"setpoint_c": float(payload["setpoint_c"]), "temp_rule": rule.rule_id},
                       interval=Interval(start=start, end=end))
    return rule, [action]

