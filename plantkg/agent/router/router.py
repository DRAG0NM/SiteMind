"""值守循环（B27）：程序准入 → 严重程度入口 → 事件流订阅 → 快慢双通道研判 → 开事件单 → 派发。

分工由程序保证，不交给主 Agent 的 LLM（D46/D53）：
- 快通道：有匹配规则且 Jev 门控放行，派给谁由图谱定位与 Jev 派域决定，不调用主 Agent；
- 慢通道：无匹配规则或门控未放行，由主 Agent 经 AgentResultSource（kind=investigate）调查后指定专家，
  再经同一个 router.experts 钩子交 B29；
- 严重程度入口（D55）先于调查：安全标记、试算损失超线或会停产时立即调 urgent 钩子；
- 本模块不 import 会诊（B29）、裁决（B28）与分级执行（B47），只在注册表上调用它们登记的钩子。
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from plantkg.agent.router.proposals import SqliteProposalSink
from plantkg.agent.router.recall import Recall
from plantkg.agent.router.severity import UNAVAILABLE, EventTypeRuleMatcher, SeverityEstimator
from plantkg.agent.router.store import RouterStore
from plantkg.agent.router.triage import (
    INVESTIGATION_FORMAT, TRIAGE_KEY, DeterministicInvestigation, DeterministicTriage, compose_query, context_text, experts_from,
    judge_event, parse_investigation, weights,
)
from plantkg.core.contracts import (
    AgentResultKind, AgentRunRecord, AgentTiming, DayEvent, ErrorCode, PlantkgError, ProbeProposal, RunBinding, RunInfo,
    RunMode, SeverityEstimate, TerminalReason, Ticket, TicketSubscription,
)
from plantkg.core.ports import AgentResultSource, Judge, ProposalSink, RuleMatcher
from plantkg.core.registry import Registry
from plantkg.core.versions import utcnow

log = logging.getLogger("plantkg.agent.router")

# 钩子名：处理器由下游分支经 registry.register_hook 登记，本模块不 import 下游
STEP_HOOK = "router.step"                      # fn(run_id, now)：每个推进步（回放 15 分钟、场景 1 分钟），B29 预演登记
PROPOSALS_READY_HOOK = "router.proposals_ready"  # fn(ticket_id, round)：本轮确定性提案都已落库或到截止，B28 登记第 1、2 层
REPLIES_READY_HOOK = "router.replies_ready"    # fn(ticket_id, round)：本轮回答都已挂上或到截止，B28 登记第 3 层
INSTRUCTION_HOOK = "router.instruction"        # fn(event)：值班人员指令类事件，B47 登记临时规则处理器
EXPERTS_HOOK = "router.experts"                # fn(ticket, binding, experts)：B29 并行发起专家运行
URGENT_HOOK = "router.urgent"                  # fn(event, reason)：立即派操作工单，B47 登记处理器
PROBE_HOOK = "router.probe"                    # fn(ticket, proposal, binding)：试探动作交 B47 按自主等级执行
FORK_HOOK = "scenario_fork"                    # 与 B17 相同，分叉时交接未关闭事件单与订阅
ROUTER_HOOK = "router.service"                 # 装配好的值守程序：入口与演示经注册表取用
URGENT_PREFIX = "urgent："    # urgent 待汇报事项的摘要前缀（仅展示用；排序看记录里的 urgent 字段）
SCENARIO_BIND_HOOK = "router.scenario_bind"    # fn(router, runner)：协同组的场景 Router 建好后调用，下游在这里拿到本次运行的 Router
INVESTIGATE_KEY = "investigate"                # AgentResultSource 的注册键（与 AgentResultKind 一致）

log = logging.getLogger("plantkg.router")

# 事件类型自带的领域（synthetic）；router.locate（B25 图谱）给出的归属并入其中
EVENT_DOMAINS: dict[str, tuple[str, ...]] = {
    "elevator_congestion": ("elevator",), "elevator_trapped": ("elevator", "hvac"),
    "load_spike": ("energy", "hvac"), "sim_limit": ("energy", "hvac"), "pv_drop": ("energy",), "heat_wave": ("hvac", "energy"),
    "lighting_fault": ("energy",), "compressor_pressure_drop": ("equipment",), "equipment_fault": ("equipment",),
    "power_quality_dip": ("equipment", "energy"), "voltage_sag": ("equipment", "energy"),
    "unknown_combo": ("equipment", "energy"), "capacity_shed_request": ("hvac", "energy"),
    "compressed_air_low": ("equipment",), "power_loss": ("energy", "equipment"), "hazmat_leak": ("equipment", "hvac"),
}


@dataclass(frozen=True)
class RouterConfig:
    min_severity: int = 2                     # 低于此严重度按噪声丢弃（severity 0 恒丢弃）
    sources: frozenset[str] = frozenset({"script", "random", "sim_limit", "operator_message"})
    event_types: frozenset[str] | None = None  # 事件类型白名单；None 不按类型过滤，未知类型进慢通道（D53①）
    instruction_types: frozenset[str] = frozenset({"operator_instruction"})
    domain_threshold: float = 0.15            # Jev 领域概率达到阈值才派给该专家
    max_experts: int = 4
    straight_through: bool = True             # 关闭直通只供 B33 编排消融
    redispatch_severity: int = 2              # 直通事件重新发起专家运行的严重度阈值
    report: bool = True                       # 结论合成（主 Agent 一句话）是否启用
    probe_window_min: int = 3                 # D61 试探的默认观察窗口（分钟）
    resolved_stream_min: int = 15             # 已解决的事件单在此窗口内继续收同类事件，过后同类事件开新单


@dataclass(frozen=True)
class Admission:
    allowed: bool
    result: str
    reason: str = ""


@dataclass(frozen=True)
class Dispatch:
    ticket: Ticket
    binding: RunBinding
    experts: tuple[str, ...]


@dataclass(frozen=True)
class Batch:
    agent_run_id: str
    source: str
    coverage_gap: bool
    final_answer: str
    ticket_ids: tuple[str, ...]
    failure: str | None = None  # 主 Agent 汇报失败并降级时的原因


@dataclass(frozen=True)
class Handled:
    event_id: str
    result: str
    ticket_id: str | None = None
    experts: tuple[str, ...] = ()
    dispatch: Dispatch | None = None
    batch: Batch | None = None
    channel: str | None = None            # fast / slow / urgent / straight_through
    investigation: dict[str, Any] | None = None


def _aware(moment: datetime) -> datetime:
    """排序比较前统一成带时区的时刻：无时区按 UTC（库内 isoformat 的既有约定），避免混合比较抛 TypeError。"""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@functools.lru_cache(maxsize=None)
def _cached_takes_run_id(fn: Callable[..., Any]) -> bool:
    return _signature_takes_run_id(fn)


def _takes_run_id(fn: Callable[..., Any]) -> bool:
    """读数钩子是否收第 4 个参数 run_id：按签名判参并缓存，四参实现内部的 TypeError 不再被回退掩盖（#201-6）。"""
    try:
        return _cached_takes_run_id(fn)
    except TypeError:  # 不可哈希的 callable：签名缓存放不下它，直接按签名判断
        return _signature_takes_run_id(fn)


def _signature_takes_run_id(fn: Callable[..., Any]) -> bool:
    try:
        parameters = tuple(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # 取不到签名（内置函数等）：按新实现调用，不再用异常回退试探
        return True
    positional = [p for p in parameters
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 4 or any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters)


class Router:
    """值守程序。run_id / knowledge_version 是当前运行与知识版本；分叉后由 on_fork 换成子运行。"""

    def __init__(self, store: RouterStore, registry: Registry, *, plant_id: str, run_id: str, knowledge_version: int = 1,
                 state_version: int | Callable[[], int] = 0, judge: Any = None, config: RouterConfig | None = None,
                 mode: str = "evaluation", locator: Callable[[DayEvent], Sequence[str]] | None = None,
                 sink: SqliteProposalSink | None = None, wall: Callable[[], datetime] = utcnow,
                 severity: SeverityEstimator | None = None,
                 rule_matcher: RuleMatcher | None = None,
                 readings: Callable[[str, datetime, datetime], Sequence[Mapping[str, Any]]] | None = None,
                 policy: Any = None,
                 trial: Callable[[Any, Sequence[Any], Any], Any] | None = None,
                 snapshot: Callable[[], Any] | None = None,
                 safety_hit: Callable[[str], bool] | None = None,
                 capacity_trip: Callable[[str], bool] | None = None,
                 downstream_lines: Callable[[str], list[str]] | None = None,
                 entity_type: Callable[[str], str | None] | None = None,
                 entity_lookup: Callable[[str], dict | None] | None = None,
                 asset_lookup: Callable[[str], dict | None] | None = None) -> None:
        self.store, self.registry = store, registry
        self.plant_id, self.run_id, self.knowledge_version = plant_id, run_id, knowledge_version
        self._state_version = state_version if callable(state_version) else (lambda: state_version)
        self.judge = judge if judge is not None else registry.ports.get(Judge)
        self.config = config or RouterConfig()
        self.mode = mode
        self.locator = locator
        self.sink = sink or SqliteProposalSink(store.writer, plant_id)
        self.recall = Recall(store, self.sink, plant_id=plant_id, entity_lookup=entity_lookup, asset_lookup=asset_lookup)
        self._wall = wall
        self._seq = 0
        self._forked_at: datetime | None = None
        self._run_router: Callable[[str], Router | None] | None = None
        self.runs: list[AgentRunRecord] = []
        self.standin: Callable[[Router, Ticket, RunBinding], None] | None = None
        self.readings = readings            # 观测层读数来源（B22 / 场景）
        self.policy = policy                # PolicyConfig：major_loss_cny 等带版本策略
        self.trial_runner = trial           # B17 TrialRunner.trial(snapshot, candidate, horizon)
        self.snapshot = snapshot            # 当前快照来源
        self.severity = severity or SeverityEstimator(
            policy=policy, readings=lambda entity, start, end: self.sensor_readings(entity, start, end),
            trial=lambda snap, candidate, horizon: self._call(trial, "trial_runner", "router.trial", snap, candidate, horizon),
            snapshot=lambda event: self._call(snapshot, "snapshot", "router.snapshot", event),
            safety_hit=lambda entity: bool(self._call(safety_hit, "", "router.safety_hit", entity)),
            capacity_trip=lambda entity, ratio, minutes: bool(self._call(capacity_trip, "", "router.capacity_trip", entity, ratio, minutes)),
            downstream_lines=lambda entity: self._call(downstream_lines, "", "router.downstream_lines", entity) or [],
            entity_type=lambda entity: self._call(entity_type, "", "router.entity_type", entity))
        self._rule_matcher = rule_matcher  # 延迟解析：B32 的规则匹配器可能在 B27 之后才登记

    @property
    def rule_matcher(self) -> RuleMatcher:
        if self._rule_matcher is None:
            self._rule_matcher = self.registry.ports.get(RuleMatcher) or EventTypeRuleMatcher.from_config()
        return self._rule_matcher

    @rule_matcher.setter
    def rule_matcher(self, value: RuleMatcher) -> None:
        self._rule_matcher = value

    def _maybe(self, direct: Any, attr: str, hook: str) -> Callable[..., Any] | None:
        """先用手动注入的实现，其次用注册表钩子登记的实现（下游可后登记）。"""
        value = direct if direct is not None else getattr(self, attr, None)
        if callable(value):
            return value
        hooks = self.registry.hooks_for(hook)
        return hooks[-1] if hooks else None

    def _call(self, direct: Any, attr: str, hook: str, *args: Any) -> Any:
        fn = self._maybe(direct, attr, hook)
        return fn(*args) if fn is not None else None

    # ---------------------------------------------------------------- 事件入口

    def admit(self, event: DayEvent) -> Admission:
        if self.store.event(event.event_id) is not None:
            return Admission(False, "dropped_duplicate", "同一 event_id 已准入")
        if event.severity < max(1, self.config.min_severity):
            return Admission(False, "dropped_severity", f"严重度 {event.severity} 低于阈值 {self.config.min_severity}")
        if event.source not in self.config.sources:
            return Admission(False, "dropped_source", f"来源 {event.source} 不在白名单")
        allowed = self.config.event_types
        if allowed is not None and event.event_type not in allowed and event.event_type not in self.config.instruction_types:
            return Admission(False, "dropped_whitelist", f"事件类型 {event.event_type} 不在白名单")
        return Admission(True, "instruction" if event.event_type in self.config.instruction_types else "admitted")

    def handle_event(self, event: DayEvent, *, flush: bool = True, run_id: str | None = None) -> Handled:
        if run_id is not None and self._run_router is not None:
            selected = self._run_router(run_id)
            if selected is None:
                raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {run_id} 没有已装配的值守路由")
            if selected is not self:
                return selected.handle_event(event, flush=flush)
        admission = self.admit(event)
        if not admission.allowed:
            self.store.record_event(event, admission.result)
            return Handled(event.event_id, admission.result)
        if admission.result == "instruction":
            return self._instruction(event)
        estimate = self.severity.estimate(event)  # D55：准入后先判是否可能伤亡或重大损失
        reason = self.severity.urgent_reason(event, estimate)
        if reason is not None:  # 不等调查与裁决，立即派单
            ticket = self._open(event, channel="urgent")
            self.store.save_severity(ticket.ticket_id, estimate)
            return self._urgent(ticket, event, reason, reason_code=self._urgent_code(event))
        if self.config.straight_through:
            hits = [t for t in self.store.matches(event.entity_id, event.event_type) if self._stream_open(t, event.at)]
            if hits:
                return self._straight_through(event, hits)
        if event.payload.get("residual_alarm") or estimate.field_check:  # D59：读数有残差报警直接进慢通道
            return self._slow_from_new_event(event, estimate, reason="reading_suspect", flush=flush)
        return self._open_and_dispatch(event, estimate, flush=flush)

    def handle_event_for(self, context: Any, event: DayEvent, *, flush: bool = True) -> Handled:
        """按可信上下文的 run_id 处理事件，保证回放事件与推进使用同一路由。"""
        return self.handle_event(event, flush=flush, run_id=getattr(context, "run_id", None))

    # ---------------------------------------------------------------- 严重程度入口（D55）

    def _urgent(self, ticket: Ticket, event: DayEvent, reason: str,
                denied_actions: Sequence[Any] = (), *, already_urgent: bool = False,
                reason_code: str = "escalation") -> Handled:
        """安全标记、重大损失或停产：立即经 urgent 钩子派操作工单，Agent 只能在此之外升级，不能撤销。

        结构化来源码（#170）写进事件 payload 与事件单状态，下游不再靠中文 reason 文案判 D55。
        """
        payload = dict(event.payload)
        payload["reason_code"] = reason_code
        if denied_actions:
            payload["denied_actions"] = [action.model_dump(mode="json") for action in denied_actions]
        event = event.model_copy(update={"payload": payload})
        handlers = self.registry.hooks_for(URGENT_HOOK)
        for handler in handlers:
            handler(event, reason)
        self.store.record_event(event, "urgent", ticket.ticket_id)
        try:
            self.store.set_ticket_state(ticket.ticket_id, "urgent_reason_code", reason_code)
        except Exception:  # noqa: BLE001 — 状态表不可写不挡派单
            pass
        if not already_urgent:
            record = {"summary": f"{URGENT_PREFIX}{reason}", "round": ticket.round, "urgent": True}
            self.store.add_pending(self._pending_id(ticket.ticket_id, "report", event.at, record), at=event.at, kind="report",
                                   ticket_id=ticket.ticket_id, record=record)
        return Handled(event.event_id, "urgent", ticket.ticket_id, (), None, None, channel="urgent")

    @staticmethod
    def _pending_id(ticket_id: str, kind: str, at: datetime, record: Mapping[str, Any]) -> str:
        """待汇报编号由内容派生（#262）：同一运行重放得到同一编号；整条记录（轮次、摘要、提案）区分同一事件单同一时刻的多条。"""
        seed = f"{ticket_id}|{kind}|{at.isoformat()}|{json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)}"
        return "pend-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]

    def _ordered_pending(self) -> list[dict[str, Any]]:
        """同一分钟内：安全类（困人等）先于普通紧急，再按开单时刻、事件单号、种类、urgent 先于本轮汇报、轮次；编号只做最后的兜底，顺序与运行期随机无关（#262）。"""
        def safety(ticket_id: str) -> bool:
            event = self._ticket_event(ticket_id)
            return event is not None and self.severity.is_safety(event)

        def key(item: Mapping[str, Any]) -> tuple[Any, ...]:
            ticket = self.store.ticket(item["ticket_id"])
            minute = _aware(datetime.fromisoformat(item["at"])).replace(second=0, microsecond=0)
            opened = _aware(ticket.opened_at) if ticket else datetime.min.replace(tzinfo=timezone.utc)
            urgent_first = not item["record"].get("urgent")  # 同单同分钟 urgent 在本轮汇报前（显式字段，不看摘要文字）
            return (minute, not safety(item["ticket_id"]), opened, item["ticket_id"], item["kind"], urgent_first,
                    item["record"].get("round") or 0, item["item_id"])
        return sorted(self.store.pending(), key=key)

    def _urgent_code(self, event: DayEvent) -> str:
        """urgent 入口的来源码：安全标记 → safety_veto；停产/重大损失 → d55。"""
        return "safety_veto" if self.severity.is_safety(event) else "d55"

    def _slow_from_new_event(self, event: DayEvent, estimate: SeverityEstimate, *, reason: str, flush: bool) -> Handled:
        """读数可疑的事件：即使命中规则也直接走慢通道，由主 Agent 先核实再决定处置。"""
        ticket = self._open(event, channel="slow")
        self.store.save_severity(ticket.ticket_id, estimate)
        domains, _ = self._judge(ticket, event)
        note = "｜读数可疑，请先现场核实" if estimate.field_check else ""
        return self._slow_round(ticket, event, domains, reason=reason, flush=flush, note=note)

    def _slow_round(self, ticket: Ticket, event: DayEvent, domains: Mapping[str, float], *, reason: str, flush: bool,
                    note: str = "") -> Handled:
        """新事件的慢通道：调查后按统一收尾进试探或派专家。"""
        investigation = self._investigate(ticket, at=event.at, domains=domains, round_=ticket.round + 1,
                                          record=event, reason=reason)
        summary = f"{event.event_type}：{event.summary}{note}"
        if investigation["diagnosis"]:
            summary += f"｜{investigation['diagnosis']}"
        self.store.record_event(event, "opened", ticket.ticket_id)
        dispatch = self._after_investigation(ticket.model_copy(update={"summary": summary}), investigation, reason=reason)
        batch = self.flush() if flush else None
        return Handled(event.event_id, "opened", ticket.ticket_id, tuple(investigation["experts"]), dispatch, batch,
                       channel="slow", investigation=investigation)

    def escalate(self, ticket_id: str, reason: str, *, denied_actions: Sequence[Any] = ()) -> Handled:
        """Agent 把事件单升级为操作工单：只升不降，经 urgent 钩子；重复升级不重复派单。"""
        urgent = self.store.channel_of(ticket_id) == "urgent"
        event = self._ticket_event(ticket_id)
        if event is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件单 {ticket_id} 没有可升级的事件")
        if urgent and not denied_actions:
            return Handled("", "urgent", ticket_id, (), None, None, channel="urgent")
        new_actions = tuple(denied_actions)
        if new_actions:
            key = "urgent_denied_actions"
            seen = set(json.loads(self.store.ticket_state(ticket_id, key) or "[]"))
            unique = []
            for action in new_actions:
                encoded = action.model_dump_json()
                if encoded not in seen:
                    seen.add(encoded)
                    unique.append(action)
            if not unique:
                return Handled("", "urgent", ticket_id, (), None, None, channel="urgent")
            new_actions = tuple(unique)
        if not urgent:
            self.store.set_channel(ticket_id, "urgent")
        handled = self._urgent(self.store.ticket(ticket_id), event, reason, new_actions, already_urgent=urgent,
                               reason_code="safety_veto" if reason == "action_safety" else "escalation")
        if new_actions:
            self.store.set_ticket_state(ticket_id, "urgent_denied_actions", json.dumps(sorted(seen)))
        return handled

    # ---------------------------------------------------------------- 事件流订阅（直通）

    def _straight_through(self, event: DayEvent, tickets: Sequence[str]) -> Handled:
        """订阅命中的事件直接送达事件单，不再研判、不调用 Jev 与主 Agent；多张事件单可同时收到。

        例外（D61）：带来关键读数残差状态变化（正常 → 报警）时，该事件单开新一轮慢通道调查，
        不重新研判、不另开单，只把本轮内容交给主 Agent；专家正在跑就记下，本轮结束时补调查。
        """
        dispatched: Dispatch | None = None
        first: str | None = None
        for ticket_id in tickets:
            ticket = self.store.ticket(ticket_id)
            if ticket is None:
                continue
            first = first or ticket_id
            ticket = ticket.model_copy(update={"event_ids": ticket.event_ids + (event.event_id,)})
            self.store.save_ticket(ticket, run_id=self.run_id)
            self.store.record_event(event, "straight_through", ticket_id)
            transition = self._residual_transition(ticket_id, event)
            running = self.store.is_running(ticket_id)
            probe = self.store.probe_of(ticket_id)
            if probe is not None and probe.get("state") != "done":  # 试探观察中：只累计，由接续调查统一带上
                probe.setdefault("events", []).append({"event_id": event.event_id, "at": event.at.isoformat(), "summary": event.summary,
                                                       "event_type": event.event_type, "residual_alarm": transition})
                self.store.save_probe(ticket_id, probe)
            elif transition and running:
                self.store.set_ticket_state(ticket_id, "residual_pending", event.event_id)
            elif transition:
                dispatched = self._residual_round(ticket, event, event.at) or dispatched
            elif not running and event.severity >= self.config.redispatch_severity:
                dispatched = self._dispatch(ticket, event.at, reason="stream")  # 没在跑才再发起一次
        return Handled(event.event_id, "straight_through", first, dispatched.experts if dispatched else (), dispatched,
                       channel="straight_through")

    def _stream_open(self, ticket_id: str, at: datetime) -> bool:
        ticket = self.store.ticket(ticket_id)
        if ticket is None or ticket.status != "resolved":
            return True
        trigger = self.store.round_trigger(ticket_id, ticket.round) or ticket.opened_at
        return at - trigger <= timedelta(minutes=self.config.resolved_stream_min)

    def _residual_round(self, ticket: Ticket, event: DayEvent, trigger_at: datetime) -> Dispatch | None:
        investigation = self._investigate(ticket, at=trigger_at, domains=self._ticket_weights(ticket.ticket_id, event),
                                          round_=ticket.round + 1, record=event, reason="residual")
        return self._after_investigation(ticket, investigation, reason="residual")

    def _after_investigation(self, ticket: Ticket, investigation: Mapping[str, Any], *, reason: str) -> Dispatch | None:
        """各调查入口共用的收尾：有试探提案就落库进“待试探”交试探处理器，没有才派专家；已派工单的通道只升不降。"""
        ticket = ticket.model_copy(update={"experts": tuple(investigation["experts"])})
        if self.store.channel_of(ticket.ticket_id) != "urgent":
            self.store.set_channel(ticket.ticket_id, "slow")
        self.store.save_ticket(ticket, run_id=self.run_id)
        binding = investigation["binding"]
        if investigation["probe"] is not None:  # D61：给出试探提案就结束本轮，等观察窗口结束再接续
            self._start_probe(ticket, investigation["probe"], binding)
            return None
        return self._dispatch(ticket, binding.trigger_at, reason=reason)

    # ---------------------------------------------------------------- 快慢双通道（D53）

    def _open_and_dispatch(self, event: DayEvent, estimate: SeverityEstimate, *, flush: bool) -> Handled:
        ticket = self._open(event)
        self.store.save_severity(ticket.ticket_id, estimate)
        domains, gate = self._judge(ticket, event)
        matcher = self.rule_matcher
        covered = matcher.covers_for_run(event, self.run_id) if hasattr(matcher, 'covers_for_run') else matcher.covers(event)
        if covered and gate:  # 快通道：有规则且门控放行，不调用主 Agent
            experts = experts_from(domains, self._locate(event), threshold=self.config.domain_threshold,
                                   limit=self.config.max_experts)
            ticket = ticket.model_copy(update={"experts": tuple(experts), "summary": f"{event.event_type}：{event.summary}"})
            self.store.set_channel(ticket.ticket_id, "fast")
            self.store.save_ticket(ticket, run_id=self.run_id)
            self.store.record_event(event, "opened", ticket.ticket_id)
            dispatch = self._dispatch(ticket, event.at, reason="fast")
            batch = self.flush() if flush else None
            return Handled(event.event_id, "opened", ticket.ticket_id, dispatch.experts, dispatch, batch, channel="fast")
        return self._slow_round(ticket, event, domains, reason="slow", flush=flush)

    def _open(self, event: DayEvent, *, channel: str = "fast") -> Ticket:
        subscriptions = (TicketSubscription(entity_id=event.entity_id, event_types=(event.event_type,)),) \
            if event.entity_id else ()
        ticket = Ticket(ticket_id=self.store.next_ticket_id(), opened_at=event.at, status="open", round=0,
                        event_ids=(event.event_id,), experts=(), subscriptions=subscriptions,
                        summary=f"{event.event_type}：{event.summary}")
        self.store.save_ticket(ticket, run_id=self.run_id, running=False)
        self.store.save_subscriptions(ticket.ticket_id, subscriptions)
        self.store.set_channel(ticket.ticket_id, channel)
        return ticket

    def _judge(self, ticket: Ticket, event: DayEvent) -> tuple[dict[str, float], bool]:
        """Jev 派域 + 门控；概率即本次检索与采纳权重。返回（领域权重，门控是否一致）。"""
        result = judge_event(self.judge, event) if self.judge is not None else None
        domains = weights(result, self._fallback_weights(event))
        gate = bool(result is not None and result.gate_accept)
        self.store.save_triage(ticket.ticket_id, ticket.round + 1, at=event.at, gate_accept=gate, domains=domains,
                               source="jev" if gate else "slow_pending", agent_run_id=None, coverage_gap=result is None,
                               summary=f"{event.event_type}：{'门控一致' if gate else '门控未放行，走慢通道'}")
        return domains, gate

    def _fallback_weights(self, event: DayEvent) -> dict[str, float]:
        domains = self._locate(event)
        if not domains:
            return {}
        share = round(1.0 / len(domains), 4)
        return {d: share for d in domains}

    def _ticket_weights(self, ticket_id: str, event: DayEvent | None) -> dict[str, float]:
        """D61 例外不重新研判：领域权重延续开单时的 Jev 结果。"""
        triage = self.store.triage(ticket_id, 1)
        if triage and triage["domains"]:
            return triage["domains"]
        return self._fallback_weights(event) if event is not None else {}

    def _locate(self, event: DayEvent) -> Sequence[str]:
        """图谱定位（router.locate，B25）的归属加事件类型的领域；图谱没接或查不到时只剩后者。"""
        located = self._call(self.locator, "", "router.locate", event) or ()
        return tuple(dict.fromkeys((*located, *EVENT_DOMAINS.get(event.event_type, ()))))

    # ---------------------------------------------------------------- 慢通道调查（D53）

    def _investigate(self, ticket: Ticket, *, at: datetime, domains: Mapping[str, float], round_: int,
                     record: DayEvent | None = None, reason: str = "slow", context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """经 AgentResultSource（kind=investigate）调查：主 Agent 给结论与要问的专家，本模块照此派发。"""
        binding = RunBinding(run_id=self.run_id, knowledge_version=self.knowledge_version, ticket_id=ticket.ticket_id,
                             round=round_, state_version=self._state_version(), trigger_at=at)
        payload: dict[str, Any] = {"ticket_id": ticket.ticket_id, "round": round_, "reason": reason, "domains": dict(domains),
                                   "as_of": at.isoformat(), "query": self._investigation_query(record, ticket, reason, at, context)}
        if record is not None:
            payload["event"] = record.model_dump(mode="json")
            payload["located"] = list(self._locate(record))  # 图谱定位的领域：确定性调查据此选专家
        payload |= dict(context or {})
        source, mode, result = self._run_investigation(binding, payload)
        probe = result.get("probe")
        experts = [e for e in (result.get("experts") or ()) if e in self._expert_domains()]
        unnamed = not experts and probe is None  # 主 Agent 没指定可用专家：回退 Jev，算覆盖不足
        if not experts:
            experts = [max(domains, key=lambda d: (domains[d], d))] if domains else []
        agent_run = self._record_run(binding, source, mode, mode != self.mode or unnamed, result, kind=AgentResultKind.investigate)
        self.store.save_investigation(ticket.ticket_id, round_, at=at, source=agent_run.source,
                                      agent_run_id=agent_run.agent_run_id, coverage_gap=agent_run.coverage_gap,
                                      diagnosis=str(result.get("diagnosis") or ""), experts=experts,
                                      evidence_refs=[str(r) for r in (result.get("evidence_refs") or ())],
                                      probe=probe.model_dump(mode="json") if probe else None)
        return {"experts": experts, "diagnosis": str(result.get("diagnosis") or ""), "probe": probe,
                "coverage_gap": agent_run.coverage_gap, "agent_run_id": agent_run.agent_run_id, "binding": binding}

    def _run_investigation(self, binding: RunBinding, payload: Mapping[str, Any]) -> tuple[Any, str, dict[str, Any]]:
        """取调查结果并按输出契约解析；运行或解析失败时明确降级到确定性调查，不拿半截结果派发。"""
        source, mode = self._source(INVESTIGATE_KEY)
        try:
            result = parse_investigation(source.get(AgentResultKind.investigate, binding, payload))
            return source, mode, result | {"probe": self._probe_proposal(result.get("probe"), binding)}
        except (PlantkgError, ValueError, TypeError) as exc:
            fallback = self.registry.mode_ports.get((AgentResultSource, "deterministic", INVESTIGATE_KEY))
            if fallback is None or fallback is source:
                raise
            result = dict(fallback.get(AgentResultKind.investigate, binding, payload))
            return fallback, "deterministic", result | {"diagnosis": f"主 Agent 调查已降级（{str(exc)[:160]}）｜{result.get('diagnosis') or ''}"}

    def _probe_proposal(self, probe: Mapping[str, Any] | None, binding: RunBinding) -> ProbeProposal | None:
        """调查给的试探提案转成契约；生效区间由本模块定为观察窗口（D61），调查方不用给时间。"""
        if not probe:
            return None
        window = int(probe.get("window_min") or self.config.probe_window_min)
        interval = {"start": binding.trigger_at, "end": binding.trigger_at + timedelta(minutes=window)}
        return ProbeProposal.model_validate({
            "ticket_id": binding.ticket_id, "round": binding.round, "created_at": binding.trigger_at, "window_min": window,
            "hypothesis": probe.get("hypothesis"), "expectations": probe.get("expectations") or (),
            "actions": [dict(action) | {"interval": interval} for action in probe.get("actions") or ()]})

    @staticmethod
    def _investigation_query(event: DayEvent | None, ticket: Ticket, reason: str, at: datetime,
                             context: Mapping[str, Any] | None) -> str:
        if event is None:
            head = (f"事件单 {ticket.ticket_id} 进入第 {ticket.round + 1} 轮接续调查（{reason}），当前时刻 {at.isoformat()}。"
                    f"事件：{ticket.summary}。请只根据本轮注入的内容与读数继续调查；只用时间戳晚于 effective_at 的读数下结论，"
                    f"probe_incomplete 为 true 时写明“试探未完成”。")
        else:
            head = (f"事件单 {ticket.ticket_id} 进入慢通道调查（{reason}，第 {ticket.round + 1} 轮）。"
                    f"事件：{event.event_type}，严重度 {event.severity}，{event.at.isoformat()}，对象 {event.entity_id}：{event.summary}。"
                    f"请调查根因，给出要问的专家与依据。")
        query = head + INVESTIGATION_FORMAT
        return f"{query}\n本轮注入内容（JSON）：\n{context_text(context)}" if context else query

    @staticmethod
    def _expert_domains() -> frozenset[str]:
        from plantkg.knowledge.judge import DOMAIN_DEFINITIONS

        return frozenset(DOMAIN_DEFINITIONS)

    def _source(self, key: str) -> tuple[Any, str]:
        for mode in (self.mode, "deterministic"):
            impl = self.registry.mode_ports.get((AgentResultSource, mode, key))
            if impl is not None:
                if mode != self.mode and self.mode == "live":
                    log.warning("%s 没有 live 来源，本次运行降级为 deterministic", key)
                return impl, mode
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"{key} 的结果来源未登记（{self.mode} 与 deterministic 都没有）")

    # ---------------------------------------------------------------- 试探与接续调查（D61）

    def _start_probe(self, ticket: Ticket, proposal: ProbeProposal, binding: RunBinding) -> None:
        """试探提案落库，事件单进“待试探”；动作交 B47（probe 钩子）按自主等级执行，生效区间即观察窗口。"""
        at = binding.trigger_at
        entity = self._entity_of(ticket)
        baseline = list(self.sensor_readings(entity, at - timedelta(minutes=proposal.window_min), at)) if entity else []
        state = {"proposal": proposal.model_dump(mode="json"), "state": "executing", "effective_at": at.isoformat(),
                 "window_end": (at + timedelta(minutes=proposal.window_min)).isoformat(),
                 "entity": entity, "baseline": baseline, "readings": []}
        self.store.save_probe(ticket.ticket_id, state)
        self.store.save_ticket(ticket.model_copy(update={"round": proposal.round}), run_id=self.run_id)
        self.store.set_ticket_status(ticket.ticket_id, "waiting")  # 待试探：状态存 probe，不另加 Ticket 状态值
        for hook in self.registry.hooks_for(PROBE_HOOK):
            hook(ticket, proposal, binding)

    def mark_probe(self, binding: RunBinding, state: str, note: str = "") -> None:
        """B47 回写试探没做成（被拒/过期/不可行）：接续调查照常发起，只标“试探未完成”。

        binding 用 PROBE_HOOK 收到的那一份：旧轮或父运行的回写只入历史，不碰当前试探。
        执行成功不用回写：窗口到期自然接续；done 是接续调查发起后由本模块写入的终态，外部不能写。
        """
        if state not in ("rejected", "expired", "infeasible"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知的试探状态 {state}")
        ticket_id = binding.ticket_id
        probe = self.store.probe_of(ticket_id)
        if probe is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件单 {ticket_id} 没有试探")
        if binding.run_id != self.run_id or binding.round != probe["proposal"]["round"]:
            self.store.set_ticket_state(ticket_id, f"late_probe_{binding.round}", state)
            return
        if probe["state"] != "executing":  # 已接续或已判失败：迟到的回写不再触发接续
            return
        probe["state"] = state
        probe["note"] = note
        self.store.save_probe(ticket_id, probe)

    def _resume_after_probe(self, ticket_id: str, probe: Mapping[str, Any], now: datetime) -> Dispatch | None:
        ticket = self.store.ticket(ticket_id)
        if ticket is None:
            return None
        effective_at = datetime.fromisoformat(probe["effective_at"])
        entity = probe.get("entity") or self._entity_of(ticket)  # D61：读观测层用实体 ID，不是 event_id
        after = list(self.sensor_readings(entity, effective_at, now)) if entity else []
        baseline = list(probe.get("baseline") or [])
        fresh = [r for r in after if r.get("at") and datetime.fromisoformat(str(r["at"])) > effective_at
                 and str(r.get("quality", "ok")).lower() in ("ok", "good", "okay")]
        incomplete = probe.get("state") != "executing" or not fresh
        note = "试探未完成" if incomplete else f"试探窗口内取到 {len(fresh)} 条新读数"
        domains = self._ticket_weights(ticket_id, self._ticket_event(ticket_id))
        proposal = probe["proposal"]
        context = {"probe": {"actions": proposal.get("actions"), "hypothesis": proposal.get("hypothesis"),
                             "expectations": proposal.get("expectations"), "window_min": proposal.get("window_min"),
                             "effective_at": probe["effective_at"], "window_end": probe.get("window_end"),
                             "result": probe.get("state"), "note": probe.get("note", ""), "status": note},
                   "probe_incomplete": incomplete, "readings_before": baseline, "readings_after": fresh,
                   "events_during_probe": list(probe.get("events") or ())}
        investigation = self._investigate(ticket, at=now, domains=domains, round_=ticket.round + 1, reason="probe", context=context)
        self.store.save_probe(ticket_id, dict(probe) | {"state": "done", "readings": fresh, "note": note})
        return self._after_investigation(ticket, investigation, reason="probe")

    def sensor_readings(self, entity: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """观测层读数（router.readings，B22），其次是窗口内事件自带的读数；都没有时明说不可用，不编造。"""
        fn = self._maybe(self.readings, "readings", "router.readings")
        rows = [dict(r) for r in self._readings_rows(fn, entity, start, end)] if fn is not None else []
        if rows:
            return rows
        found = self.store.last_event_for_entity(entity) or {}
        reading = found.get("payload", {}).get("reading")
        at = (reading.get("at") or found.get("at")) if isinstance(reading, dict) else None
        if at and start <= datetime.fromisoformat(at) <= end:
            return [dict(reading, at=at)]
        return [{"at": end.isoformat(), "value": None, "quality": UNAVAILABLE, "source": "none", "note": "观测层没有这段读数，不能当作证据"}]

    def _readings_rows(self, fn: Callable[..., Any], entity: str, start: datetime, end: datetime) -> Sequence[Mapping[str, Any]]:
        """读数钩子按运行限定：新实现收 `(entity, start, end, run_id)`，旧的三参实现照旧调用（#186/#201-6）。"""
        if _takes_run_id(fn):
            return fn(entity, start, end, self.run_id)
        return fn(entity, start, end)

    def sources(self, payload: Mapping[str, Any]) -> list[str]:
        """确定性调查的来源候选：router.sources（B25 find_sources）；图谱没接时由调查回退到事件实体。"""
        event = payload.get("event") or {}
        if not event.get("entity_id"):
            return []
        return list(self._call(None, "", "router.sources", event["entity_id"], event.get("event_type", "")) or ())

    def _entity_of(self, ticket: Ticket) -> str | None:
        if ticket.subscriptions:
            return ticket.subscriptions[0].entity_id
        event = self._ticket_event(ticket.ticket_id)
        return event.entity_id if event else None

    def _residual_transition(self, ticket_id: str, event: DayEvent) -> bool:
        """D61：只在关键读数残差“正常 → 报警”的跳变时开新一轮，持续报警不重复开。"""
        flag = bool(event.payload.get("residual_alarm"))
        explicit = event.payload.get("residual_transition") == "normal_alarm"
        previous = self.store.ticket_state(ticket_id, "residual_alarm") == "1"
        self.store.set_ticket_state(ticket_id, "residual_alarm", "1" if flag else "0")
        return explicit or (flag and not previous)

    def answer_question(self, question_id: str, answer: str) -> None:
        """B35 回答卡片回写答案；下一条需要时由调查运行注入。"""
        self.store.answer(question_id, answer)

    def _ticket_event(self, ticket_id: str) -> DayEvent | None:
        ticket = self.store.ticket(ticket_id)
        if ticket is None or not ticket.event_ids:
            return None
        found = self.store.event(ticket.event_ids[-1])
        return found["event"] if found else None


    # ---------------------------------------------------------------- 派发（交 B29）

    def _dispatch(self, ticket: Ticket, trigger_at: datetime, *, reason: str) -> Dispatch:
        round_ = ticket.round + 1
        binding = RunBinding(run_id=self.run_id, knowledge_version=self.knowledge_version, ticket_id=ticket.ticket_id,
                             round=round_, state_version=self._state_version(), trigger_at=trigger_at)
        updated = ticket.model_copy(update={"round": round_, "status": "open", "closed_at": None})
        self.store.save_ticket(updated, run_id=self.run_id, running=True)
        self.store.save_round(binding, updated.experts, reason)
        handlers = self.registry.hooks_for(EXPERTS_HOOK)
        if handlers:
            for handler in handlers:
                handler(updated, binding, updated.experts)
        elif self.standin is not None:
            self.standin(self, updated, binding)
        else:  # 没有会诊模块：只登记派发，等 B29 接上；不代表专家已经跑过
            self.store.set_running(ticket.ticket_id, False)
        return Dispatch(updated, binding, updated.experts)

    def complete_round(self, ticket_id: str, round_: int) -> Dispatch | None:
        """本轮结束：调用提案就绪钩子；有排队的后续事件就开下一轮。由会诊（B29）在各位专家结果到齐后调用。

        先核对绑定、轮次与完成状态：旧轮、重复完成、分叉前父运行的回调只入历史，不改状态、不调钩子。
        """
        ticket = self.store.ticket(ticket_id)
        if ticket is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件单 {ticket_id} 不存在")
        late = self._late_reason(ticket, round_)
        if late is None and not self.store.claim_round(ticket_id, round_):
            late = "duplicate"
        if late is not None:  # 绑定不符的结果只入历史，不执行、不回写
            self.store.set_ticket_state(ticket_id, f"late_round_{round_}", late)
            return None
        self.store.set_running(ticket_id, False)
        proposals = self.sink.list(ticket_id, round_)
        async_decision = False
        for hook in self.registry.hooks_for(PROPOSALS_READY_HOOK):
            owner = getattr(hook, "__self__", None)
            result = hook(ticket_id, round_)
            if self.mode == "live" and getattr(owner, "async_decisions", False) and result is None:
                async_decision = True
        replied = [p for p in proposals if p.proposal_id and self.sink.replied(p.proposal_id)]
        if len({p.diagnosis for p in replied if p.diagnosis}) > 1:  # 量化不了的分歧（根因判断不同）
            for hook in self.registry.hooks_for(REPLIES_READY_HOOK):
                owner = getattr(hook, "__self__", None)
                result = hook(ticket_id, round_)
                if self.mode == "live" and getattr(owner, "async_decisions", False) and result is None:
                    async_decision = True
        # 就绪钩子（B47/B28）可能已把事件单写成 waiting/blocked：重读后再决定终态（#168 F1）
        ticket = self.store.ticket(ticket_id) or ticket
        trigger = self.store.round_trigger(ticket_id, round_)
        queued = [(eid, at) for eid, at in self.store.events_of(ticket_id) if trigger is None or at > trigger]
        residual = self.store.ticket_state(ticket_id, "residual_pending")
        if residual:  # 专家运行期间来的残差跳变：下一轮是慢通道调查，排队的事件一并带上
            self.store.set_ticket_state(ticket_id, "residual_pending", "")
            event = self.store.event(residual)["event"]
            return self._residual_round(ticket, event, max([event.at, *(at for _, at in queued)]))
        if queued:  # 专家运行期间并入的事件排到下一次
            return self._dispatch(ticket, max(at for _, at in queued), reason="stream_next_round")
        status = ticket.status if async_decision else ("resolved" if proposals and ticket.status == "open" else ticket.status)

        self.store.save_ticket(ticket.model_copy(update={"status": status, "closed_at": None}), run_id=self.run_id, running=False)
        if self.config.report and proposals:
            record = {"summary": ticket.summary, "round": round_, "proposals": [p.proposal_id for p in proposals]}
            self.store.add_pending(self._pending_id(ticket_id, "report", ticket.opened_at, record), at=ticket.opened_at,
                                   kind="report", ticket_id=ticket_id, record=record)
        return None

    def _late_reason(self, ticket: Ticket, round_: int) -> str | None:
        """核对事件单当前轮次与绑定：已关闭、不是当前轮、或派发时的运行与知识版本已换（分叉）都算迟到。"""
        if ticket.status == "closed":
            return "closed"
        if round_ != ticket.round or (bound := self.store.round_binding(ticket.ticket_id, round_)) is None:
            return "stale_round"
        return None if bound == (self.run_id, self.knowledge_version) else "other_run"

    # ---------------------------------------------------------------- 合批与主 Agent

    def flush(self) -> Batch | None:
        """把积压的待汇报结论合成一批，调一次主 Agent；失败时降级确定性研判，pending 照常取走。"""
        items = self._ordered_pending()
        if not items:
            return None
        first = self.store.ticket(items[0]["ticket_id"])
        binding = RunBinding(run_id=self.run_id, knowledge_version=self.knowledge_version, ticket_id=items[0]["ticket_id"],
                             round=max(first.round, 1) if first else 1, state_version=self._state_version(),
                             trigger_at=max(datetime.fromisoformat(i["at"]) for i in items))
        payload_items = [{"kind": i["kind"], "ticket_id": i["ticket_id"], **i["record"]} for i in items]
        payload = {"query": compose_query(payload_items), "as_of": binding.trigger_at.isoformat(), "items": payload_items}
        source, mode = None, "deterministic"
        result: Mapping[str, Any] | None = None
        status, terminal_reason, latency_ms = "finished", None, 0
        failure: str | None = None
        try:
            source, mode = self._source(TRIAGE_KEY)
            result = source.get(AgentResultKind.triage, binding, payload)
            status = str(result.get("status") or "finished")
            terminal_reason = result.get("terminal_reason")
            latency_ms = int(result.get("latency_ms") or 0)
            if status != "finished":
                failure = f"主 Agent 汇报未完成（{status}，{terminal_reason or '无终态原因'}）"
            elif not str(result.get("final_answer") or "").strip():
                failure = "主 Agent 汇报没有回答"
        except (PlantkgError, ValueError, TypeError) as exc:
            failure = f"主 Agent 汇报失败（{str(exc)[:160]}）"
        if failure is not None:
            log.warning("合批降级为确定性研判：%s", failure)
            fallback = self.registry.mode_ports.get((AgentResultSource, "deterministic", TRIAGE_KEY))
            try:
                if fallback is not None and fallback is not source:
                    result = fallback.get(AgentResultKind.triage, binding, payload)
                    source, mode = fallback, "deterministic"
                else:
                    result = {"final_answer": "；".join(f"{i['ticket_id']}：{i['record'].get('summary', '')}" for i in items)[:500]}
            except (PlantkgError, ValueError, TypeError) as exc:
                log.warning("确定性研判也失败，直接用待汇报事项拼句：%s", exc)
                result = {"final_answer": "；".join(f"{i['ticket_id']}：{i['record'].get('summary', '')}" for i in items)[:500]}
            status = "failed"
            latency_ms = 0
        coverage_gap = failure is not None or mode != self.mode or bool(result.get("coverage_gap"))
        record = self._record_run(binding, source, mode, coverage_gap, result, status=status,
                                  terminal_reason=terminal_reason, latency_ms=latency_ms)
        self.store.take_pending([i["item_id"] for i in items])
        return Batch(record.agent_run_id, record.source, coverage_gap, str(result.get("final_answer") or "")[:500],
                     tuple(dict.fromkeys(i["ticket_id"] for i in items)), failure)

    def _record_run(self, binding: RunBinding, source: Any, mode: str, coverage_gap: bool,
                    result: Mapping[str, Any], *, kind: AgentResultKind = AgentResultKind.triage,
                    status: str | None = None, terminal_reason: Any = None,
                    latency_ms: int | None = None) -> AgentRunRecord:
        self._seq += 1
        now = self._wall()
        latency = max(0, int(latency_ms if latency_ms is not None else result.get("latency_ms") or 0))
        actual = str(status or result.get("status") or "finished")
        status = actual if actual in ("finished", "failed", "timeout", "blocked", "cancelled") else "finished"
        try:
            reason = TerminalReason(terminal_reason) if terminal_reason is not None else None
        except ValueError:
            reason = None
        origin = result.get("source", getattr(source, "source_kind", None))
        # 实际执行的是确定性实现：与 live 回退同一口径记 deterministic，不再记 evaluation+deterministic（#243）
        deterministic = (isinstance(source, (DeterministicTriage, DeterministicInvestigation)) or mode == "deterministic"
                         or origin == "deterministic")
        try:
            run_mode = RunMode.deterministic if deterministic else RunMode(mode)
        except ValueError:
            run_mode = RunMode(self.mode)
        if deterministic:
            source_name = "deterministic"
        elif origin == "nexent" or mode not in ("trial", "evaluation"):
            source_name = "nexent"  # 本次采集或 live 实调，耗时是真实墙钟
        else:
            source_name = "cache"
            latency = 0  # 重放没有等待
        record = AgentRunRecord(
            agent_run_id=f"run-{kind.value}-{self._seq:04d}", kind=kind, agent_name="plantkg_main",
            run_mode=run_mode, source=source_name,
            binding=binding,
            timing=AgentTiming(origin_event_at=binding.trigger_at, trigger_at=binding.trigger_at,
                               effective_at=binding.trigger_at + timedelta(minutes=1),
                               requested_at=now - timedelta(milliseconds=latency), returned_at=now, latency_ms=latency),
            status=status, terminal_reason=reason,
            coverage_gap=coverage_gap or bool(result.get("coverage_gap")) or (deterministic and self.mode != "deterministic"),
            final_answer=str(result.get("final_answer") or "")[:2000] or None)
        self.runs.append(record)
        self.store.save_agent_run(record)
        return record

    # ---------------------------------------------------------------- 值班人员消息与推进步

    def _instruction(self, event: DayEvent) -> Handled:
        """值班人员一句话指令：最高优先级，立即触发一次运行；转临时规则交给 B47 登记的处理器（本卡不 import B47）。

        处理器返回 False 表示这次指令没收下（如解析不出区域或设定值）；单个处理器异常不打断值守循环，
        全部处理器都没收下时按未处理记录。
        """
        handlers = self.registry.hooks_for(INSTRUCTION_HOOK)
        # 指令不开事件单，处理器用实际接收路由的运行定位场景。原始事件仍原样入库。
        routed = event.model_copy(update={"payload": {**event.payload, "run_id": self.run_id}})
        served = False
        for handler in handlers:
            try:
                served = (handler(routed) is not False) or served
            except Exception:  # noqa: BLE001 — 单条坏指令不打断值守循环
                log.exception("值班指令 %s 的处理器 %r 执行失败", event.event_id, handler)
        result = "instruction" if served else "instruction_unhandled"
        self.store.record_event(event, result)
        return Handled(event.event_id, result, None, (), None, self.flush())

    def step(self, now: datetime) -> None:
        """每个推进步（回放 15 分钟、场景 1 分钟）：调用 B29 预演登记的钩子，到期试探发起接续调查，再合批。"""
        for hook in self.registry.hooks_for(STEP_HOOK):
            hook(self.run_id, now)
        for item in self.store.due_probes(now):
            self._resume_after_probe(item["ticket_id"], item["probe"], now)
        self.flush()

    # ---------------------------------------------------------------- 关闭与分叉

    def close(self, ticket_id: str, at: datetime, summary: str = "") -> None:
        """事件单关闭：退订该事件流，后续事件不再直通到这里。"""
        ticket = self.store.ticket(ticket_id)
        if ticket is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件单 {ticket_id} 不存在")
        self.store.close_ticket(ticket_id, at, summary or ticket.summary)

    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None:
        """知识发布分叉：未关闭事件单与订阅交接到子运行，并标记需按新知识版本重新派发（重派 trigger_at 取 forked_at）。"""
        self.run_id = child_run.run_id
        self._forked_at = at
        for ticket in self.store.open_tickets():
            self.store.save_ticket(ticket, run_id=child_run.run_id, running=False)

    def resume_after_fork(self, at: datetime | None = None, knowledge_version: int | None = None) -> list[Dispatch]:
        """按新知识版本重新派发未关闭事件单；新一轮 trigger_at 取 forked_at。

        待试探的事件单不重派，等窗口结束由接续调查接手；试探已接续完（done）的照常重派。
        已派工单（urgent）的按 D55 立即工单，不重新交给会诊。
        """
        forked_at = at or self._forked_at
        if forked_at is None:
            raise PlantkgError(ErrorCode.PRECONDITION, "没有待重派的分叉（先调 on_fork）")
        if knowledge_version is not None:
            self.knowledge_version = knowledge_version
        self._forked_at = None
        dispatches = []
        for ticket in self.store.open_tickets():
            probe = self.store.probe_of(ticket.ticket_id)
            if self.store.channel_of(ticket.ticket_id) == "urgent" or (probe and probe.get("state") != "done"):
                continue
            if ticket.status == "waiting":
                continue
            dispatches.append(self._dispatch(ticket, forked_at, reason="fork"))
        return dispatches


def install(registry: Registry, writer: Any, plant_id: str, *, run_id: str, knowledge_version: int = 1, **options: Any) -> Router:
    """装配：登记 ProposalSink、慢通道工具、plantkg_recall、分叉钩子与确定性研判/调查；返回值守程序。"""
    from plantkg.agent.router.tools import QuestionActionHandler, make_tools

    store = RouterStore(writer, plant_id)
    sink = SqliteProposalSink(writer, plant_id)
    registry.register(ProposalSink, sink)
    router = Router(store, registry, plant_id=plant_id, run_id=run_id, knowledge_version=knowledge_version, sink=sink, **options)
    tools = make_tools(router)
    registry.register_tool("recall", router.recall)
    registry.register_tool("ask_operator", tools["ask_operator"])
    registry.register_tool("escalate", tools["escalate"])
    registry.register_tool("trial", tools["trial"], default=True)
    # B35 回答卡：确认 question 待确认动作 = 把答案（reason）回写给提问的事件单
    registry.register_action_handler("question", QuestionActionHandler(router))
    registry.register_hook(FORK_HOOK, router)
    if (AgentResultSource, "deterministic", TRIAGE_KEY) not in registry.mode_ports:
        registry.register_mode(AgentResultSource, "deterministic", DeterministicTriage(), key=TRIAGE_KEY)
    if (AgentResultSource, "deterministic", INVESTIGATE_KEY) not in registry.mode_ports:
        registry.register_mode(AgentResultSource, "deterministic", DeterministicInvestigation(router.sources), key=INVESTIGATE_KEY)
    return router

