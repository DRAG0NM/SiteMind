"""B47 分级执行服务：执行策略、操作工单、临时规则、抽查/纠错队列、试探与协同处置。

接缝全部是注册表钩子（B27 的 urgent / instruction / probe / step / fork 与场景确认），不 import 值守层：
- 事件入口判为伤亡或重大损失 → 立即开操作工单、自动执行图谱预案里的保护动作，再把处置方案交给主 Agent；
- B28 裁决选中的动作经 router.execution 钩子进来，按“后果 × 自主等级 × 置信度”决定执行、抽查或等人批准；
- 人工确认后以确认时刻为新 trigger_at，经 TrialRunner 复核与幂等执行表执行，记录两段耗时；
- 值班指令转临时规则（带生效时段、试算代价），到期自动失效，分叉时交给子运行。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any, Iterable, Mapping, Sequence

from pydantic import ValidationError

from plantkg.core.contracts import (
    ActionReadiness, Actor, AgentResultKind, Context, DayEvent, DeliveryOutcome, ErrorCode, ExpertProposal, Interval,
    PendingActionKind, PlantkgError, ProbeProposal, ProposedAction, RunBinding, RunInfo, SimAction, TempRule,
    ToolResult, safe_error_details, tool_error,
)
from plantkg.core.ports import AgentResultSource, PendingActionStore, RunStore
from plantkg.world.scenario.dispatch import DirectDispatcher, is_record_only

from . import playbooks, policy
from .override import HumanOverride
from .records import ExecutionRecord, QueueItem, StoredTempRule, WorkOrderTask
from .store import ExecutionStore

log = logging.getLogger("plantkg.execution_policy")

# 钩子名（与 B27/B17 约定一致；调用方登记，本包不 import 值守层）
ROUTER_HOOK = "router.service"
URGENT_HOOK = "router.urgent"
INSTRUCTION_HOOK = "router.instruction"
PROBE_HOOK = "router.probe"
STEP_HOOK = "router.step"
EXECUTION_HOOK = "router.execution"
SCENARIO_HOOK = "scenario_plugin"
SCENARIO_BIND_HOOK = "router.scenario_bind"
SCENARIO_CONFIRM_HOOK = "scenario.confirm"
FORK_HOOK = "scenario_fork"
TICKET_WRITEBACK_HOOK = "router.ticket_writeback"  # B27 登记：无绑定拒绝时按 run 作用域兜底把事件单置 blocked

# 值班指令的确定性解析（中文口语，configured/synthetic）
_ZONE_WORDS = (("行政楼", "admin"), ("办公", "admin"), ("车间", "production"), ("洁净", "cleanroom"),
               ("危化", "hazmat"), ("机房", "datacenter"))
_SETPOINT = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:℃|度)")
_WINDOW = re.compile(r"(\d{1,2}:\d{2})\s*[～~\-至到]\s*(\d{1,2}:\d{2})")
_SCRIPT_HHMM = re.compile(r"^\s*(?:\+(\d+)\s+)?(\d{1,2}):(\d{2})\s*$")


def _ceil_minute(at: datetime) -> datetime:
    floor = at.replace(second=0, microsecond=0)
    return floor if at == floor else floor + timedelta(minutes=1)


def _fingerprint(action: ProposedAction) -> tuple[str, str, str]:
    return action.entity_id, action.command, repr(sorted(action.params.items()))


def _sim(action: ProposedAction, start: datetime | None = None) -> SimAction:
    length = action.interval.end - action.interval.start
    start = start or action.interval.start
    return SimAction(entity_id=action.entity_id, command=action.command, params=dict(action.params),
                     interval=Interval(start=start, end=start + length))


def _script_time(text: str, start_day: int = 0) -> tuple[int, int, int]:
    """剧本时刻写法 → (日偏移, 时, 分)；24:00 归一为次日 00:00，其余时刻必须在 0-24 时内。"""
    match = _SCRIPT_HHMM.match(str(text))
    if match is None:
        raise ValueError(f"时刻 {text!r} 不是 HH:MM 或 +N HH:MM")
    day = int(match.group(1)) if match.group(1) is not None else start_day
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour == 24 and minute == 0:
        return day + 1, 0, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时刻 {text!r} 不在 0-24 时内")
    return day, hour, minute


def _script_text(day: int, hour: int, minute: int) -> str:
    clock = f"{hour:02d}:{minute:02d}"
    return f"+{day} {clock}" if day else clock


class ExecutionPolicyService:
    def __init__(self, registry: Any, store: ExecutionStore | None, *, plant_id: str = "", protections: bool = True) -> None:
        self.registry, self.store, self.plant_id = registry, store, plant_id
        self.protections = protections
        self._bindings: dict[int, tuple[Any, Any]] = {}  # id(router) -> (router, runner | None)
        self.override = HumanOverride(self)  # 暂停自动执行、取消与撤销（#245）

    def ensure_store(self, writer: Any, plant_id: str) -> None:
        """场景注册表在第一次绑定时才有运行库：那时才建执行表，之前不落任何执行记录。"""
        if self.store is None:
            self.store = ExecutionStore(writer, plant_id)
            self.plant_id = plant_id

    # ---------------------------------------------------------------- 装配与发现
    def bind(self, router: Any, runner: Any = None) -> None:
        if runner is not None and self.store is None:
            db = getattr(runner, "db", None)
            if db is not None:
                self.ensure_store(db.writer, db.plant_id)
        self._bindings[id(router)] = (router, runner)

    def _routers(self) -> list[Any]:
        found, seen = [], set()
        for router in (*self.registry.hooks_for(ROUTER_HOOK), *(r for r, _ in self._bindings.values())):
            if router is not None and id(router) not in seen and hasattr(router, "store"):
                seen.add(id(router))
                found.append(router)
        return found

    def _router_for_event(self, event: DayEvent) -> Any | None:
        for router in self._routers():
            try:
                for ticket in router.store.open_tickets():
                    if event.event_id in ticket.event_ids:
                        return router
            except Exception:  # noqa: BLE001 — 某个运行的表不可读不挡其他运行
                continue
        return None

    def _ticket_for_event(self, router: Any, event: DayEvent) -> Any | None:
        try:
            return next((t for t in router.store.open_tickets() if event.event_id in t.event_ids), None)
        except Exception:  # noqa: BLE001
            return None

    def _router_for_ticket(self, ticket_id: str) -> Any | None:
        for router in self._routers():
            try:
                if router.store.ticket(ticket_id) is not None:
                    return router
            except Exception:  # noqa: BLE001
                continue
        return None

    def _runner_for(self, router: Any) -> Any | None:
        bound = self._bindings.get(id(router))
        return bound[1] if bound else None

    @staticmethod
    def _ticket_event(router: Any, ticket: Any) -> DayEvent | None:
        if ticket is None or not ticket.event_ids:
            return None
        found = router.store.event(ticket.event_ids[-1])
        return found["event"] if found else None

    # ---------------------------------------------------------------- 事件入口：伤亡 / 重大损失
    def on_urgent(self, event: DayEvent, reason: str) -> None:
        """D55：立即开操作工单，同时自动执行预案保护动作；后续处置交主 Agent 准备、人在工单里决定。"""
        if self.store is None:
            return
        router = self._router_for_event(event)
        ticket = self._ticket_for_event(router, event) if router is not None else None
        if router is None or ticket is None:
            return
        runner = self._runner_for(router)
        round_ = max(ticket.round, 1)
        denied = self._denied_actions(event)
        code = (event.payload or {}).get("reason_code")  # B27（#170）的结构化来源码；旧事件没有时按下文文案兜底
        severity = code == "d55" if code else self._severity_reason(reason)  # D55 入口：安全标记、停产或超重大损失线
        serious = severity or code == "safety_veto"  # 安全标记入口仍要按预案开单并交专家处置
        self._expire_older(router, runner, ticket.ticket_id, round_)
        existing = self.store.pending_task(ticket.ticket_id, router.run_id)
        if existing is not None and existing.kind == "work_order" and existing.round >= round_:
            if denied:
                self._append_actions(existing, denied)
            return
        playbook = playbooks.playbook_for(router.registry, event) if serious else None
        playbook_id = playbook.get("id") if playbook else None
        start = self._effective(event.at, router.policy)
        protection, disposal, skipped = playbooks.plan(playbook, event, runner, start) if playbook else ((), (), ())
        for note in skipped:  # 预案模板里的实体解析不到：跳过该动作并留记录
            self._note(runner, "b47_playbook_skipped", event.at, note)
        actions = self._dedupe((*denied, *disposal))
        if severity:
            basis, origin = f"D55 伤亡或重大损失：{reason}", "d55"
        elif code == "safety_veto" or reason == "action_safety":
            basis, origin = f"安全否决：{reason}；只把被否决的动作交人工复核", "safety_veto"
        else:
            basis, origin = f"升级：{reason}", "escalation"
        task = WorkOrderTask(
            work_order_id=self.store.next_id("WO"), ticket_id=ticket.ticket_id, run_id=router.run_id, round=round_,
            created_at=event.at,
            reason=f"{reason}；预案：{playbook.get('label', '无') if playbook else '无'}",
            kind="work_order", origin=origin, playbook_id=playbook_id, actions=actions,
            basis=basis + (f"；被否决动作 {len(denied)} 个" if denied else ""))
        self.store.save_task(task)
        self._propose_pending(task, PendingActionKind.work_order)
        self._mirror_task(runner, task)
        self._hold_ticket(router, runner, ticket.ticket_id, "work_order")
        if runner is not None and self.protections and protection:
            if self._protection_decision(router, event, protection):
                try:  # 保护动作失败不挡工单与值守循环；失败原因已进执行表
                    self._execute_protection(runner, ticket, event, playbook_id, protection)
                except Exception:  # noqa: BLE001
                    log.exception("预案保护动作执行失败：%s", playbook_id)
            else:
                self._record_protection_deferred(runner, ticket, event, playbook_id, protection)
        if not denied and ticket.round == 0 and serious:  # 事件入口的第一次 urgent：让主 Agent 继续准备处置方案
            try:  # 本钩子在 B27 记录事件之前调用：先补齐事件行，专家运行才拿得到本轮事件
                router.store.record_event(event, "urgent", ticket.ticket_id)
                experts = self._urgent_experts(router, runner, event)
                if experts:
                    ticket = ticket.model_copy(update={"experts": tuple(experts)})
                    router.store.save_ticket(ticket, run_id=router.run_id)
                    router._dispatch(ticket, event.at, reason="urgent")
            except Exception:  # noqa: BLE001 — 派发失败不挡工单；事件单会由值守层标状态
                log.exception("urgent 事件单 %s 派发专家失败", ticket.ticket_id)

    @staticmethod
    def _severity_reason(reason: str) -> bool:
        """区分 D55 严重程度入口与其它升级原因（诊断未决、组合不可行、Agent 升级、安全否决）。"""
        return (reason.startswith("安全标记") or reason.startswith("未保护时会停产")
                or "超过重大损失线" in reason or reason.startswith("试算损失"))

    def _protection_decision(self, router: Any, event: DayEvent, actions: Sequence[SimAction]) -> bool:
        _, credibility_note = self._credibility(router, event)
        payload = event.payload or {}
        reading = payload.get("reading") or payload
        quality = str(reading.get("quality", "ok")).lower()
        suspect = (credibility_note == "读数可疑" or bool(reading.get("residual_alarm"))
                   or quality not in ("ok", "good", "okay", "unavailable"))
        if not suspect:
            return True
        answer = self._ask_protection_agent(router, event, actions)
        return answer is not False

    def _ask_protection_agent(self, router: Any, event: DayEvent, actions: Sequence[SimAction]) -> bool | None:
        source = router.registry.mode_ports.get((AgentResultSource, "live", "investigate"))
        if source is None:
            return None
        ticket = self._ticket_for_event(router, event)
        if ticket is None:
            return None
        binding = RunBinding(run_id=router.run_id, knowledge_version=router.knowledge_version,
                             ticket_id=ticket.ticket_id, round=max(ticket.round, 1),
                             state_version=router._state_version(), trigger_at=event.at)
        query = (f"事件 {event.event_id} 的关键读数有疑点。请仅判断是否立即执行以下预案保护动作；"
                 "保护动作可立即执行或 defer。只输出 JSON：{\"protection_decision\":\"execute\"|\"defer\","
                 "\"reason\":\"一句话依据\"}。\n事件："
                 f"{event.model_dump_json()}\n保护动作：{json.dumps([a.model_dump(mode='json') for a in actions], ensure_ascii=False)}")
        payload = {"query": query, "as_of": event.at.isoformat(), "event": event.model_dump(mode="json"),
                   "protection_actions": [a.model_dump(mode="json") for a in actions]}
        budget = getattr(getattr(router.policy, "response_budget", None), "layer12_min", 1)
        completed = Event()
        result: dict[str, Any] = {}

        def ask() -> None:
            try:
                result["value"] = source.get(AgentResultKind.investigate, binding, payload)
            except Exception as exc:  # 无结论到期按任务卡默认执行
                result["error"] = exc
            finally:
                completed.set()

        Thread(target=ask, daemon=True).start()
        try:
            has_result = completed.wait(timeout=max(0.0, float(budget) * 60))
        except (OverflowError, TypeError, ValueError):
            return None
        if not has_result or "error" in result:
            return None
        response = result["value"]
        if not isinstance(response, Mapping):
            return None
        answer = response.get("protection_decision")
        if answer is None and response.get("status") == "finished":
            try:
                answer = json.loads(str(response.get("final_answer") or "")).get("protection_decision")
            except (json.JSONDecodeError, AttributeError, TypeError):
                return None
        return answer == "execute" if answer in ("execute", "defer") else None

    def _record_protection_deferred(self, runner: Any, ticket: Any, event: DayEvent, playbook_id: str,
                                    actions: Sequence[SimAction]) -> None:
        for index, action in enumerate(actions):
            key = f"protection#{ticket.ticket_id}#{playbook_id}#{index}#r{max(ticket.round, 1)}"
            self.store.save_execution(ExecutionRecord(
                key=key, run_id=runner.run_info.run_id, ticket_id=ticket.ticket_id, round=max(ticket.round, 1),
                action_index=index, entity_id=action.entity_id, command=action.command, status="rejected", at=event.at,
                detail="读数可疑，Agent 判断暂缓预案保护动作"))

    @staticmethod
    def _urgent_experts(router: Any, runner: Any, event: DayEvent) -> list[str]:
        """urgent 事件也要有专家准备处置方案：优先场景路由表，其次事件类型默认领域，最后图谱定位。"""
        routes = getattr(getattr(runner, "dispatcher", None), "routes", None) or {}
        found = list(routes.get(event.event_type, ()))
        if not found:
            from plantkg.agent.router.router import EVENT_DOMAINS  # 延迟导入：装配期不依赖值守层

            found = list(EVENT_DOMAINS.get(event.event_type, ()))
        if not found:
            for hook in reversed(router.registry.hooks_for("router.locate")):
                try:
                    found = list(hook(event) or ())
                except Exception:  # noqa: BLE001
                    found = []
                if found:
                    break
        return found

    @staticmethod
    def _denied_actions(event: DayEvent) -> tuple[ProposedAction, ...]:
        raw = (event.payload or {}).get("denied_actions") or ()
        out = []
        for item in raw:
            try:
                out.append(ProposedAction.model_validate(item))
            except ValueError:  # 坏数据不进工单
                continue
        return tuple(out)

    def _append_actions(self, task: WorkOrderTask, actions: Iterable[ProposedAction]) -> None:
        merged = self._dedupe((*task.actions, *actions))
        if merged != task.actions:
            saved = self.store.save_task(task.model_copy(update={"actions": merged}))
            router = self._router_for_ticket(saved.ticket_id)
            self._mirror_task(self._runner_for(router) if router is not None else None, saved)

    def _execute_protection(self, runner: Any, ticket: Any, event: DayEvent, playbook_id: str | None,
                            actions: Sequence[SimAction]) -> int:
        executed = 0
        paused = self.store.active_pause(runner.run_info.run_id) is not None
        if paused:  # 暂停自动执行期间保护动作照常执行（#245）
            self._note(runner, "autonomy_protection_continues", event.at,
                       {"ticket_id": ticket.ticket_id, "playbook_id": playbook_id, "note": "暂停期间保护动作照常执行"})
        for index, action in enumerate(actions):
            key = f"protection#{ticket.ticket_id}#{playbook_id}#{index}#r{max(ticket.round, 1)}"
            record = ExecutionRecord(key=key, run_id=runner.run_info.run_id, ticket_id=ticket.ticket_id, round=max(ticket.round, 1),
                                     action_index=index, entity_id=action.entity_id, command=action.command, at=event.at,
                                     detail="预案保护动作（暂停期间保护动作照常执行）" if paused else "预案保护动作")
            if not self.store.claim_execution(record):
                continue
            try:  # 保护动作来自已审核的图谱预案（configured），不再逐条过动作级安全标记
                runner.apply(action, source={"b47": "protection", "playbook": playbook_id, "ticket_id": ticket.ticket_id,
                                             "event_id": event.event_id})
            except PlantkgError as exc:
                self.store.save_execution(record.model_copy(update={"status": "infeasible", "detail": str(exc)[:200]}))
                continue
            self.store.save_execution(record)
            executed += 1
        return executed

    # ---------------------------------------------------------------- B28 裁决动作入口
    def execute(self, runner: Any, proposal: ExpertProposal, state: Any = None) -> DeliveryOutcome:
        """router.execution：按策略决定每个动作的去向；执行仍走 B17 直派器的 apply 路径。

        返回执行回执（#168）：B28 只按回执映射裁决结果，不 import 本包、也不再把“排队/退回/迟到”写成 executed。
        """
        indexes = tuple(range(len(proposal.actions)))
        if self.store is None:
            return self._direct_receipt(runner, proposal, state, "分级执行未装配执行表，按直派器结果",
                                        indexes, stale_when_empty=False)
        router = getattr(getattr(runner, "dispatcher", None), "router", None) or self._router_for_ticket(proposal.ticket_id)
        if router is None or proposal.binding is None:
            return self._direct_receipt(runner, proposal, state, "没有绑定，按直派器结果", indexes, stale_when_empty=True)
        ticket = router.store.ticket(proposal.ticket_id)
        event = self._ticket_event(router, ticket)
        if ticket is None or event is None:
            return self._direct_receipt(runner, proposal, state, "事件单不存在，按直派器结果", indexes, stale_when_empty=True)
        round_ = proposal.binding.round
        if round_ != ticket.round:  # 迟到轮次只入历史，不执行、不回写（B46 绑定口径）
            reason = f"事件单已到第 {ticket.round} 轮，本轮是第 {round_} 轮"
            try:
                runner.note("history", runner.now, {
                    "proposal_id": proposal.proposal_id, "expert": proposal.expert, "reason": reason})
            except Exception:  # noqa: BLE001
                pass
            return DeliveryOutcome(outcome="stale", reason=reason, action_indexes=indexes)
        stale = self._stale_reason(runner, proposal, state, ticket)
        if stale is not None:  # 绑定/时序不符：只入历史，不做策略判定也不落执行表
            try:
                runner.note("history", runner.now, {"proposal_id": proposal.proposal_id, "expert": proposal.expert,
                                                    "reason": stale})
            except Exception:  # noqa: BLE001
                pass
            return DeliveryOutcome(outcome="stale", reason=stale, action_indexes=indexes)
        self._expire_older(router, runner, ticket.ticket_id, round_)

        run_id = proposal.binding.run_id
        pending = self.store.pending_task(ticket.ticket_id, run_id)
        # D55 的伤亡/重大损失事件整单走工单；安全否决等升级只把被否决动作交工单，无关动作照常按矩阵执行
        d55 = (router.store.channel_of(ticket.ticket_id) == "urgent" and pending is not None
               and pending.origin == "d55")
        event_type = event.event_type
        entity_type = self._entity_type(router, event.entity_id)
        category = self.store.category(event_type, entity_type)
        known = self.store.level(category)
        level = self._effective_level(router, category)
        bound_policy = self._bound_policy(router)
        # An authorization for a correction is not authority for a different event.
        if event_type == 'unknown_combo':
            matcher = router.rule_matcher
            if (not hasattr(matcher, 'authorizes_for_run')
                    or not matcher.authorizes_for_run(event, router.run_id, proposal.actions)):
                level = 'L0'
        if known is None:
            self.store.mark_seen(category, event.at, level)

        jev_ok = True
        triage = router.store.triage(ticket.ticket_id, round_)
        if triage is not None:
            jev_ok = bool(triage.get("gate_accept", True))
        credibility, credibility_note = self._credibility(router, event)
        trial = self._trial(router, [_sim(a) for a in proposal.actions if a.readiness != ActionReadiness.needs_human],
                            self._horizon(proposal.actions))
        hard = [violation for violation in (trial.violations if trial is not None else ()) if violation.kind != "safety"]
        feasible = not hard  # 安全违规不在这里判不可行：动作会进操作工单由人确认
        confidence, parts = policy.compose_confidence(proposal, jev_consistent=jev_ok, trial_feasible=feasible,
                                                      credibility=credibility)
        band = policy.confidence_band(confidence, bound_policy)

        protection = self._protection_scope(router, runner, event, ticket) if d55 else ()
        allowed: list[ProposedAction] = []
        protected_actions: list[ProposedAction] = []  # 暂停期间按预案模板照常执行的动作，落 source.b47=protection
        to_task: list[ProposedAction] = []
        queue_items: list[tuple[str, ProposedAction, int, str]] = []
        if hard:
            detail = f"执行前复核不可行：{hard[0].detail}"
            for index, action in enumerate(proposal.actions):
                self._record(proposal, ticket, round_, index, action, "infeasible", detail)
            self._note(runner, "b47_infeasible", event.at,
                       {"ticket_id": ticket.ticket_id, "proposal_id": proposal.proposal_id, "detail": "退回 B28"})
            return DeliveryOutcome(outcome="blocked_infeasible", reason=detail, action_indexes=indexes)

        correction = False
        considered = 0
        paused = self.store.active_pause(run_id)  # 暂停自动执行（#245）：非保护动作一律压到只提议
        paused_actions: list[tuple[int, ProposedAction]] = []
        for index, action in enumerate(proposal.actions):
            if self.store.execution(run_id, self._execution_key(proposal, index, round_, ticket.ticket_id)) is not None:
                continue  # 幂等：同一（提案, 动作序号, 轮次）重复提交直接忽略
            considered += 1
            if action.readiness == ActionReadiness.needs_human:
                to_task.append(action)
                self._record(proposal, ticket, round_, index, action, "queued", "needs_human：人工待办")
                continue
            if d55 and self._is_protection(action, protection):
                self._record(proposal, ticket, round_, index, action, "skipped_duplicate", "与预案保护动作重复")
                continue
            if d55:  # 重大损失事件：处置方案进工单，不越过工单流程（D55）
                to_task.append(action)
                self._record(proposal, ticket, round_, index, action, "queued", "urgent：进操作工单处置方案")
                continue
            if self._safety_veto(router, action):
                to_task.append(action)
                self._record(proposal, ticket, round_, index, action, "queued", "安全标记：进操作工单")
                continue
            consequence = self._consequence(router, action.command)
            decision = policy.decide(level, consequence, confidence, bound_policy)
            if decision.outcome == policy.CORRECTION:
                correction = True
                self._record(proposal, ticket, round_, index, action, "rejected", decision.reason)
                continue
            if paused is not None and decision.outcome in (policy.EXECUTE, policy.EXECUTE_SAMPLING, policy.EXECUTE_REVIEW):
                if self._is_template_protection(router, runner, event, action):
                    protected_actions.append(action)
                    self._note(runner, "autonomy_protection_continues", event.at,
                               {"ticket_id": ticket.ticket_id, "entity_id": action.entity_id, "command": action.command,
                                "note": "暂停期间保护动作照常执行"})
                else:
                    paused_actions.append((index, action))
                    self._record(proposal, ticket, round_, index, action, "proposed",
                                 f"暂停自动执行（{paused['paused_by']}）：只提议，等人批准；原判定：{decision.reason}")
                    continue
            if decision.outcome in (policy.EXECUTE, policy.EXECUTE_SAMPLING, policy.EXECUTE_REVIEW):
                allowed.append(action)
                kind = policy.queue_kind(decision.outcome)
                if kind is not None:
                    queue_items.append((kind, action, index, decision.reason))
                continue
            to_task.append(action)
            self._record(proposal, ticket, round_, index, action, "proposed", decision.reason)

        executed = 0
        if allowed:
            sub = proposal.model_copy(update={"actions": tuple(allowed), "confidence": confidence})
            executed = DirectDispatcher.deliver(runner.dispatcher, sub, state,
                                                protection=tuple(protected_actions))
            for index, action in enumerate(proposal.actions):
                if action in allowed:
                    status = "executed" if executed else "skipped_duplicate"
                    self._record(proposal, ticket, round_, index, action, status,
                                 f"置信度 {confidence:.2f}（{band}，{credibility_note}）")
            for kind, action, index, reason in queue_items:
                self._queue(kind, ticket, round_, proposal, action, index,
                            reason=f"{reason}；置信度 {confidence:.2f}")
        task = None
        if to_task:
            task = self._queue_task(router, runner, ticket, round_, to_task, proposal, level, band, confidence)
        paused_tasks = [self._queue_paused_task(router, runner, ticket, round_, action, proposal, level, band,
                                                confidence, paused) for _, action in paused_actions]
        if not allowed and not to_task and not paused_tasks and correction:
            self._queue("correction", ticket, round_, proposal, proposal.actions[0], 0,
                        reason=f"置信度 {confidence:.2f} 低于 {bound_policy.confidence_low}：不执行，转人工纠错")
            self._hold_ticket(router, runner, ticket.ticket_id, "correction")
        self._note(runner, "b47_execution", event.at,
                   {"ticket_id": ticket.ticket_id, "proposal_id": proposal.proposal_id, "level": level,
                    "confidence": confidence, "parts": parts, "executed": executed, "considered": considered,
                    "queued": len(to_task), "paused": len(paused_tasks), "bases": [item[0] for item in queue_items]})
        queued = {index for index, action in enumerate(proposal.actions) if action in to_task} | {i for i, _ in paused_actions}
        if to_task or paused_tasks:
            ids = tuple(item.work_order_id for item in (*([task] if task is not None else []), *paused_tasks))
            reason = "动作交操作工单/人工待办等人确认" if to_task else "暂停自动执行：动作只提议，等人逐条批准"
            return DeliveryOutcome(outcome="queued_work_order" if task is not None and task.kind == "work_order" else "queued_human",
                                   reason=reason, work_order_ids=ids, action_indexes=tuple(sorted(queued)))
        if executed:
            return DeliveryOutcome(outcome="executed", reason=f"执行 {executed} 个动作",
                                   action_indexes=tuple(index for index, action in enumerate(proposal.actions) if action in allowed))
        if correction:
            return DeliveryOutcome(outcome="correction",
                                   reason=f"置信度 {confidence:.2f} 低于 {router.policy.confidence_low}：转人工纠错",
                                   action_indexes=indexes)
        return DeliveryOutcome(outcome="executed", reason="没有需要执行的动作",
                               action_indexes=())

    def _is_template_protection(self, router: Any, runner: Any, event: DayEvent, action: ProposedAction) -> bool:
        """保护动作按预案模板的 protection 标记判定（#245）：与本事件预案里的某条保护动作同对象、同命令、同参数。"""
        playbook = playbooks.playbook_for(router.registry, event)
        if not playbook:
            return False
        protection, _, _ = playbooks.plan(playbook, event, runner, self._effective(event.at, router.policy))
        return any(item.entity_id == action.entity_id and item.command == action.command
                   and dict(item.params) == dict(action.params) for item in protection)

    def _queue_paused_task(self, router: Any, runner: Any, ticket: Any, round_: int, action: ProposedAction,
                           proposal: ExpertProposal, level: str, band: str, confidence: float,
                           paused: Mapping[str, Any]) -> WorkOrderTask:
        """暂停期间被压成只提议的动作：一条一张人工待办，恢复后仍逐条批准，不并入其他工单。"""
        task = WorkOrderTask(
            work_order_id=self.store.next_id("HD"), ticket_id=ticket.ticket_id, run_id=router.run_id, round=round_,
            created_at=proposal.binding.trigger_at if proposal.binding is not None else datetime.now().astimezone(),
            reason=f"人工待办：暂停自动执行期间只提议（{paused['paused_by']}：{paused['reason']}）",
            kind="human_decision", origin="manual", actions=(action,), autonomy_paused=True,
            basis=f"自主等级 {level}、置信度 {confidence:.2f}（{band}）：暂停自动执行，出方案等人批准")
        self.store.save_task(task)
        self._propose_pending(task, PendingActionKind.human_decision)
        self._mirror_task(runner, task)
        self._hold_ticket(router, runner, ticket.ticket_id, "work_order")
        return self.store.task(task.work_order_id) or task

    def _direct_receipt(self, runner: Any, proposal: ExpertProposal, state: Any, reason: str,
                        indexes: tuple[int, ...], *, stale_when_empty: bool) -> DeliveryOutcome:
        """直派器兜底：按实际执行数出回执；0 个时按原因记 blocked_infeasible/stale（#204-4b）。"""
        executed = DirectDispatcher.deliver(runner.dispatcher, proposal, state)
        if executed:
            # 直派器只回执行计数，部分执行时无法确定具体序号；沿用既有口径给本次提案的全部序号
            return DeliveryOutcome(outcome="executed", reason=f"{reason}，直派器执行 {executed} 个动作",
                                   action_indexes=indexes)
        return DeliveryOutcome(outcome="stale" if stale_when_empty else "blocked_infeasible",
                               reason=f"{reason}，直派器没有执行动作", action_indexes=())

    def _queue_task(self, router: Any, runner: Any, ticket: Any, round_: int,
                    actions: Sequence[ProposedAction], proposal: ExpertProposal, level: str, band: str,
                    confidence: float) -> WorkOrderTask:
        task = self.store.pending_task(ticket.ticket_id, router.run_id)
        if task is not None and (task.round == round_ or task.origin == "d55"):
            # 同一轮已有工单/待办：动作并入，不重复开单；D55 整单等人工，后续轮次也并进原工单（#170）
            merged = self._dedupe((*task.actions, *actions))
            if merged != task.actions:
                task = self.store.save_task(task.model_copy(update={"actions": merged}))
                self._mirror_task(runner, task)
            self._hold_ticket(router, runner, ticket.ticket_id, "work_order")
            return task
        task = WorkOrderTask(
            work_order_id=self.store.next_id("HD"), ticket_id=ticket.ticket_id, run_id=router.run_id, round=round_,
            created_at=proposal.binding.trigger_at if proposal.binding is not None else datetime.now().astimezone(),
            reason=f"人工待办：{proposal.diagnosis or '动作需人批准'}",
            kind="human_decision", origin="manual", actions=self._dedupe(actions),
            basis=f"自主等级 {level}、置信度 {confidence:.2f}（{band}）：出方案等人批准")
        self.store.save_task(task)
        self._propose_pending(task, PendingActionKind.human_decision)
        self._mirror_task(runner, task)
        self._hold_ticket(router, runner, ticket.ticket_id, "work_order")
        return task

    # ---------------------------------------------------------------- 人工确认与拒绝
    def confirm_from_scenario(self, runner: Any, at: datetime, event_id: str, event: DayEvent | None = None) -> bool:
        router = getattr(getattr(runner, "dispatcher", None), "router", None)
        if router is None or self.store is None:
            return False
        ticket_id = (getattr(runner, "tickets", None) and runner.tickets.by_event.get(event_id)) or \
            router.store.event_ticket(event_id)
        if ticket_id is None:
            return False
        tasks = [t for t in self.store.tasks(ticket_id, "proposed")
                 if t.run_id == runner.run_info.run_id and t.kind in ("work_order", "human_decision")]
        if not tasks:
            return False
        for task in tasks:
            self._confirm_task(router, runner, task, at)
            self._cancel_pending(task, "已按剧本确认路径执行，统一确认端点不再重复执行")
        return True

    def confirm(self, work_order_id: str, at: datetime) -> bool:
        """按工单编号确认（演示与测试用）：找到绑定运行后执行。"""
        task = self.store.task(work_order_id) if self.store is not None else None
        if task is None:
            return False
        runner = self._runner_for_run(task.run_id)
        router = self._router_for_run(task.run_id)
        if runner is None or router is None:
            return False
        self._confirm_task(router, runner, task, at)
        self._cancel_pending(task, "已按工单编号确认执行，统一确认端点不再重复执行")
        return True

    def reject(self, work_order_id: str, at: datetime, reason: str = "") -> bool:
        return self._reject(work_order_id, reason) == "ok"

    def _reject(self, work_order_id: str, reason: str, *, cancel_pending: bool = True) -> str:
        """拒绝工单：返回 ok / conflict（任务已不是待确认）/ writeback_failed（事件单 blocked 回写失败，任务不变可重试）。

        统一端点自己会把待确认动作标 rejected，它调用时传 cancel_pending=False，免得这里再关一次待确认动作。
        """
        task = self.store.task(work_order_id) if self.store is not None else None
        if task is None:
            return "conflict"
        if task.status != "proposed":  # 重复拒绝幂等忽略；已完成/已过期的工单不能再驳回
            return "ok" if task.status == "rejected" else "conflict"
        # 事件单回写成功后才改任务：回写失败时任务仍 proposed，调用方可重试（#240 复审 P1）
        if not self._persist_ticket_blocked(task):
            return "writeback_failed"
        task = task.model_copy(update={"status": "rejected", "basis": f"{task.basis}；拒绝：{reason}"})
        self.store.save_task(task)
        if cancel_pending:
            self._cancel_pending(task, f"工单已拒绝：{reason}")
        runner = self._runner_for_run(task.run_id)
        if runner is not None:
            self._mirror_task(runner, task)
        self._queue("correction", None, 0, None, None, 0,
                    reason=f"人工拒绝操作工单 {work_order_id}：{reason}", ticket_id=task.ticket_id, run_id=task.run_id)
        return "ok"

    def _persist_ticket_blocked(self, task: WorkOrderTask) -> bool:
        """拒绝的事件单回写：有 Router 时走 _hold_ticket，未绑定时按任务 run 作用域直接落 B27（#240 复审 P1）。"""
        router = self._router_for_run(task.run_id)
        if router is not None:
            runner = self._runner_for_run(task.run_id)
            self._hold_ticket(router, runner, task.ticket_id, "correction", status="blocked")
            ticket = router.store.ticket(task.ticket_id)
            if ticket is not None and ticket.status == "blocked":
                return True
        hooks = self.registry.hooks_for(TICKET_WRITEBACK_HOOK) if self.registry is not None else ()
        if not hooks:
            log.warning("事件单 %s 的 blocked 回写没有 B27 兜底（任务 %s）", task.ticket_id, task.work_order_id)
            return False
        try:
            return bool(hooks[-1](task.ticket_id, task.run_id))
        except Exception:  # noqa: BLE001 — 兜底失败按未生效处理，任务不改状态，可重试
            log.exception("事件单 %s 的 blocked 兜底回写失败（任务 %s）", task.ticket_id, task.work_order_id)
            return False

    def _confirm_task(self, router: Any, runner: Any, task: WorkOrderTask, at: datetime) -> DeliveryOutcome:
        if task.status != "proposed":
            return DeliveryOutcome(outcome="executed", reason="操作工单已处理，未重复执行")
        effective = self._effective(at, router.policy)
        # 台账动作（#168）不是仿真动作：不试算、不下发，确认后只登记维护记录
        sims = [_sim(action, max(effective, action.interval.start)) for action in task.actions if not is_record_only(action)]
        horizon = (Interval(start=min(action.interval.start for action in sims), end=max(action.interval.end for action in sims))
                   if sims else None)
        trial = self._trial(router, sims, horizon, force=True) if sims else None
        hard = [v for v in (trial.violations if trial is not None else ()) if v.kind != "safety"]
        if hard:  # 安全违规已由人工确认代替；容量、硬区域不可行仍不执行
            detail = hard[0].detail
            task = task.model_copy(update={"status": "done", "confirmed_at": at, "done_at": at,
                                           "basis": f"{task.basis}；确认后复核不可行，未执行：{detail}"})
            self.store.save_task(task)
            self._mirror_task(runner, task)
            self._note(runner, "b47_work_order_infeasible", at,
                       {"work_order_id": task.work_order_id, "ticket_id": task.ticket_id, "detail": detail})
            self._hold_ticket(router, runner, task.ticket_id, "work_order", status="blocked")
            return DeliveryOutcome(outcome="blocked_infeasible", reason=detail,
                                   action_indexes=tuple(range(len(task.actions))))
        task = task.model_copy(update={"status": "confirmed", "confirmed_at": at})
        self.store.save_task(task)
        self._mirror_task(runner, task)
        task = task.model_copy(update={"status": "in_progress"})
        self.store.save_task(task)
        executed = failed = 0
        applied: list[int] = []
        failed_indexes: list[int] = []
        for index, action in enumerate(task.actions):
            key = f"wo#{task.work_order_id}#{index}"
            record = ExecutionRecord(key=key, run_id=runner.run_info.run_id, ticket_id=task.ticket_id, round=task.round,
                                     action_index=index, entity_id=action.entity_id, command=action.command, at=at,
                                     detail="人工确认后执行")
            if is_record_only(action):
                if self.store.claim_execution(record.model_copy(update={"detail": "台账动作：只登记维护记录"})):
                    self.store.save_execution(record.model_copy(update={"detail": "台账动作：只登记维护记录"}))
                    self._note(runner, "maintenance_log", at,
                               {"work_order_id": task.work_order_id, "ticket_id": task.ticket_id,
                                "entity_id": action.entity_id, "command": action.command, "params": action.params})
                    applied.append(index)
                executed += 1
                continue
            if not self.store.claim_execution(record):
                continue
            try:
                runner.apply(_sim(action, max(effective, action.interval.start)),
                             source={"work_order_id": task.work_order_id, "ticket_id": task.ticket_id,
                                     "confirmed_at": at.isoformat(), "trigger_at": at.isoformat(),
                                     "effective_at": effective.isoformat(), "round": task.round})
            except PlantkgError as exc:
                failed += 1
                failed_indexes.append(index)
                self.store.save_execution(record.model_copy(update={"status": "infeasible", "detail": str(exc)[:200]}))
                continue
            self.store.save_execution(record)
            executed += 1
            applied.append(index)
        if executed == 0 and failed:
            task = task.model_copy(update={"status": "done", "done_at": at,
                                           "basis": f"{task.basis}；确认后动作全部不可执行，未执行"})
            self.store.save_task(task)
            self._mirror_task(runner, task)
            self._note(runner, "b47_work_order_infeasible", at,
                       {"work_order_id": task.work_order_id, "ticket_id": task.ticket_id, "detail": "确认后全部动作不可执行"})
            self._hold_ticket(router, runner, task.ticket_id, "work_order", status="blocked")
            return DeliveryOutcome(outcome="blocked_infeasible", reason="确认后动作全部不可执行，未执行",
                                   action_indexes=tuple(failed_indexes))
        task = task.model_copy(update={"status": "done", "done_at": at,
                                       "basis": f"{task.basis}；确认后执行 {executed} 个动作"})
        self.store.save_task(task)
        self._mirror_task(runner, task)
        ticket = router.store.ticket(task.ticket_id)
        opened = ticket.opened_at if ticket is not None else task.created_at
        self._note(runner, "b47_work_order_done", at,
                   {"work_order_id": task.work_order_id, "ticket_id": task.ticket_id, "executed": executed,
                    "event_to_order_min": round((task.created_at - opened).total_seconds() / 60, 1),
                    "order_to_done_min": round((at - task.created_at).total_seconds() / 60, 1)})
        self._hold_ticket(router, runner, task.ticket_id, "work_order", status="resolved")
        return DeliveryOutcome(outcome="executed", reason=f"确认后执行 {executed} 个动作",
                               action_indexes=tuple(applied))

    # ---------------------------------------------------------------- 临时规则（值班指令）
    def on_instruction(self, event: DayEvent) -> bool:
        """B27 的指令钩子：转成临时规则返回 True；解析不出返回 False；硬约束/试算不可行转人工待办。

        硬约束与试算不可行不拒绝：不下发动作、不登记生效临时规则，记 rejected 与 needs_human（含可读原因），
        返回 True 表示已接住并转人工。
        """
        if self.store is None:
            return False
        run_id = event.payload.get("run_id")
        if run_id is None:  # 兼容单 Router 的直接调用；多个运行时必须由值守层指定。
            routers = self._routers()
            router = routers[0] if len(routers) == 1 else None
        else:
            router = self._router_for_run(run_id)
        if router is None:
            return False
        runner = self._runner_for(router)
        if runner is None:
            router.store.set_ticket_state(f"instruction_{event.event_id}", "parsed", "failed：未绑定可执行指令的场景")
            return False
        if any(item.rule.source_event_id == event.event_id for item in self.store.temp_rules(run_id=router.run_id)):
            return True
        parsed, reason = self._parse_instruction(event, runner)
        if parsed is None:
            router.store.set_ticket_state(f"instruction_{event.event_id}", "parsed", f"failed：{reason}")
            return False
        zone_entity, setpoint_c, start, end = parsed
        action = SimAction(entity_id=zone_entity, command="set_setpoint",
                           params={"setpoint_c": setpoint_c}, interval=Interval(start=start, end=end))
        hard = sorted(set(self._zone_flags(runner, zone_entity)) & {"no_shed", "hard_climate", "no_power_cut"})
        if hard:
            self._defer_instruction(router, runner, event, f"硬约束：区域标记 {hard} 不能改设定，临时规则未生效")
            return True
        try:
            cost = runner.estimate_cost((action,), end)
        except Exception:  # 试算失败沿用转人工路径
            cost = None
        if cost is None:
            self._defer_instruction(router, runner, event, "试算不可行，临时规则未生效")
            return True
        rule = TempRule(rule_id=self.store.next_id("TR"), text=event.summary, effective=action.interval,
                        entity_ids=(zone_entity,), created_at=event.at,
                        source_event_id=event.event_id, estimated_cost_cny=round(cost, 2))
        self.store.save_temp_rule(StoredTempRule(rule=rule, run_id=router.run_id))
        action = action.model_copy(update={"params": {**action.params, "temp_rule": rule.rule_id}})
        runner.apply(action, source={"temp_rule": rule.rule_id, "event_id": event.event_id})
        runner.temp_rules.add(rule)
        runner.trace.temp_rules[rule.rule_id] = rule
        runner.db.store.write_records(runner.run_info.run_id,
                                      [("temp_rule", rule.rule_id, event.at, rule.model_dump(mode="json"))])
        runner.note("temp_rule", event.at, {"rule_id": rule.rule_id, "cost_cny": rule.estimated_cost_cny,
                                            "hard_zone": hard, "text": rule.text})
        router.store.set_ticket_state(f"instruction_{event.event_id}", "temp_rule", rule.rule_id)
        return True

    def _defer_instruction(self, router: Any, runner: Any, event: DayEvent, reason: str) -> None:
        """硬约束或试算不可行：不下发动作、不登记生效临时规则，记 rejected 与 needs_human 供人工待办。"""
        self._note(runner, "rejected", event.at,
                   {"event_id": event.event_id, "rule_text": event.summary, "reason": f"{reason}，转人工待办"})
        if router is not None:
            router.store.set_ticket_state(f"instruction_{event.event_id}", "status", f"needs_human：{reason}")

    def _parse_instruction(self, event: DayEvent, runner: Any) -> tuple[tuple[str, float, datetime, datetime] | None, str]:
        """解析值班指令为（区域实体, 设定值, 起, 止）；解析不出、窗口越界或区域未知时返回 (None, 原因)。"""
        payload = event.payload or {}
        text = str(payload.get("text") or event.summary)
        zone = payload.get("zone")
        try:
            match = _SETPOINT.search(text)
            if zone is None:
                zone = next((key for word, key in _ZONE_WORDS if word in text), None)
            if not match or zone is None:
                return None, "解析不出区域或设定值"
            setpoint = float(match.group(1))
            scenario = runner.world.scenario
            window = _WINDOW.search(text)
            start_text = payload.get("start") or (window.group(1) if window else "13:00")
            end_text = payload.get("end") or (window.group(2) if window else "17:00")
            start_day, start_hour, start_minute = _script_time(start_text)
            end_day, end_hour, end_minute = _script_time(end_text, start_day)
            start = scenario.instant(_script_text(start_day, start_hour, start_minute))
            end = scenario.instant(_script_text(end_day, end_hour, end_minute))
            if end > scenario.end:
                return None, f"规则结束 {end_text} 超出剧本结束"
            zone_entity = runner.world.index.eid(f"zone:{zone}")
            if end <= start:
                end = start + timedelta(hours=4)
            return (zone_entity, setpoint, start, end), ""
        except KeyError:
            return None, f"未知区域 {zone}"
        except ValueError as exc:
            return None, f"指令窗口不合法：{exc}"

    # ---------------------------------------------------------------- 试探（D61）
    def on_probe(self, ticket: Any, proposal: ProbeProposal, binding: RunBinding) -> None:
        if self.store is None:
            return
        router = self._router_for_ticket(ticket.ticket_id)
        if router is None:
            return
        runner = self._runner_for(router)
        if runner is None:
            return
        if self.store.active_pause(binding.run_id) is not None:  # 暂停自动执行（#245）：试探动作也不自动下发
            router.mark_probe(binding, "rejected", "暂停自动执行：试探动作不自动下发")
            return
        actions = [SimAction(entity_id=a.entity_id, command=a.command, params=dict(a.params), interval=a.interval)
                   for a in proposal.actions]
        trial = self._trial(router, actions, proposal.actions[0].interval if proposal.actions else None)
        if trial is not None and not trial.feasible:
            router.mark_probe(binding, "infeasible", "试探动作试算不可行")
            return
        category = self.store.category("probe", None)
        known = self.store.level(category)
        # 试探是调查的一部分、动作按可撤回设计：默认按见过的类别 L2，policy 里显式给 probe 等级时才覆盖
        bound_policy = self._bound_policy(router)
        level = self._effective_level(router, category) if self.registry.hooks_for('evolution.autonomy_for_run') else \
            ((getattr(bound_policy, "autonomy_levels", {}) or {}).get("probe") or
             getattr(bound_policy, "autonomy_default_level", "L2"))
        if known is None:
            self.store.mark_seen(category, proposal.created_at, level)
        decision = policy.decide(level, policy.REVERSIBLE, 0.85, bound_policy)
        if decision.outcome not in (policy.EXECUTE, policy.EXECUTE_SAMPLING, policy.EXECUTE_REVIEW):
            router.mark_probe(binding, "rejected", f"自主等级 {level}：试探动作也只提议")
            return
        for index, action in enumerate(actions):
            key = f"probe#{ticket.ticket_id}#r{proposal.round}#{index}"
            record = ExecutionRecord(key=key, run_id=binding.run_id, ticket_id=ticket.ticket_id, round=binding.round,
                                     action_index=index, entity_id=action.entity_id, command=action.command,
                                     at=proposal.created_at, detail=f"试探窗口 {proposal.window_min} 分钟")
            if not self.store.claim_execution(record):
                continue
            try:
                runner.apply(action, source={"b47": "probe", "ticket_id": ticket.ticket_id, "round": proposal.round})
            except PlantkgError as exc:
                self.store.save_execution(record.model_copy(update={"status": "infeasible", "detail": str(exc)[:200]}))
                router.mark_probe(binding, "infeasible", str(exc)[:200])
                continue
            self.store.save_execution(record)
        if decision.outcome == policy.EXECUTE_REVIEW:  # L1：试探动作先做后审
            self._queue("review", ticket, proposal.round, None, actions[0] if actions else None, 0,
                        reason=f"试探动作先做后审（{level}）", ticket_id=ticket.ticket_id, run_id=binding.run_id,
                        at=proposal.created_at)

    # ---------------------------------------------------------------- 分叉与推进步
    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        if self.store is None:
            return
        runner = self._runner_for_run(parent_run.run_id)
        router = getattr(getattr(runner, 'dispatcher', None), 'router', None)
        self.store.seed_run_caps(parent_run.run_id, at,
                                 (lambda category: self._effective_level(router, category)) if router else None)
        categories = self.store.copy_run_caps(parent_run.run_id, child_run.run_id, at)
        self.override.inherit_pause(parent_run.run_id, child_run.run_id)  # 运行级暂停交给子运行（#245）
        # Only a newly published policy that explicitly raises this category can
        # release its inherited safety cap; unrelated publications cannot.
        hooks = self.registry.hooks_for('evolution.policy_for_run') if self.registry is not None else ()
        if hooks and runner is not None:
            try:
                fallback = runner.policy
                old = hooks[-1](parent_run.run_id, fallback)
                new = hooks[-1](child_run.run_id, fallback)
                auth_hooks = self.registry.hooks_for('evolution.authorized_category_for_manifest')
                authorized = auth_hooks[-1](child_run.version_manifest_id) if auth_hooks else None
                if old is not None and new is not None and old.policy_version != new.policy_version:
                    for category in categories:
                        updated = policy.autonomy_level(new, category, seen=False)
                        cap = self.store.level(f'@run:{child_run.run_id}:{category}')
                        if (authorized == category and category in new.autonomy_levels and cap is not None
                                and int(updated[1]) > int(cap[0][1])):
                            self.store.set_level(f'@run:{child_run.run_id}:{category}', 'L3', at)
            except Exception:  # noqa: BLE001 — retain inherited caps on uncertainty
                pass

        for stored in self.store.temp_rules(run_id=parent_run.run_id, status="active"):
            if stored.rule.effective.end <= at:
                self.store.save_temp_rule(stored.model_copy(update={"status": "expired"}))
            else:
                # 分叉不换规则编号：只把执行表这一行改归子运行（主键是 plant_id+rule_id），
                # 场景库、动作来源、控制台与导出始终是同一个编号（#245）
                self.store.save_temp_rule(stored.model_copy(update={"run_id": child_run.run_id}))
        runner = self._runner_for_run(parent_run.run_id)  # 分叉钩子在 run_info 切换前调用：镜像仍写父运行的场景视图
        for task in self.store.tasks():
            if task.run_id != parent_run.run_id:
                continue
            if task.status == "proposed":  # 待确认的子工单交接给子运行，并进场景视图/日报（#106）
                child_task = task.model_copy(update={"work_order_id": self.store.next_id("WO"),
                                                     "pending_action_id": None, "run_id": child_run.run_id})
                self.store.save_task(child_task)
                kind = PendingActionKind.work_order if child_task.kind == "work_order" else PendingActionKind.human_decision
                self._propose_pending(child_task, kind)
                self._mirror_task(runner, child_task)
            elif task.status in ("confirmed", "in_progress"):
                # 已由父运行开始处置：留在父运行记完成并注明，子运行不再重复执行
                done = task.model_copy(update={"status": "done", "done_at": at,
                                               "basis": f"{task.basis}；分叉时已在父运行 {parent_run.run_id} 处置，"
                                                        f"子运行不再重复执行"})
                self.store.save_task(done)
                self._mirror_task(runner, done)

    def authorization_published(self, parent_manifest: str, manifest_id: str, category: str | None,
                                action_id: str, at: datetime) -> None:
        if self.store is not None:
            self.store.publish_manifest_caps(parent_manifest, manifest_id, category, action_id, at)

    def step(self, run_id: str, now: datetime) -> None:
        if self.store is None:
            return
        self.store.expire_temp_rules(now)
        router = next((r for r in self._routers() if getattr(r, "run_id", None) == run_id), None)
        if router is None:
            return
        deadline = timedelta(minutes=int(getattr(router.policy, "human_deadline_min", 30)))
        for task in self.store.tasks(status="proposed"):
            if task.kind != "human_decision" or task.run_id != run_id or task.autonomy_paused:
                continue  # 暂停积压的提议不按时限过期：恢复后逐条批准，确认时仍会重新试算
            if now - task.created_at >= deadline:
                expired = task.model_copy(update={"status": "expired",
                                                  "basis": f"{task.basis}；到期未处理，不执行"})
                self.store.save_task(expired)
                self._cancel_pending(expired, "人工待办到期未处理")
                runner = self._runner_for(router)
                if runner is not None:
                    self._mirror_task(runner, expired)
                self._hold_ticket(router, runner, task.ticket_id, "work_order", status="blocked")

    # ---------------------------------------------------------------- 队列结果回写（供 B32）
    def resolve_queue(self, kind: str, item_id: str, status: str, record: Mapping[str, Any] | None = None,
                      actor: Actor | None = None) -> None:
        if self.store is None:
            return
        if status not in ("correct", "wrong", "accepted", "rejected"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知的队列结果 {status}")
        self.store.resolve_queue(kind, item_id, status, dict(record or {}))
        self._emit_feedback(kind, item_id, status, record, actor)

    def _emit_feedback(self, kind: str, item_id: str, status: str, record: Mapping[str, Any] | None,
                       actor: Actor | None) -> None:
        """把批准/驳回/抽查结果交给 B32 进化（经钩子，不在本包 import 进化服务）。"""
        hooks = self.registry.hooks_for("evolution.record_correction") if self.registry is not None else ()
        if not hooks or self.store is None:
            return
        item = next((entry for entry in self.store.queue(kind) if entry.item_id == item_id), None)
        if item is None:
            return
        router = self._router_for_ticket(item.ticket_id)
        event = self._ticket_event(router, router.store.ticket(item.ticket_id)) if router is not None else None
        if event is None:
            return
        outcome = ("corrected" if status in ("correct", "accepted") else "rejected") if kind == "correction" \
            else ("approved" if status in ("accepted", "correct") else "rejected")
        category = self.store.category(event.event_type, self._entity_type(router, event.entity_id))
        feedback = {"category": category, "event_type": event.event_type, "outcome": outcome,
                    "source": kind,
                    "system_error": bool((record or {}).get("system_error", True)),
                    "note": str((record or {}).get("note") or (record or {}).get("reason") or "")}
        context = Context(plant_id=self.plant_id, industry_pack="manufacturing", actor=actor, channel="offline",
                          trace_id=f"b47-feedback-{item_id}", as_of=datetime.now(timezone.utc),
                          idempotency_key=f"b47-feedback-{kind}-{item_id}")
        try:
            hooks[-1](context, item.ticket_id, {"feedback": feedback})
        except Exception:  # noqa: BLE001 — 反馈失败不回滚已确认的队列结果
            log.debug("反馈未进入 B32 进化：%s", item_id, exc_info=True)

    def _emit_task_feedback(self, task: WorkOrderTask, kind: str, *, system_error: bool, note: str,
                            actor: Actor | None) -> None:
        """工单/人工待办被统一端点拒绝后通知 B32（#193 口径：system_error=False 只记录、不降级）。"""
        self.emit_ticket_feedback(task.ticket_id, kind, outcome="rejected", system_error=system_error, note=note,
                                  actor=actor, key=f"b47-reject-{task.work_order_id}")

    def emit_ticket_feedback(self, ticket_id: str, source: str, *, outcome: str, system_error: bool, note: str,
                             actor: Actor | None, key: str) -> None:
        """按事件单把一次人工结果交给 B32 进化（经钩子）；撤销动作的 system_wrong 记账也走这里。"""
        hooks = self.registry.hooks_for("evolution.record_correction") if self.registry is not None else ()
        if not hooks or self.store is None:
            return
        router = self._router_for_ticket(ticket_id)
        event = self._ticket_event(router, router.store.ticket(ticket_id)) if router is not None else None
        if event is None:
            return
        category = self.store.category(event.event_type, self._entity_type(router, event.entity_id))
        feedback = {"category": category, "event_type": event.event_type, "outcome": outcome,
                    "source": source, "system_error": system_error, "note": note}
        context = Context(plant_id=self.plant_id, industry_pack="manufacturing", actor=actor, channel="offline",
                          trace_id=key, as_of=datetime.now(timezone.utc), idempotency_key=key)
        try:
            hooks[-1](context, ticket_id, {"feedback": feedback})
        except Exception:  # noqa: BLE001 — 反馈失败不回滚已生效的人工结果
            log.debug("人工结果未进入 B32 进化：%s", key, exc_info=True)

    def review_outcome(self, item_id: str, ok: bool, actor: Actor | None = None, *, system_error: bool = True,
                       note: str = "") -> None:
        """先做后审复核：不通过时按动作可逆性回滚，并给该类事件降一级。重复复核同一项幂等。"""
        if self.store is None:
            return
        items = [item for item in self.store.queue("review") if item.item_id == item_id]
        if not items:
            return
        item = items[0]
        if item.status != "pending":  # 已复核过：不回滚、不重复降级
            return
        self.resolve_queue("review", item_id, "correct" if ok else "wrong",
                           {"reviewed": True, "system_error": system_error, "note": note}, actor=actor)
        if ok:
            return
        ticket_id = item.ticket_id
        router = self._router_for_ticket(ticket_id)
        runner = self._runner_for(router) if router is not None else None
        event = self._ticket_event(router, router.store.ticket(ticket_id)) if router is not None else None
        if router is None or event is None:
            return
        category = self.store.category(event.event_type, self._entity_type(router, event.entity_id))
        effective = self._effective_level(router, category)
        if getattr(router, 'run_id', None):
            lowered = f'L{max(0, int(effective[1]) - 1)}'
            self.store.set_level(f'@run:{router.run_id}:{category}', lowered, item.at)
            try:
                manifest_id = self._run_info(router).version_manifest_id
                self.store.set_level(f'@policy:{manifest_id}:{category}', lowered, item.at)
            except (KeyError, PlantkgError):
                pass
        current = self.store.level(category)
        if current is not None and current[0] != "L0":
            self.store.set_level(category, f"L{int(current[0][1]) - 1}", item.at)
        if runner is not None and item.record.get("entity_id") and item.record.get("command") in (
                "set_mode", "set_active", "switch_circuit", "set_setpoint"):
            rollback = SimAction(entity_id=item.record["entity_id"], command="set_mode", params={"mode": "auto"},
                                 interval=Interval(start=item.at, end=item.at + timedelta(minutes=1)))
            try:
                runner.apply(rollback, source={"b47": "rollback", "ticket_id": ticket_id, "item_id": item_id})
            except PlantkgError:
                pass

    # ---------------------------------------------------------------- 待确认动作（B15 统一确认端点）
    @staticmethod
    def _system_error(action: Any, edits: Mapping[str, Any] | None = None) -> bool:
        """#193/#234：确认/拒绝时可经 edits（或队列 payload）声明“不是系统判断有误”；edits 优先。"""
        payload = action.payload or {}
        chosen = edits if edits is not None else (getattr(action, "edits", None) or {})
        return bool(chosen.get("system_error", payload.get("system_error", True)))

    def confirm_from_pending(self, kind: str, action: Any, actor: Actor) -> ToolResult:
        payload = action.payload or {}
        # 值班填的文本随确认原因进来（B35 纠错卡 / 回答卡），作为 note 交给 B32 进化反馈
        note = str(getattr(action, "reason", "") or "")
        if kind in ("correction", "sampling"):
            self.resolve_queue(kind, str(payload.get("item_id")), str(payload.get("result") or "accepted"),
                               {"system_error": self._system_error(action), "note": note}, actor=actor)
            return ToolResult(ok=True, business_status="done",
                              data={"item_id": payload.get("item_id"), "kind": kind, "outcome": "executed",
                                    "basis": "纠错结果已回写队列" if kind == "correction" else "抽查结果已回写队列"})
        if kind == "review":  # 复核通过；复核不通过走 reject_from_pending（回滚并降级）
            item_id = str(payload.get("item_id") or "")
            self.review_outcome(item_id, True, actor, system_error=self._system_error(action), note=note)
            return ToolResult(ok=True, business_status="done",
                              data={"item_id": item_id, "kind": kind, "outcome": "executed", "basis": "复核通过"})
        if self.store is None:
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "分级执行未装配执行表")
        work_order_id = str(payload.get("work_order_id") or "")
        task = self.store.task(work_order_id)
        if task is None:
            return tool_error(ErrorCode.NOT_FOUND, f"操作工单 {work_order_id} 不存在")
        if task.status != "proposed":  # 过期/完成/驳回的工单不能再“确认成功但不执行”
            return tool_error(ErrorCode.ACTION_EXPIRED if task.status == "expired" else ErrorCode.STATE_CONFLICT,
                              f"操作工单 {work_order_id} 已是 {task.status}，不能再确认")
        at = action.decided_at or datetime.now(tz=action.proposed_at.tzinfo)
        runner = self._runner_for_run(task.run_id)
        router = self._router_for_run(task.run_id)
        if runner is None or router is None:
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, f"操作工单 {work_order_id} 的执行器不在线，确认未生效")
        receipt = self._confirm_task(router, runner, task, at)
        updated = self.store.task(work_order_id) or task
        # #204-4a：business_status 保持 done（B35 值班台按它显示），执行结果放 data.outcome/basis 区分“已执行/未执行”
        return ToolResult(ok=True, business_status="done",
                          data={"work_order_id": work_order_id, "status": updated.status,
                                "outcome": receipt.outcome, "basis": updated.basis})

    def reject_from_pending(self, kind: str, action: Any, actor: Actor,
                            edits: Mapping[str, Any] | None = None) -> ToolResult:
        """统一确认端点拒绝时的执行函数（只在执行函数声明 reject 时可被调用）。

        edits 是复核员随拒绝提交的修订（#234 只允许 system_error）；有它时优先于队列 payload。
        """
        payload = action.payload or {}
        note = str(getattr(action, "reason", "") or "")
        if kind == "review":
            item_id = str(payload.get("item_id") or "")
            self.review_outcome(item_id, False, actor, system_error=self._system_error(action, edits), note=note)
            return ToolResult(ok=True, business_status="done", data={"item_id": item_id, "kind": kind})
        if kind in ("correction", "sampling"):  # 拒绝也要落队列并发反馈，否则 pending 永远占住
            item_id = str(payload.get("item_id") or "")
            result = "wrong" if kind == "correction" else "rejected"
            self.resolve_queue(kind, item_id, result,
                               {"system_error": self._system_error(action, edits), "note": note}, actor=actor)
            return ToolResult(ok=True, business_status="done", data={"item_id": item_id, "kind": kind,
                                                                    "result": result})
        # 操作工单 / 人工待办退回：任务进入 rejected，并照常开一条纠错队列项（与 B47 自己的 reject 同一条路径）
        work_order_id = str(payload.get("work_order_id") or "")
        if self.store is None:
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "分级执行未装配执行表")
        if not work_order_id:
            return tool_error(ErrorCode.PRECONDITION, f"{kind} 动作缺少 work_order_id，无法退回")
        task = self.store.task(work_order_id)
        if task is None:
            return tool_error(ErrorCode.NOT_FOUND, f"操作工单 {work_order_id} 不存在")
        if task.status == "rejected":  # 上次拒绝已落库但决定提交失败：幂等成功，让端点继续提交决定（#258）
            return ToolResult(ok=True, business_status="done",
                              data={"work_order_id": work_order_id, "kind": kind, "status": "rejected"})
        if task.status != "proposed":
            return tool_error(ErrorCode.ACTION_EXPIRED if task.status == "expired" else ErrorCode.STATE_CONFLICT,
                              f"操作工单 {work_order_id} 已是 {task.status}，不能再退回")
        reason = str(getattr(action, "reason", "") or "")
        at = action.decided_at or datetime.now(tz=action.proposed_at.tzinfo)
        # #204-1：统一端点的“不同意”走现有拒绝语义：工单置 rejected、事件单 correction+blocked、进纠错队列；
        # human_decision 是人工否决，同一条路径并留一条带原因的 note。事件单 blocked 只在 _reject 里回写一次。
        text = f"值班人员在统一端点拒绝：{reason}" if kind == "work_order" else f"人工否决：{reason}"
        outcome = self._reject(work_order_id, text, cancel_pending=False)
        if outcome == "writeback_failed":
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE,
                              f"事件单 {task.ticket_id} 的 blocked 回写失败，拒绝未生效，可重试")
        if outcome != "ok":
            return tool_error(ErrorCode.STATE_CONFLICT, f"操作工单 {work_order_id} 已不是待确认状态，拒绝未生效")
        if kind == "work_order":
            self._emit_task_feedback(task, kind, system_error=self._system_error(action, edits), note=reason, actor=actor)
        else:
            runner = self._runner_for_run(task.run_id)
            if runner is not None:
                self._note(runner, "b47_human_rejected", at,
                           {"work_order_id": task.work_order_id, "ticket_id": task.ticket_id,
                            "reason": "人工否决", "note": reason})
        return ToolResult(ok=True, business_status="done",
                          data={"work_order_id": work_order_id, "kind": kind, "status": "rejected"})

    # ---------------------------------------------------------------- 内部
    def _effective(self, at: datetime, policy_config: Any) -> datetime:
        budget = getattr(getattr(policy_config, "response_budget", None), "layer12_min", 1)
        return _ceil_minute(at + timedelta(minutes=int(budget)))

    def _hold_ticket(self, router: Any, runner: Any, ticket_id: str, pending: str, *, status: str = "waiting") -> None:
        state = runner.tickets.tickets.get(ticket_id) if runner is not None else None
        if state is not None:
            if status == "resolved":
                state.pending.clear()
            elif pending not in state.pending:
                state.pending.append(pending)
            state.update(status=status)
            save = getattr(runner.dispatcher, "save", None)
            if callable(save):
                save(state, runner.now)
        try:
            router.store.set_ticket_status(ticket_id, status)
        except Exception:  # noqa: BLE001
            pass

    def _mirror_task(self, runner: Any, task: WorkOrderTask) -> None:
        if runner is None or not callable(getattr(runner, "save_work_order", None)):
            return
        order = task.contract()
        runner.save_work_order(order)
        runner.tickets.work_orders[task.work_order_id] = order

    def _cancel_pending(self, task: WorkOrderTask, reason: str) -> None:
        """工单进入终态后关闭对应待确认动作，避免统一确认端点还能“确认成功但不执行”。"""
        if not task.pending_action_id:
            return
        store = self.registry.ports.get(PendingActionStore)
        cancel = getattr(store, "cancel", None) if store is not None else None
        if not callable(cancel):
            return
        try:
            cancel(task.pending_action_id, reason)
        except PlantkgError as exc:  # 已确认/已执行：由确认端点返回冲突，这里只记日志
            log.warning("待确认动作 %s 未能随工单 %s 关闭：%s（%s）",
                        task.pending_action_id, task.work_order_id, exc.code.value, exc.message)

    def _expire_older(self, router: Any, runner: Any, ticket_id: str, round_: int) -> None:
        for task in self.store.tasks(ticket_id, "proposed"):
            if task.run_id != router.run_id:
                continue
            if task.round < round_ and task.origin != "d55":
                # D55 整单等人工：后续轮次不把原工单过期掉，否则重大损失门控随轮次丢失（#102/#170）
                expired = task.model_copy(update={"status": "expired"})
                self.store.save_task(expired)
                self._mirror_task(runner, expired)
                self._cancel_pending(expired, "事件单已到新一轮，这张工单不再可确认")

    def _propose_pending(self, task: WorkOrderTask, kind: PendingActionKind) -> None:
        store = self.registry.ports.get(PendingActionStore)
        if store is None:
            return
        context = Context(plant_id=self.plant_id, industry_pack="manufacturing",
                          actor=Actor(actor_id="b47-execution-policy", roles=("operator",)), channel="offline",
                          trace_id=f"b47-{task.work_order_id}", as_of=task.created_at.astimezone(timezone.utc), run_id=task.run_id,
                          idempotency_key=f"b47-{task.work_order_id}")
        # target_ref：工单/人工待办用 work_order_id（统一确认端点的请求体只带动作编号与修订）
        payload = {"target_ref": task.work_order_id, "work_order_id": task.work_order_id, "ticket_id": task.ticket_id,
                   "kind": task.kind, "reason": task.reason, "actions": [a.model_dump(mode="json") for a in task.actions]}
        try:
            action = store.propose(kind, payload, context)
            self.store.save_task(task.model_copy(update={"pending_action_id": action.action_id}))
        except PlantkgError as exc:
            if exc.code == ErrorCode.DEPENDENCY_UNAVAILABLE:  # 统一确认端点未就绪不挡开单
                log.debug("操作工单 %s 未能登记待确认动作", task.work_order_id, exc_info=True)
            else:
                log.warning("操作工单 %s 登记待确认动作失败：%s（%s）", task.work_order_id, exc.code.value, exc.message)
        except ValidationError as exc:  # 契约不符是 B47 自己的 bug：记 warning，不吞成 debug
            log.warning("操作工单 %s 的待确认动作不符合契约：%s", task.work_order_id, safe_error_details(exc))

    def _queue(self, kind: str, ticket: Any, round_: int, proposal: ExpertProposal | None, action: ProposedAction | None,
               index: int, *, reason: str, ticket_id: str | None = None, run_id: str | None = None,
               at: datetime | None = None, system_error: bool | None = None) -> None:
        tid = ticket_id or (ticket.ticket_id if ticket is not None else "")
        if not tid:
            return
        item_id = f"{kind}-{tid.rsplit('/', 1)[-1]}-{round_}-{proposal.proposal_id if proposal else 'manual'}-{index}"
        created_at = (at or (proposal.binding.trigger_at if proposal and proposal.binding else datetime.now(timezone.utc))
                      ).astimezone(timezone.utc)
        record = {"proposal_id": proposal.proposal_id if proposal else None, "round": round_,
                  "entity_id": action.entity_id if action else None, "command": action.command if action else None,
                  "reason": reason, "confidence": proposal.confidence if proposal else None}
        if system_error is not None:  # #193：拒绝/纠错原因可声明“不是系统判断有误”，随队列透传给 B32
            record["system_error"] = bool(system_error)
        self.store.add_queue(QueueItem(kind=kind, item_id=item_id, ticket_id=tid,
                                       run_id=run_id or (proposal.binding.run_id if proposal and proposal.binding else None),
                                       at=created_at, record=record))
        if kind in ("correction", "sampling", "review"):  # 统一确认端点可看到并从工作台回写结果（B35）；review 确认=通过、拒绝=不通过
            store = self.registry.ports.get(PendingActionStore)
            if store is not None:
                context = Context(plant_id=self.plant_id, industry_pack="manufacturing",
                                  actor=Actor(actor_id="b47-execution-policy", roles=("operator",)), channel="offline",
                                  trace_id=f"b47-{item_id}", as_of=created_at, idempotency_key=f"b47-{item_id}")
                payload = {"target_ref": item_id, "item_id": item_id, "ticket_id": tid, "kind": kind}
                if system_error is not None:
                    payload["system_error"] = bool(system_error)
                try:
                    store.propose(PendingActionKind(kind), payload, context)
                except PlantkgError as exc:
                    if exc.code == ErrorCode.DEPENDENCY_UNAVAILABLE:
                        log.debug("队列项 %s 未能登记待确认动作", item_id, exc_info=True)
                    else:
                        log.warning("队列项 %s 登记待确认动作失败：%s（%s）", item_id, exc.code.value, exc.message)
                except ValidationError as exc:
                    log.warning("队列项 %s 的待确认动作不符合契约：%s", item_id, safe_error_details(exc))

    @staticmethod
    def _execution_key(proposal: ExpertProposal, index: int, round_: int, ticket_id: str) -> str:
        return f"{ticket_id}#{proposal.proposal_id or proposal.expert}#{index}#r{round_}"

    def _effective_level(self, router: Any, category: str) -> str:
        if self.registry is not None:
            bound_hooks = self.registry.hooks_for("evolution.autonomy_for_run")
            if bound_hooks and getattr(router, 'run_id', None):
                try:
                    runner = self._runner_for(router)
                    published = bound_hooks[-1](category, router.run_id, runner.policy if runner else router.policy)
                except Exception:  # noqa: BLE001 — missing run must not use the active version
                    published = None
                published = published or 'L0'
                cap = self.store.level(f'@run:{router.run_id}:{category}') if self.store is not None else None
                try:
                    manifest_id = self._run_info(router).version_manifest_id
                    policy_cap = self.store.level(f'@policy:{manifest_id}:{category}') if self.store else None
                except (KeyError, PlantkgError):
                    return 'L0'
                if cap is None and self.store is not None:
                    release = self.store.level(f'@release:{manifest_id}:{category}')
                    legacy = None if release else self.store.legacy_downgrade(category)
                    restricted = policy_cap[0] if policy_cap else legacy
                    if restricted is not None:
                        self.store.set_level(f'@run:{router.run_id}:{category}', restricted, datetime.now(timezone.utc))
                        cap = (restricted, True)
                levels = [published] + ([cap[0]] if cap else []) + ([policy_cap[0]] if policy_cap else [])
                return min(levels, key=lambda value: int(value[1]))
            for hook in reversed(self.registry.hooks_for("evolution.published_autonomy")):
                try:
                    published = hook(category)
                except Exception:  # noqa: BLE001
                    published = None
                if published:
                    return published
        stored = self.store.level(category) if self.store is not None else None
        return stored[0] if stored is not None else policy.autonomy_level(router.policy, category, seen=False)

    def _run_info(self, router: Any) -> RunInfo:
        runner = self._runner_for(router)
        runs = getattr(getattr(runner, 'db', None), 'runs', None)
        return (runs or self.registry.resolve(RunStore)).get_run(router.run_id)

    def _bound_policy(self, router: Any) -> Any:
        hooks = self.registry.hooks_for('evolution.policy_for_run') if self.registry is not None else ()
        if hooks and getattr(router, 'run_id', None):
            try:
                runner = self._runner_for(router)
                return hooks[-1](router.run_id, runner.policy if runner else router.policy) or router.policy
            except Exception:  # noqa: BLE001 — _effective_level fails closed
                return router.policy
        return router.policy

    def _record(self, proposal: ExpertProposal, ticket: Any, round_: int, index: int, action: ProposedAction,
                status: str, detail: str) -> None:
        key = self._execution_key(proposal, index, round_, ticket.ticket_id)
        run_id = proposal.binding.run_id if proposal.binding else ""
        record = ExecutionRecord(key=key, run_id=run_id, ticket_id=ticket.ticket_id, proposal_id=proposal.proposal_id,
                                 round=round_, action_index=index, entity_id=action.entity_id, command=action.command,
                                 status=status, at=proposal.binding.trigger_at if proposal.binding else ticket.opened_at,
                                 detail=detail[:300])
        if self.store.execution(run_id, key) is None:
            self.store.claim_execution(record)
        else:
            self.store.save_execution(record)

    def _credibility(self, router: Any, event: DayEvent) -> tuple[float, str]:
        if not event.entity_id:
            return 0.85, "无对象读数"
        try:
            readings = list(router.sensor_readings(event.entity_id, event.at - timedelta(minutes=10), event.at))
        except Exception:  # noqa: BLE001
            return 0.85, "观测层不可用"
        if not readings:
            return 0.85, "无读数"
        suspect = any(bool(r.get("residual_alarm")) or str(r.get("quality", "ok")).lower() not in ("ok", "good", "okay", "unavailable")
                      for r in readings)
        if suspect:
            return 0.7, "读数可疑"
        try:
            stale = all(bool(r.get("at")) and datetime.fromisoformat(str(r["at"])) < event.at - timedelta(minutes=5)
                        for r in readings if r.get("at"))
        except (TypeError, ValueError):  # 观测层给了非 ISO 时间：不冒充新鲜读数
            return 0.85, "读数时间不可解析"
        return (0.85, "读数偏旧") if stale else (1.0, "读数可信")

    def _stale_reason(self, runner: Any, proposal: ExpertProposal, state: Any, ticket: Any) -> str | None:
        """与 B17 直派器同一口径判迟到：只在当前运行、当前轮、状态版本容差内且未过生效时刻的提案才执行。"""
        binding, timing = proposal.binding, proposal.timing
        if binding is None or timing is None:
            return None  # 没有时序的提案交给直派器判，B47 不凭空判过期
        coordinator = getattr(runner, "coordinator", None)
        if coordinator is None:
            return None
        round_ = getattr(getattr(state, "ticket", None), "round", ticket.round)
        try:
            effective = _ceil_minute(timing.effective_at)
            stale = coordinator.stale_reason(
                binding, run_id=runner.run_info.run_id, round_=round_,
                state_version=runner.state.state_version,
                tolerance=runner.world.scenario.params.state_version_tolerance,
                now=runner.now, effective_at=effective)
            expected = coordinator.effective_at(binding.trigger_at)
            expected_layer3 = coordinator.effective_at(binding.trigger_at, layer=3)
            if stale is None and (timing.trigger_at != binding.trigger_at
                                  or timing.effective_at not in (expected, expected_layer3)):
                stale = (f"生效时刻 {timing.effective_at.isoformat()} 不等于 trigger_at + "
                         f"响应预算 {expected.isoformat()}（第 1/2 层）或 {expected_layer3.isoformat()}（第 3 层）")
            return stale
        except Exception:  # noqa: BLE001 — 判不了迟到不冒充判定结果
            return None

    def _trial(self, router: Any, actions: Sequence[SimAction], horizon: Interval | None, *, force: bool = False) -> Any | None:
        if not actions or horizon is None:
            return None
        trial = getattr(router, "trial_runner", None)
        snapshot = getattr(router, "snapshot", None)
        if not callable(trial) or not callable(snapshot):
            return None
        try:
            snap = snapshot()
            # 离线手工派发时快照可能停在几小时前：重放整段不产生新信息，复核交给确认环节
            if not force and horizon.start - getattr(snap, "as_of", horizon.start) > timedelta(minutes=60):
                return None
            return trial(snap, tuple(actions), horizon)
        except Exception:  # noqa: BLE001 — 试算台没接上时不判不可行
            return None

    @staticmethod
    def _horizon(actions: Sequence[ProposedAction]) -> Interval | None:
        if not actions:
            return None
        return Interval(start=min(a.interval.start for a in actions), end=max(a.interval.end for a in actions))

    @staticmethod
    def _entity_type(router: Any, entity: str | None) -> str | None:
        if not entity:
            return None
        for hook in reversed(router.registry.hooks_for("router.entity_type")):
            try:
                return hook(entity)
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _consequence(router: Any, command: str) -> str:
        for hook in reversed(router.registry.hooks_for("router.action_consequence")):
            try:
                return hook(command)
            except Exception:  # noqa: BLE001
                continue
        return policy.HARD

    @staticmethod
    def _safety_veto(router: Any, action: ProposedAction) -> bool:
        """动作级安全否决：按图谱安全标记；聚合对象（电梯群、空压系统、区域）不算查不清，交给 B28 第 1 层。"""
        for hook in reversed(router.registry.hooks_for("router.action_safety")):
            try:
                result = hook(action.entity_id, action.command)
            except Exception:  # noqa: BLE001
                continue
            tags = [tag for tag in result.get("tags") or () if tag.get("id") != "unknown_target"]
            if tags:
                return True
            if result.get("requires_work_order"):  # 图谱查不到对象：按 B25 safety_hit 口径判断是否真的查不清
                for safety in reversed(router.registry.hooks_for("router.safety_hit")):
                    try:
                        return bool(safety(action.entity_id))
                    except Exception:  # noqa: BLE001
                        continue
                return True
            return False
        return False

    def _protection_scope(self, router: Any, runner: Any, event: DayEvent, ticket: Any) -> tuple[tuple[str, str], ...]:
        if not self.protections:
            return ()
        return tuple((record.entity_id, record.command) for record in self.store.executions(
            ticket_id=ticket.ticket_id, run_id=runner.run_info.run_id)
            if record.key.startswith("protection#") and record.status == "executed")

    @staticmethod
    def _is_protection(action: ProposedAction, scope: Sequence[tuple[str, str]]) -> bool:
        return any(action.entity_id == entity and action.command == command for entity, command in scope)

    @staticmethod
    def _dedupe(actions: Iterable[ProposedAction]) -> tuple[ProposedAction, ...]:
        seen, out = set(), []
        for action in actions:
            key = _fingerprint(action)
            if key not in seen:
                seen.add(key)
                out.append(action)
        return tuple(out)

    @staticmethod
    def _zone_flags(runner: Any, entity: str) -> list[str]:
        try:
            return list(runner.world.zone_flags(entity))
        except Exception:  # noqa: BLE001
            return []

    def _runner_for_run(self, run_id: str | None) -> Any | None:
        if not run_id:
            return None
        for _, runner in self._bindings.values():
            if runner is not None and getattr(getattr(runner, "run_info", None), "run_id", None) == run_id:
                return runner
        return None

    def _router_for_run(self, run_id: str | None) -> Any | None:
        if not run_id:
            return None
        return next((router for router in self._routers() if getattr(router, "run_id", None) == run_id), None)

    @staticmethod
    def _note(runner: Any, kind: str, at: datetime, record: Mapping[str, Any]) -> None:
        note = getattr(runner, "note", None)
        if callable(note):
            try:
                note(kind, at, dict(record))
            except Exception:  # noqa: BLE001
                pass


class PendingActionHandler:
    """B15 统一确认端点的执行函数：确认操作工单/人工待办，回写抽查与纠错结果。"""

    def __init__(self, service: ExecutionPolicyService, kind: str) -> None:
        self.service, self.kind = service, kind

    def apply(self, action: Any, actor: Actor) -> ToolResult:
        return self.service.confirm_from_pending(self.kind, action, actor)

    def reject(self, action: Any, actor: Actor, edits: Mapping[str, Any] | None = None) -> ToolResult:
        """复核项拒绝 = 复核不通过：回滚动作并降一级（B04 对声明了 reject 的执行函数调用）。"""
        return self.service.reject_from_pending(self.kind, action, actor, edits)

