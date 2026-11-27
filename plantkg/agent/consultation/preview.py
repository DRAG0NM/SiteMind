"""预演（B29）：未来 30 分钟可预见冲突的后台确定性提案与状态指纹缓存。

值守循环每个推进步（场景 1 分钟、回放 15 分钟）经 B27 的每步钩子调用这里。规则是程序化的：
早高峰、生产计划里的大负荷启动、峰段开始这类能提前知道的冲突，叠加储能 SOC 低或变压器负载率高才命中；
命中就开一张预演事件单，在后台只算专家确定性提案并按当时的状态指纹缓存，不做裁决、不执行、不对人说话。
预演只预判、不复用：真实事件到达时一律按真实事件现场算提案，算完再和匹配到的预演对比动作，
结果写成事件单状态 preview_check_<轮次>，供报告说明“与预演一致”或偏差在哪。
预演不跑 LLM，事件单只是记录，不算值守层的打开事件单。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.agent.consultation.sources import EXPERT_KEY
from plantkg.agent.router.router import EVENT_DOMAINS
from plantkg.core.contracts import AgentResultKind, DayEvent, ExpertProposal, RunBinding, Ticket
from plantkg.core.ports import AgentResultSource
from plantkg.core.versions import utcnow

# 预演输入：装配方登记后，主路由（无场景运行）也能按真实数据做预演
PREVIEW_SIGNALS_HOOK = "consultation.preview_signals"  # fn(router, at) -> Sequence[PreviewSignal]
PREVIEW_STATE_HOOK = "consultation.preview_state"      # fn(router, at) -> Mapping[str, float]
PREVIEW_CHECK_KEY = "preview_check"                    # 事件单状态键前缀：preview_check_<轮次> 记与现场提案的对比
WAIT_S = 1.0                                            # 对比时最多等后台计算这么久，超时只记预演还没算完
MAX_CASES = 32


@dataclass(frozen=True)
class PreviewSignal:
    """一条可预见的冲突。entity_id 为预演事件的对象（图谱实体 ID），没有对象时为空。
    payload 保留剧本已知的事件载荷（node、discharge_kw 等），供预演按当时已知条件算提案。"""

    kind: str  # morning_peak / plan_start / peak_start
    at: datetime
    event_type: str
    experts: tuple[str, ...]
    summary: str
    entity_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _Case:
    key: str
    signal: PreviewSignal
    check_at: datetime
    fingerprint: dict[str, float]
    event: DayEvent
    drafts: tuple[ExpertProposal, ...] = ()
    compute_ms: int | None = None
    error: str | None = None
    ticket_id: str | None = None  # 预演事件单：对比结果里指回它，方便查记录
    state: str = "computing"  # computing / ready / failed
    ready: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class PreviewMatch:
    """匹配到的一条预演，只用于和现场提案对比，不进入裁决。note 非空表示预演没算成。"""

    signal: PreviewSignal
    drafts: tuple[ExpertProposal, ...]
    compute_ms: int
    fingerprint: dict[str, float]
    ticket_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class PreviewCheck:
    """现场提案与预演的对比结果：matched 是否匹配到预演，same_actions 动作类型与目标是否一致。"""

    matched: bool
    same_actions: bool = False
    diff: tuple[str, ...] = ()
    preview_ticket: str | None = None
    signal: PreviewSignal | None = None


class Preview:
    """一个值守程序的预演缓存。signals/state 由装配方给：场景运行读世界，主路由读登记的能力钩子。"""

    def __init__(self, router: Any, *, signals: Callable[[datetime], Sequence[PreviewSignal]],
                 state: Callable[[datetime], Mapping[str, float]], payload: Callable[[DayEvent, RunBinding], Mapping[str, Any]],
                 state_version: Callable[[], int], wall: Callable[[], datetime] = utcnow,
                 tz: ZoneInfo = ZoneInfo("Asia/Shanghai"),
                 binding_at: Callable[[_Case], datetime] | None = None) -> None:
        self.router, self.signals, self.state, self.payload = router, signals, state, payload
        self.state_version, self.wall, self.tz = state_version, wall, tz
        self.binding_at = binding_at or (lambda case: case.signal.at)
        self._cases: dict[str, _Case] = {}
        self._lock = threading.Lock()
        self.horizon = 30
        self.state_nature = "unknown"

    # ---------------------------------------------------------------- 每个推进步
    def observe(self, now: datetime) -> None:
        policy = self.router.policy
        horizon = int(getattr(policy, "preview_horizon_min", 30))
        self.horizon = horizon
        if not self._triggered(self._safe_state(now), policy):
            return
        for signal in self.signals(now):
            if not signal.experts or not (0 <= (signal.at - now).total_seconds() <= horizon * 60):
                continue
            key = f"{signal.kind}|{signal.at.isoformat()}|{signal.event_type}|{signal.entity_id or ''}"
            with self._lock:
                if key in self._cases or len(self._cases) >= MAX_CASES:
                    continue
                case = _Case(key=key, signal=signal, check_at=now, fingerprint=self._safe_state(now), event=_event_for(signal))
                self._cases[key] = case
            threading.Thread(target=self._compute, args=(case,), name=f"preview-{key}", daemon=True).start()
        self._prune(now, horizon)

    def ready(self) -> bool:
        with self._lock:
            return all(case.state != "computing" for case in self._cases.values())

    def wait(self, timeout: float = 10.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready():
                return True
            time.sleep(0.02)
        return self.ready()

    # ---------------------------------------------------------------- 事件到达后的对比
    def match(self, event: DayEvent | None) -> PreviewMatch | None:
        """按事件类型、对象与时间窗找最近的一条预演，只给对比用，不影响现场提案。"""
        if event is None:
            return None
        with self._lock:
            candidates = [c for c in self._cases.values() if self._matches(c, event)]
        if not candidates:
            return None
        case = min(candidates, key=lambda c: abs((event.at - c.signal.at).total_seconds()))
        found = PreviewMatch(signal=case.signal, drafts=(), compute_ms=case.compute_ms or 0,
                             fingerprint=dict(case.fingerprint), ticket_id=case.ticket_id)
        if not case.ready.wait(WAIT_S):
            return replace(found, note="预演还在计算")
        if case.state != "ready":
            return replace(found, note=f"预演计算失败：{case.error}")
        return replace(found, drafts=case.drafts)

    # ---------------------------------------------------------------- 后台计算
    def _compute(self, case: _Case) -> None:
        router = self.router
        started = time.monotonic()
        ticket: Ticket | None = None
        try:
            ticket = Ticket(ticket_id=router.store.next_ticket_id(), opened_at=case.signal.at, status="open", round=1,
                            rehearsal=True, experts=case.signal.experts, summary=f"预演：{case.signal.summary}")
            router.store.save_ticket(ticket, run_id=router.run_id, running=False)
            case.ticket_id = ticket.ticket_id
            binding = RunBinding(run_id=router.run_id, knowledge_version=router.knowledge_version, ticket_id=ticket.ticket_id,
                                 round=1, state_version=self.state_version(), trigger_at=self.binding_at(case))
            case.drafts = tuple(self._drafts(binding, case))
            case.state = "ready"
        except Exception as exc:  # noqa: BLE001 — 预演失败只留下记录，不影响值守循环
            case.state, case.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            case.compute_ms = int((time.monotonic() - started) * 1000)
            if ticket is not None:
                try:
                    router.store.close_ticket(ticket.ticket_id, case.signal.at, f"预演：{case.signal.summary}")
                except Exception:  # noqa: BLE001 — 记录失败不改预演结果
                    pass
            case.ready.set()

    def _drafts(self, binding: RunBinding, case: _Case) -> list[ExpertProposal]:
        source = self.router.registry.resolve_mode(AgentResultSource, "deterministic", EXPERT_KEY)
        payload = dict(self.payload(case.event, binding))

        def one(expert: str) -> ExpertProposal:
            result = source.get(AgentResultKind.expert, binding, payload | {"expert": expert})
            if result.get("proposal") is None:
                raise ValueError(f"专家 {expert} 的确定性结果源没有给出提案")
            return ExpertProposal.model_validate(result["proposal"])

        with ThreadPoolExecutor(max_workers=max(1, min(len(case.signal.experts), 4)), thread_name_prefix="preview-expert") as pool:
            return list(pool.map(one, case.signal.experts))

    # ---------------------------------------------------------------- 输入与判定
    def _triggered(self, state: Mapping[str, float], policy: Any) -> bool:
        """储能 SOC 低或变压器负载率高才值得预演；两个量都没有时不猜。"""
        soc, ratio = state.get("soc"), state.get("transformer_ratio")
        return (soc is not None and soc <= float(getattr(policy, "preview_soc_low", 0.3))) or \
               (ratio is not None and ratio >= float(getattr(policy, "preview_transformer_high", 0.85)))

    def _safe_state(self, at: datetime) -> dict[str, float]:
        try:
            found = dict(self.state(at))
            if isinstance(found.get("nature"), str):
                self.state_nature = found["nature"]
            return {k: float(v) for k, v in found.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        except Exception:  # noqa: BLE001 — 预演输入不可用时不做预演
            return {}

    def _matches(self, case: _Case, event: DayEvent) -> bool:
        if case.signal.event_type != event.event_type:
            return False
        if case.signal.entity_id and event.entity_id != case.signal.entity_id:
            return False
        return 0 <= (event.at - case.signal.at).total_seconds() <= self.horizon * 60

    def _prune(self, now: datetime, horizon: int) -> None:
        with self._lock:
            expired = [c for c in self._cases.values()
                       if now > c.signal.at + timedelta(minutes=max(horizon, 1)) and c.state != "computing"]
            for case in expired:
                self._cases.pop(case.key, None)


# ---------------------------------------------------------------- 场景运行与主路由的输入


def scenario_signals(runner: Any, tz: ZoneInfo, horizon_min: int) -> list[PreviewSignal]:
    """场景运行的可预见冲突：早高峰规则 + 剧本里 30 分钟内的大负荷事件 + 分时电价峰段开始。"""
    now = runner.now
    out = morning_peak(now, tz, horizon_min)
    for at, item in _queued_events(runner, now, horizon_min):
        experts = EVENT_DOMAINS.get(item.type, ())
        if not experts or item.severity <= 0:
            continue
        entity = _eid(runner, item.entity_id, item.source)
        out.append(PreviewSignal(kind="plan_start", at=at, event_type=item.type, experts=tuple(experts),
                                 summary=f"生产计划的大负荷启动：{item.summary or item.type}", entity_id=entity,
                                 payload=dict(item.payload)))
    out.extend(_peak_start(runner, now, horizon_min))
    return out


def scenario_state(runner: Any) -> dict[str, float]:
    """场景运行的关键量：储能 SOC 与 TX1 负载率（取最近一分钟账本）。"""
    state: dict[str, float] = {}
    try:
        state["soc"] = round(float(runner.energy) / float(runner.world.storage.capacity_kwh), 4)
    except Exception:  # noqa: BLE001 — 没有储能就不预演 SOC 那条线
        pass
    try:
        limit = float(runner.world.grid.limits.get("TX1") or 0.0)
        ledger = list(runner.trace.ledger)
        if limit:
            flow = float(ledger[-1].flows_kw.get("TX1") or 0.0)
            state["transformer_ratio"] = round(flow / limit, 4)
    except Exception:  # noqa: BLE001 — 还没有账本时只按 SOC 判
        pass
    return state


def morning_peak(now: datetime, tz: ZoneInfo, horizon_min: int) -> list[PreviewSignal]:
    """早高峰规则（configured）：当地 08:00 开始，检查窗口内给出一次信号。"""
    local = now.astimezone(tz)
    peak = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if peak < local:
        peak += timedelta(days=1)
    if not (0 <= (peak - local).total_seconds() <= horizon_min * 60):
        return []
    return [PreviewSignal(kind="morning_peak", at=peak.astimezone(timezone.utc), event_type="load_spike",
                          experts=("energy", "hvac", "elevator", "equipment"),
                          summary="早高峰：行政楼人梯拥堵与车间大负荷启动叠加，逼近配电容量")]


def _queued_events(runner: Any, now: datetime, horizon_min: int) -> list[tuple[datetime, Any]]:
    """剧本队列里 30 分钟内要发生的事件（只读快照，不消费）。"""
    try:
        queued = list(runner.queue)
    except Exception:  # noqa: BLE001
        return []
    return [(at, item) for kind, at, item in queued if kind == "event" and 0 <= (at - now).total_seconds() <= horizon_min * 60]


def _eid(runner: Any, entity: str | None, source: str) -> str | None:
    if not entity or source == "sim_limit":
        return entity
    try:
        return runner.world.index.eid(entity)
    except Exception:  # noqa: BLE001
        return entity


def _peak_start(runner: Any, now: datetime, horizon_min: int) -> list[PreviewSignal]:
    """分时电价从非峰段进入峰段的时刻（规则已发布的价格时段表）。"""
    try:
        inputs = runner.world.inputs
        slots, periods = list(inputs.loads.slots), list(inputs.price_periods)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for index in range(1, min(len(slots), len(periods))):
        at, period = slots[index], str(periods[index])
        if not (0 <= (at - now).total_seconds() <= horizon_min * 60):
            continue
        if "峰" in period and "峰" not in str(periods[index - 1]):
            out.append(PreviewSignal(kind="peak_start", at=at, event_type="sim_limit", experts=("energy", "hvac"),
                                     summary=f"{period}开始，储能与暖通提前让出容量"))
    return out


def _event_for(signal: PreviewSignal) -> DayEvent:
    return DayEvent(event_id=f"preview://{signal.kind}-{signal.at.strftime('%Y%m%dT%H%M%S')}", at=signal.at,
                    event_type=signal.event_type, source="script", entity_id=signal.entity_id, severity=3,
                    summary=signal.summary, payload=dict(signal.payload), nature="synthetic")


# ---------------------------------------------------------------- 现场提案与预演的对比


def compare_actions(before: Sequence[ExpertProposal], after: Sequence[ExpertProposal]) -> tuple[bool, list[str]]:
    """逐位专家比较动作类型、目标、执行参数（功率、削减比例、设定值等）与持续时长：一致返回 (True, [])，不一致给出简短说明。

    起止时刻随事件时刻平移，不比；同一专家对同一对象的同名动作可有多个，按（时长、参数）排序后逐个比。
    """
    left, right = _grouped(before), _grouped(after)
    diff: list[str] = []
    for expert in sorted(set(left) | set(right)):
        if expert not in right:
            diff.append(f"{expert}：预演有、事件没有")
        elif expert not in left:
            diff.append(f"{expert}：事件多出、预演没有")
        elif {k: len(v) for k, v in left[expert].items()} != {k: len(v) for k, v in right[expert].items()}:
            diff.append(f"{expert}：预演 {_actions_text(set(left[expert]))}，事件 {_actions_text(set(right[expert]))}")
        else:
            for (command, entity), items in sorted(right[expert].items()):
                for (min_a, params_a), (min_b, params_b) in zip(left[expert][(command, entity)], items):
                    changes = _params_text(params_a, params_b)
                    if min_a != min_b:
                        changes.append(f"时长 {min_a:g}→{min_b:g} 分钟")
                    if changes:
                        diff.append(f"{expert}：{command}→{entity.rsplit('/', 1)[-1]} {'、'.join(changes)}")
    return not diff, diff


def _grouped(proposals: Sequence[ExpertProposal]) -> dict[str, dict[tuple[str, str], list[tuple[float, dict[str, Any]]]]]:
    out: dict[str, dict[tuple[str, str], list[tuple[float, dict[str, Any]]]]] = {}
    for p in proposals:
        group = out.setdefault(p.expert, {})
        for a in p.actions:
            minutes = (a.interval.end - a.interval.start).total_seconds() / 60
            group.setdefault(_action_key(a), []).append((minutes, dict(a.params)))
    for group in out.values():
        for items in group.values():
            items.sort(key=lambda item: (item[0], repr(sorted(item[1].items()))))
    return out


def _params_text(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [f"{key} {before.get(key, '无')}→{after.get(key, '无')}"
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]


def _action_key(action: Any) -> tuple[str, str]:
    return (str(action.command), str(action.entity_id))


def _actions_text(actions: set[tuple[str, str]]) -> str:
    if not actions:
        return "无动作"
    shown = [f"{command}→{entity.rsplit('/', 1)[-1]}" for command, entity in sorted(actions)[:3]]
    return "、".join(shown) + ("…" if len(actions) > 3 else "")

