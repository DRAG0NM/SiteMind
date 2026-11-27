"""跨域会诊（B29）：按 B27 的派发结果并行发起专家运行、解析回答挂到提案上、记录分歧。

- 专家运行经 AgentResultSource（kind=expert）取得：live 由 B23 登记，trial/evaluation 与确定性回退
  由本卡登记；每位专家独立运行、同一时刻最多 4 路，回答解析失败只少诊断，提案照常进入裁决。
- 先动作、后补依据：live 专家经 propose 工具落库提案后，本卡立即完成本轮（B27 的提案就绪钩子），
  回答晚到只挂到提案上补诊断与依据，不挡裁决；确定性来源的提案与结果同一次调用，全部返回后完成本轮。
- 回答在挂接前核对绑定与事件单当前轮次：运行、轮次或知识版本不符的标过期，只入历史（store 状态键），不回写提案。
- 一位专家超时或失败：事件单标受阻，其余专家照常；live 失败后用确定性提案兜底，动作照常可裁决。
- 专家结论冲突时原样写进事件单状态（conflicts_round_<轮次>）并记证据事件，供 B28 第 3 层使用。
- 预演：每个推进步经 Preview 检查未来冲突并在后台算确定性提案；真实事件一律按现场条件算提案，
  算完再和匹配到的预演对比动作，写事件单状态 preview_check_<轮次>，报告里说明与预演一致或偏差在哪。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.agent.consultation.context import ConsultationContext
from plantkg.agent.consultation.fallback import DirectApplyFallback
from plantkg.agent.consultation.preview import (
    PREVIEW_CHECK_KEY, PREVIEW_SIGNALS_HOOK, PREVIEW_STATE_HOOK, Preview, PreviewCheck, compare_actions, morning_peak,
    scenario_signals, scenario_state,
)
from plantkg.agent.consultation.replies import parse_reply, stale_reason
from plantkg.agent.consultation.sources import EXPERT_KEY, install_sources
from plantkg.agent.router.router import (
    EXPERTS_HOOK, PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, ROUTER_HOOK, SCENARIO_BIND_HOOK, STEP_HOOK,
)
from plantkg.core.contracts import (
    AgentResultKind,
    AgentRunRecord,
    AgentTiming,
    DayEvent,
    ErrorCode,
    EvidenceEvent,
    ExpertProposal,
    ExpertReply,
    PlantkgError,
    RunBinding,
    Ticket,
)
from plantkg.core.ports import AgentResultSource, EvidenceSink
from plantkg.core.versions import utcnow

log = logging.getLogger(__name__)

SERVICE_HOOK = "consultation.service"  # 装配好的会诊实例；演示与下游经注册表取用
ELEVATOR_CONTEXT_HOOK = "expert_context:elevator"  # 电梯专家 propose 工具取本轮事件与状态
HVAC_SITUATION_HOOK = "hvac_situation"             # 暖通专家 propose 工具取本轮区域视图
EQUIPMENT_READINGS_HOOK = "equipment.readings"     # 设备专家 propose 工具取本轮事件与读数
ENERGY_STATE_HOOK = "energy_state:"                # 能源专家 propose 工具按 run_id 取本轮事件与状态
MAX_PARALLEL = 4                        # 同一时刻最多 4 路专家独立运行（03 §3.0）
POLL_S = 0.02                           # live 等提案落库的轮询间隔（秒）
FAILED = frozenset({"failed", "timeout", "blocked", "cancelled"})
SAVE_ORDER_TIMEOUT_S = 30.0             # 等前位专家落库的上限；超时放弃固定顺序，避免整轮被拖住（#207 终审）


class _RoundSaveOrder:
    """一轮内按固定专家顺序落库：后位专家等前位专家落库或结束提案阶段。

    提案编号在落库事务里按 MAX(seq)+1 分配，并行专家谁先落库谁拿号；固定顺序后同一剧本的提案 ID、
    选中提案与冲突摘要可复现（#207 P2）。专家仍并行运行，只把落库排成固定顺序。
    """

    def __init__(self, experts: Sequence[str]) -> None:
        self.order = list(experts)
        self.index = {expert: i for i, expert in enumerate(self.order)}
        self.done: set[str] = set()
        self.cond = threading.Condition()

    def wait_turn(self, expert: str) -> None:
        i = self.index.get(expert)
        if i is None:
            return
        deadline = time.monotonic() + SAVE_ORDER_TIMEOUT_S
        with self.cond:
            while any(e not in self.done for e in self.order[:i]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # 有界等待：前位专家卡住时放弃顺序照常落库，不拖住整轮
                    log.warning("提案落库等固定顺序超时，%s 改为直接落库（前位专家未结束提案阶段）", expert)
                    return
                self.cond.wait(remaining)

    def finished(self, expert: str) -> None:
        with self.cond:
            self.done.add(expert)
            self.cond.notify_all()


def _replay_plan_signals(items: Sequence[Mapping[str, Any]], at: datetime, horizon_min: int) -> list[Any]:
    from plantkg.agent.consultation.preview import PreviewSignal

    out = []
    for item in items:
        plan, config = item["plan"], item["config"]
        operations = config.get("operations", {}).get("items", [])
        for action in plan.actions:
            if action.quantity != "operation_start" or not (0 <= (action.interval.start - at).total_seconds() <= horizon_min * 60):
                continue
            out.append(PreviewSignal(kind="plan_start", at=action.interval.start, event_type="load_spike",
                                     experts=("energy", "hvac"), summary="已批准生产计划的大负荷启动",
                                     entity_id=action.object, payload={"action_id": action.action_id,
                                     "operation_id": next((op["operation_id"] for op in operations
                                                            if op.get("entity_id") == action.object), None)}))
        slots = sorted(config.get("tariff", {}).get("slots", []), key=lambda slot: slot["start"])
        peak = max((slot["price_cny_per_kwh"] for slot in slots), default=None)
        for previous, slot in zip(slots, slots[1:]):
            starts = datetime.fromisoformat(slot["start"])
            if (slot["price_cny_per_kwh"] == peak and previous["price_cny_per_kwh"] < peak
                    and 0 <= (starts - at).total_seconds() <= horizon_min * 60):
                out.append(PreviewSignal(kind="peak_start", at=starts, event_type="sim_limit",
                                         experts=("energy", "hvac"), summary="固定计划电价进入当日峰价时段"))
    return out


@dataclass(frozen=True)
class Lane:
    """一条专家泳道的结果：耗时、来源与结构化报告，供展示与冒烟测试。"""

    expert: str
    started_at: datetime
    returned_at: datetime
    elapsed_ms: int
    status: str
    source: str
    proposal_ids: tuple[str, ...] = ()
    diagnosis: str | None = None
    reply_status: str | None = None
    error: str | None = None


class Consultation:
    """一个注册表上的会诊：可绑定多个值守程序（主路由 + 每次场景运行一个）。"""

    def __init__(self, registry: Any, *, settings: Any = None, wall: Any = utcnow) -> None:
        self.registry, self.settings, self._wall = registry, settings, wall
        self._bindings: list[tuple[Any, Any]] = []  # (Router, runner | None)
        self._previews: dict[int, Preview] = {}
        self._context = ConsultationContext()  # 本轮现场：live 专家的 propose 工具按绑定取
        self._energy_hooks: set[str] = set()  # 已按 run_id 登记能源状态钩子的运行
        self._context_lock = threading.Lock()
        self._active: dict[str, tuple[Any, Any]] = {}
        self._reply_jobs: list[threading.Event] = []  # live 回答的后台收集任务（wait_replies 用）
        self._reply_lock = threading.Lock()
        self.held_status: dict[str, str] = {}  # 占位消费者算出的事件单状态（待人处理时不是已解决）
        self.last_lanes: tuple[Lane, ...] = ()
        self.last_preview: PreviewCheck | None = None
        self.last_elapsed_ms = 0

    # ---------------------------------------------------------------- 装配
    def attach(self) -> None:
        """绑定装配时已经登记的主路由；场景运行的路由在 SCENARIO_BIND_HOOK 里绑定。"""
        for router in self.registry.hooks_for(ROUTER_HOOK):
            self.bind(router)

    def bind(self, router: Any, runner: Any = None) -> None:
        self._bindings = [(r, n) for r, n in self._bindings if r is not router] + [(router, runner)]
        self._previews[id(router)] = self._make_preview(router, runner)

    def on_scenario_end(self, runner: Any) -> None:
        """B17 的 SCENARIO_END_HOOK：场景跑到剧本结束，解绑该运行的路由与预演缓存，注销能源状态钩子。

        不等后台回答：结束后才到的回答走已有的迟到/挂接判定，这里只清理会累积的绑定与钩子。
        """
        removed = [router for router, bound in self._bindings if bound is runner]
        if not removed:
            return
        self._bindings = [(router, bound) for router, bound in self._bindings if bound is not runner]
        self._active = {tid: found for tid, found in self._active.items() if found[1] is not runner}
        with self._reply_lock:
            self._reply_jobs = [job for job in self._reply_jobs if not job.is_set()]
        for router in removed:
            self._previews.pop(id(router), None)
            run_id = getattr(router, "run_id", None)
            if run_id in self._energy_hooks:
                self._energy_hooks.discard(run_id)
                self.registry.unregister_hook(f"{ENERGY_STATE_HOOK}{run_id}", self.energy_state)

    def _make_preview(self, router: Any, runner: Any) -> Preview:
        tz = ZoneInfo(getattr(self.settings, "timezone", None) or "Asia/Shanghai")
        if runner is not None:
            try:
                tz = ZoneInfo(runner.world.site.plant_tz)
            except Exception:  # noqa: BLE001 — 站点时区不可用时保持配置时区
                pass
        if runner is not None:
            signals = lambda now: scenario_signals(runner, tz, self._horizon(router))  # noqa: E731
            state = lambda at: scenario_state(runner)  # noqa: E731
        else:
            signals = lambda now: self._hook_signals(router, now, tz)  # noqa: E731
            state = lambda at: self._hook_state(router, at)  # noqa: E731
        state_version = (lambda: runner.state.state_version) if runner is not None else (lambda: router._state_version())
        payload = lambda event, binding: self._payload(router, runner, event, binding)  # noqa: E731
        binding_at = (lambda case: case.check_at) if getattr(router, "_replay", False) else None
        return Preview(router, signals=signals, state=state, payload=payload, state_version=state_version,
                       wall=self._wall, tz=tz, binding_at=binding_at)

    def _hook_signals(self, router: Any, now: datetime, tz: ZoneInfo) -> list[Any]:
        horizon = self._horizon(router)
        signals = morning_peak(now, tz, horizon)
        for provider in self.registry.hooks_for(PREVIEW_SIGNALS_HOOK):
            try:
                signals.extend(provider(router, now))
            except Exception:  # noqa: BLE001 — 预演输入失败不打断值守循环
                continue
        return signals

    def _hook_state(self, router: Any, at: datetime) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for provider in self.registry.hooks_for(PREVIEW_STATE_HOOK):
            try:
                found = provider(router, at)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(found, Mapping):
                state.update({k: v for k, v in found.items() if isinstance(v, (int, float))})
                if isinstance(found.get("nature"), str):
                    state["nature"] = found["nature"]
        return state

    def _horizon(self, router: Any) -> int:
        return int(getattr(getattr(router, "policy", None), "preview_horizon_min", 30))

    # ---------------------------------------------------------------- 专家现场上下文
    def elevator_context(self, context: Any, binding: RunBinding,
                         focus: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """expert_context:elevator：本轮事件按 B20 确定性结果源的口径补时刻、单号与默认影响。"""
        found = self._context.get(binding)
        if found is None:
            return {}, {}
        event = dict(found.get("event") or {}) | {"at": binding.trigger_at, "ticket_id": binding.ticket_id}
        from plantkg.world.scenario.sims import DEFAULT_EFFECTS  # 与 ElevatorExpertSource.get 同口径：剧本没写影响时用默认影响
        event["payload"] = {"effects": DEFAULT_EFFECTS.get(event.get("event_type"), [])} | dict(event.get("payload") or {})
        return event, dict(found.get("state") or {}) | {"confirmed": bool(found.get("confirmed"))}

    def hvac_situation(self, context: Any, binding: RunBinding) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """hvac_situation：本轮区域视图；没有 situation 时返回 None，暖通专家走保守提案。"""
        found = self._context.get(binding)
        if found is None:
            return None
        situation = (found.get("state") or {}).get("situation")
        if situation is None:
            return None
        return dict(found.get("event") or {}), dict(situation) | {"confirmed": bool(found.get("confirmed"))}

    def equipment_readings(self, binding: RunBinding) -> dict[str, Any]:
        """equipment.readings：本轮事件与状态；查不到本轮时返回空，专家按事件单给保守结论。"""
        found = self._context.get(binding)
        if found is None:
            return {}
        state = dict(found.get("state") or {})
        state.setdefault("ids", found.get("ids") or {})
        state.setdefault("knowledge", sorted(found.get("knowledge") or ()))
        return {"event": dict(found.get("event") or {}), **state}

    def energy_state(self, binding: RunBinding) -> tuple[dict[str, Any], dict[str, Any]]:
        """energy_state:<run_id>：本轮事件与状态（含 ids）；查不到本轮时返回空事件与空状态。"""
        found = self._context.get(binding)
        if found is None:
            return {}, {}
        state = dict(found.get("state") or {})
        state.setdefault("ids", found.get("ids") or {})
        return dict(found.get("event") or {}), state

    def _register_energy_state(self, run_id: str) -> None:
        """能源状态钩子按 run_id 命名：同一 run_id 只登记一次，在执行该运行的专家前登记。"""
        if run_id in self._energy_hooks:
            return
        with self._context_lock:
            if run_id in self._energy_hooks:
                return
            self.registry.register_hook(f"{ENERGY_STATE_HOOK}{run_id}", self.energy_state)
            self._energy_hooks.add(run_id)

    # ---------------------------------------------------------------- 钩子入口
    def dispatch(self, ticket: Ticket, binding: RunBinding, experts: Sequence[str]) -> None:
        """B27 的 router.experts 钩子：并行发起本轮的专家运行，提案齐了就完成本轮。

        live：专家经 propose 工具落库提案即完成本轮（B27 的提案就绪钩子），回答随后只补诊断与依据；
        确定性来源的提案与结果同一次调用，全部返回后完成本轮。
        预演只参与对比：真实事件一律按现场条件算提案，算完写 preview_check_<轮次>。
        别的分支在同一注册表上也登记了专家处理器时让位（由它接管这一轮），避免同一位专家被跑两遍。
        """
        found = self._find_router(binding.run_id, binding.ticket_id)
        if found is None:
            return  # 不是本实例值守的程序（同进程另一份注册表）
        router, runner = found
        if len(router.registry.hooks_for(EXPERTS_HOOK)) > 1:
            return
        self._active[ticket.ticket_id] = (router, runner)
        self.last_lanes = ()
        self.last_preview = None
        started = time.monotonic()
        experts = tuple(dict.fromkeys(str(e) for e in experts))
        lanes: dict[str, Lane] = {}
        if self._source(router)[1] == "live":
            # live 提案经 propose 工具直接落库，不在可复现范围；不传固定顺序，免得后位专家持名额等前位、前位等名额
            self._live_round(router, runner, ticket, binding, experts, lanes, started)
        else:
            order = _RoundSaveOrder(experts)  # 本轮落库按固定专家顺序，保证提案编号可复现
            lanes = {lane.expert: lane for lane in self._parallel(router, runner, ticket, binding, experts, order)}
            self.last_elapsed_ms = int((time.monotonic() - started) * 1000)
            self._check_preview(router, ticket, binding)
            following = router.complete_round(ticket.ticket_id, binding.round)
            self._apply_held_status(router, ticket.ticket_id, following)
            self.last_lanes = tuple(lanes.values())
            self._record_conflicts(router, ticket, binding, self.last_lanes)
            if following is None and any(lane.status in FAILED for lane in self.last_lanes):
                router.store.set_ticket_status(ticket.ticket_id, "blocked")  # 专家运行失败：事件单受阻，其余提案照常
        self.last_lanes = tuple(lanes.values())

    def _live_round(self, router: Any, runner: Any, ticket: Ticket, binding: RunBinding, experts: Sequence[str],
                    lanes: dict[str, Lane], started: float) -> None:
        """live：后台跑各专家，提案落库就完成本轮；回答收集与挂接交给后台，dispatch 立刻返回。"""
        # 每位专家提案后要占着线程等本轮裁决（gate）：线程一人一个，真正的并发数由名额（slots）按上限控制
        cap = max(1, min(int(getattr(router.config, "max_experts", MAX_PARALLEL) or MAX_PARALLEL), MAX_PARALLEL))
        slots = threading.Semaphore(cap)
        gate = threading.Event()  # 本轮裁决完成前，回答只留在运行里，不挂提案
        phase = {expert: threading.Event() for expert in experts}  # 提案阶段结束（落库或确认没有）
        pool = ThreadPoolExecutor(max_workers=max(1, len(experts)), thread_name_prefix="consult")
        try:
            futures = {expert: pool.submit(self._one, router, runner, ticket, binding, expert, gate, phase[expert], slots)
                       for expert in experts}
            self._wait_proposals(router, binding, experts, futures, phase)
            self.last_elapsed_ms = int((time.monotonic() - started) * 1000)
            self._check_preview(router, ticket, binding)  # 对比要赶在本轮汇报排队之前，标题才带得上结论
            try:
                following = router.complete_round(ticket.ticket_id, binding.round)
                self._apply_held_status(router, ticket.ticket_id, following)
            finally:
                gate.set()
        except BaseException:
            pool.shutdown(wait=False)
            raise
        self._collect_replies(router, ticket, binding, futures, lanes, pool, following)

    def _apply_held_status(self, router: Any, ticket_id: str, following: Any) -> None:
        held = self.held_status.pop(ticket_id, None)
        if following is None and held and held != "resolved":
            router.store.set_ticket_status(ticket_id, held)

    def _collect_replies(self, router: Any, ticket: Ticket, binding: RunBinding, futures: Mapping[str, Any],
                         lanes: dict[str, Lane], pool: ThreadPoolExecutor, following: Any) -> None:
        """后台收回答：挂接已由各运行完成，这里汇总泳道、记分歧、按需标受阻。"""
        done = threading.Event()
        with self._reply_lock:
            self._reply_jobs = [job for job in self._reply_jobs if not job.is_set()] + [done]

        def collect() -> None:
            try:
                for expert, future in futures.items():
                    try:
                        lanes[expert] = future.result()
                    except Exception as exc:  # noqa: BLE001 — 单条收集异常不打断其他专家
                        lanes[expert] = Lane(expert=expert, started_at=binding.trigger_at, returned_at=self._wall(), elapsed_ms=0,
                                             status="failed", source="live", error=f"{type(exc).__name__}: {exc}")
                if self._current_reason(router, binding) is None:  # 本轮仍是当前轮才更新展示与受阻
                    self.last_lanes = tuple(lanes.values())
                    conflicts = self._record_conflicts(router, ticket, binding, self.last_lanes)
                    if conflicts:  # live 的回答在本轮裁决之后才挂上，B27 的完成钩子看不到分歧，由本卡补发
                        for hook in router.registry.hooks_for(REPLIES_READY_HOOK):
                            hook(ticket.ticket_id, binding.round)
                    if following is None and any(lane.status in FAILED for lane in self.last_lanes):
                        router.store.set_ticket_status(ticket.ticket_id, "blocked")  # 补报告失败也标受阻
            finally:
                pool.shutdown(wait=True)
                done.set()

        threading.Thread(target=collect, name=f"consult-replies-{binding.round}", daemon=True).start()

    def _wait_proposals(self, router: Any, binding: RunBinding, experts: Sequence[str],
                        futures: Mapping[str, Any], phase: Mapping[str, threading.Event]) -> None:
        """等各位专家的提案阶段结束；单个运行异常只结束该专家，其他专家继续等到一起再裁决。"""
        pending = set(experts)
        while pending:
            pending -= {p.expert for p in router.sink.list(binding.ticket_id, binding.round)}
            pending -= {expert for expert in tuple(pending)
                        if futures[expert].done() and not phase[expert].is_set()}  # 运行异常提前结束：只放掉这一位
            if not pending or all(phase[expert].is_set() for expert in pending):
                return
            time.sleep(POLL_S)

    def step(self, run_id: str, now: datetime) -> None:
        """B27 的 router.step 钩子：每个推进步做一次预演检查。"""
        router = next((r for r, _ in self._bindings if r.run_id == run_id), None)
        preview = self._previews.get(id(router)) if router is not None else None
        if preview is None:
            return
        try:
            preview.observe(now)
        except Exception:  # noqa: BLE001 — 预演失败不影响值守循环
            return

    def find(self, ticket_id: str, round_: int | None = None) -> tuple[Any, Any] | None:
        """按事件单找值守程序：优先本轮刚派发的那一个，其次按（运行, 轮次）在已绑定的路由里找。"""
        found = self._active.get(ticket_id)
        if found is not None and (round_ is None or (found[0].store.round_trigger(ticket_id, round_) is not None)):
            return found
        for router, runner in reversed(self._bindings):
            if router.store.ticket(ticket_id) is None:
                continue
            if round_ is not None and router.store.round_trigger(ticket_id, round_) is None:
                continue
            return router, runner
        return None

    def wait_preview(self, timeout: float = 10.0) -> bool:
        return all(preview.wait(timeout) for preview in self._previews.values())

    def wait_replies(self, timeout: float = 10.0) -> bool:
        """等 live 专家的回答挂接与分歧记录完成；演示与测试用，值守主流程不等。"""
        with self._reply_lock:
            jobs = list(self._reply_jobs)
        return all(job.wait(timeout) for job in jobs)

    # ---------------------------------------------------------------- 专家并行
    def _parallel(self, router: Any, runner: Any, ticket: Ticket, binding: RunBinding,
                  experts: Sequence[str], order: _RoundSaveOrder) -> list[Lane]:
        def one(expert: str) -> Lane:
            return self._one(router, runner, ticket, binding, expert, order=order)

        workers = max(1, min(len(experts), int(getattr(router.config, "max_experts", MAX_PARALLEL) or MAX_PARALLEL), MAX_PARALLEL))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="consult") as pool:
            return list(pool.map(one, experts))

    def _one(self, router: Any, runner: Any, ticket: Ticket, binding: RunBinding, expert: str,
             gate: threading.Event | None = None, proposed: threading.Event | None = None,
             slots: threading.Semaphore | None = None, order: _RoundSaveOrder | None = None) -> Lane:
        """一位专家的一次运行。

        运行与结果处理（解析、落库）在同一个隔离里：异常按该专家失败处理、走确定性兜底；
        回答挂接失败也只把该专家记为失败，其余专家与整轮不受影响。
        """
        requested_at = self._wall()
        returned_at = requested_at
        proposal_ids: list[str] = []
        reply: ExpertReply | None = None
        diagnosis: str | None = None
        result: Mapping[str, Any] | None = None
        error: str | None = None
        status = "failed"
        coverage_gap = True
        source_name = "deterministic" if router.mode == "deterministic" else ("cache" if router.mode in ("trial", "evaluation") else "nexent")
        acquired = False
        try:
            if slots is not None:  # live 并发名额只管运行与落库，等裁决时已归还，排队的专家才起得来
                slots.acquire()
                acquired = True
            source, mode = self._source(router)
            if mode == "deterministic" and router.mode in ("trial", "evaluation"):
                source_name = "deterministic"  # 缓存/评测来源没登记，实际取的是确定性实现
            payload = self._payload(router, runner, self._event(router, ticket), binding) | {"expert": expert}
            if mode == "live":  # live 专家经 propose 工具取现场：先把本轮事件与状态寄存下来
                self._context.put(binding, payload)
                self._register_energy_state(binding.run_id)
            status = "finished"
            try:
                result = source.get(AgentResultKind.expert, binding, payload)
            except Exception as exc:  # noqa: BLE001 — 单个专家失败不中断本轮
                error = f"{type(exc).__name__}: {exc}"
            returned_at = self._wall()
            if error is not None:
                status = "failed"
            if result is not None:
                status = str(result.get("status") or "finished")
                source_name = str(result.get("source") or source_name)
                if result.get("proposal") is not None:
                    draft = ExpertProposal.model_validate(result["proposal"])
                    diagnosis = draft.diagnosis
                    proposal_ids.append(self._save(router, expert, draft, binding, self._event(router, ticket),
                                                  requested_at, returned_at, order))
                proposal_ids += [str(pid) for pid in result.get("proposal_ids") or () if str(pid) not in proposal_ids]
            failed = error is not None or status in FAILED
            missing = not proposal_ids and not failed  # 运行结束但没交提案
            coverage_gap = (mode != router.mode or failed or missing or bool(result and result.get("coverage_gap"))
                            or (mode == "live" and not payload.get("state")))
            if (failed or missing) and mode != "deterministic":
                covered = self._fallback(router, runner, ticket, binding, expert, proposal_ids, requested_at, returned_at, order)
                if covered and router.mode in ("trial", "evaluation"):
                    source_name = "deterministic"  # 落的是确定性兜底提案
                if missing and not covered:
                    status = "blocked"  # 没交提案也没有确定性兜底：按受阻处理
            elif missing:
                status = "blocked"
            if missing:
                router.store.set_ticket_state(binding.ticket_id, f"proposal_missing_{binding.round}_{expert}", "1")
        except Exception as exc:  # noqa: BLE001 — 提案校验/落库异常只结束这一位，其他专家照常
            error = error or f"{type(exc).__name__}: {exc}"
            status, diagnosis, coverage_gap = "failed", None, True
            proposal_ids = []
            try:
                if self._fallback(router, runner, ticket, binding, expert, proposal_ids, requested_at, self._wall(), order)                         and router.mode in ("trial", "evaluation"):
                    source_name = "deterministic"
            except Exception:  # noqa: BLE001 — 兜底也失败就只剩受阻
                pass
        finally:
            if acquired:
                slots.release()
            if order is not None:  # 提案阶段结束：固定顺序里后面的专家可以落库了
                order.finished(expert)
        if proposed is not None:  # 提案阶段结束：落库了或确认不会有，裁决不必再等
            proposed.set()
        try:
            if gate is not None:  # 先动作、后补依据：本轮裁决完成后才把回答挂到提案上
                gate.wait()
            if result is not None and "final_answer" in result:
                reply = parse_reply(result)
                self._attach_reply(router, binding, reply, proposal_ids, expert)
                if reply.diagnosis:
                    diagnosis = reply.diagnosis
        except Exception as exc:  # noqa: BLE001 — 回答挂接失败只把该专家记为失败，不中断整轮
            error = f"{type(exc).__name__}: {exc}"
            status, coverage_gap = "failed", True
        timing = self._timing(router, binding, self._event(router, ticket), requested_at, returned_at, result)
        # trial/evaluation 下实际执行的是确定性实现：与主 Agent、live 回退同一口径记 deterministic 加覆盖不足（#166、#243）
        offline_fallback = router.mode in ("trial", "evaluation") and source_name == "deterministic"
        record = AgentRunRecord(
            agent_run_id=self._run_id(result, expert, binding),
            kind=AgentResultKind.expert, agent_name=expert, run_mode="deterministic" if offline_fallback else router.mode,
            source=source_name, binding=binding, timing=timing, status=status,
            coverage_gap=coverage_gap or offline_fallback, proposal_ids=tuple(proposal_ids),
            final_answer=str(result.get("final_answer") or "")[:2000] or None if result else None,
        )
        router.store.save_agent_run(record)
        router.runs.append(record)
        return Lane(expert=expert, started_at=requested_at, returned_at=returned_at,
                    elapsed_ms=int((returned_at - requested_at).total_seconds() * 1000), status=record.status,
                    source=source_name, proposal_ids=tuple(proposal_ids), diagnosis=diagnosis,
                    reply_status=reply.status if reply else None, error=error)

    @staticmethod
    def _proposal_of(router: Any, binding: RunBinding, expert: str) -> str | None:
        """本事件单本轮该专家已有的提案编号（propose 工具或兜底落库的）。"""
        for proposal in router.sink.list(binding.ticket_id):
            if proposal.proposal_id and proposal.expert == expert and proposal.binding is not None \
                    and proposal.binding.round == binding.round:
                return proposal.proposal_id
        return None

    def _fallback(self, router: Any, runner: Any, ticket: Ticket, binding: RunBinding, expert: str,
                  proposal_ids: list[str], requested_at: datetime, returned_at: datetime,
                  order: _RoundSaveOrder | None = None) -> bool:
        """运行失败或没交提案时的确定性兜底：本轮已有提案就复用，否则补一份。返回本轮该专家是否已有提案。"""
        existing = self._proposal_of(router, binding, expert)
        if existing is not None:
            if existing not in proposal_ids:
                proposal_ids.append(existing)
            return True
        fallback = self.registry.mode_ports.get((AgentResultSource, "deterministic", EXPERT_KEY))
        if fallback is None:
            return False
        try:
            payload = self._payload(router, runner, self._event(router, ticket), binding) | {"expert": expert}
            result = fallback.get(AgentResultKind.expert, binding, payload)
            if result.get("proposal") is not None:
                draft = ExpertProposal.model_validate(result["proposal"])
                proposal_ids.append(self._save(router, expert, draft, binding, self._event(router, ticket),
                                               requested_at, returned_at, order))
                return True
        except Exception:  # noqa: BLE001 — 没有兜底就只剩受阻，不伪造提案
            return False
        return False

    # ---------------------------------------------------------------- 预演对比
    def _check_preview(self, router: Any, ticket: Ticket, binding: RunBinding) -> None:
        """现场提案与匹配到的预演对比：写 preview_check_<轮次>，并把结论带进报告的一句话。"""
        preview = self._previews.get(id(router))
        event = self._event(router, ticket)
        if preview is None or event is None:
            return
        match = preview.match(event)
        if match is None:
            check = PreviewCheck(matched=False)
        elif match.note:
            check = PreviewCheck(matched=True, diff=(match.note,), preview_ticket=match.ticket_id, signal=match.signal)
        else:
            same, diff = compare_actions(match.drafts, router.sink.list(ticket.ticket_id, binding.round))
            check = PreviewCheck(matched=True, same_actions=same, diff=tuple(diff), preview_ticket=match.ticket_id,
                                 signal=match.signal)
        self.last_preview = check
        router.store.set_ticket_state(ticket.ticket_id, f"{PREVIEW_CHECK_KEY}_{binding.round}",
                                      json.dumps({"matched": check.matched, "same_actions": check.same_actions,
                                                  "diff": list(check.diff), "preview_ticket": check.preview_ticket},
                                                 ensure_ascii=False))
        if check.matched:  # B27 的合批汇报按事件单摘要合成主 Agent 一句话
            self._annotate_summary(router, ticket.ticket_id,
                                   "（与预演一致）" if check.same_actions else f"（预演偏差：{'；'.join(check.diff)}）")

    @staticmethod
    def _annotate_summary(router: Any, ticket_id: str, note: str) -> None:
        """把预演对比结论附到事件单摘要；摘要已带过就不重复。"""
        ticket = router.store.ticket(ticket_id)
        if ticket is None or note in ticket.summary:
            return
        router.store.save_ticket(ticket.model_copy(update={"summary": ticket.summary + note}), run_id=router.run_id)

    # ---------------------------------------------------------------- 落库与回答
    def _save(self, router: Any, expert: str, draft: ExpertProposal, binding: RunBinding, event: DayEvent | None,
              requested_at: datetime, returned_at: datetime, order: _RoundSaveOrder | None = None) -> str:
        timing = self._timing(router, binding, event, requested_at, returned_at, None)
        proposal = draft.model_copy(update={"proposal_id": None, "ticket_id": binding.ticket_id, "binding": None, "timing": timing})
        if order is not None:  # 按固定专家顺序落库，提案编号在同一剧本里可复现
            order.wait_turn(expert)
        return router.sink.save(ExpertProposal.model_validate(proposal.model_dump()), binding)

    def _attach_reply(self, router: Any, binding: RunBinding, reply: ExpertReply, proposal_ids: list[str],
                      expert: str) -> None:
        """按 proposal_id 挂诊断与依据；解析失败、不属于该专家本轮运行、过期或已换轮次的回答只记录不挂接。"""
        if reply.status == "parse_failed":
            router.store.set_ticket_state(binding.ticket_id, f"reply_parse_failed_{binding.round}", "1")
            return
        proposal_id = reply.proposal_id or (proposal_ids[0] if len(proposal_ids) == 1 else None)
        if proposal_id is None:
            router.store.set_ticket_state(binding.ticket_id, f"reply_without_proposal_{binding.round}", "1")
            return
        owned = any(p.proposal_id == proposal_id and p.expert == expert
                    for p in router.sink.list(binding.ticket_id, binding.round))
        if not owned:  # 模型填了别的专家或别的运行的提案：不挂接，别改别人的诊断
            router.store.set_ticket_state(binding.ticket_id, f"reply_foreign_proposal_{binding.round}_{expert}",
                                          f"提案 {proposal_id} 不属于本轮 {expert} 的运行")
            return
        try:
            stored = router.sink.get(proposal_id)
        except PlantkgError:
            router.store.set_ticket_state(binding.ticket_id, f"late_reply_{binding.round}", f"提案 {proposal_id} 不存在")
            return
        reason = self._current_reason(router, binding) or stale_reason(stored, binding)
        if reason is not None:
            router.store.set_ticket_state(binding.ticket_id, f"late_reply_{binding.round}", reason)
            return
        router.store.set_ticket_state(binding.ticket_id, f"reply_status_{binding.round}_{proposal_id}", reply.status)
        router.store.set_ticket_state(binding.ticket_id, f"reply_diagnosis_{binding.round}_{proposal_id}", reply.diagnosis or "")
        if reply.question:
            router.store.set_ticket_state(binding.ticket_id, f"reply_question_{binding.round}", reply.question)
        router.sink.attach_reply(proposal_id, reply)
        if proposal_id not in proposal_ids:
            proposal_ids.append(proposal_id)

    @staticmethod
    def _current_reason(router: Any, binding: RunBinding) -> str | None:
        """回答挂接前核对事件单当前轮次与当前运行、知识版本；不一致返回原因（旧轮回答只入历史）。"""
        ticket = router.store.ticket(binding.ticket_id)
        if ticket is None:
            return "事件单不存在"
        if ticket.status == "closed":
            return "事件单已关闭"
        if ticket.round != binding.round:
            return f"事件单已到第 {ticket.round} 轮，本轮是第 {binding.round} 轮"
        if router.run_id != binding.run_id:
            return f"提案来自运行 {binding.run_id}，当前运行是 {router.run_id}"
        if router.knowledge_version != binding.knowledge_version:
            return f"提案的知识版本 v{binding.knowledge_version}，当前是 v{router.knowledge_version}"
        return None

    # ---------------------------------------------------------------- 分歧与记录
    def _record_conflicts(self, router: Any, ticket: Ticket, binding: RunBinding, lanes: Sequence[Lane]) -> bool:
        """把已挂到提案上的不同诊断记进事件单；返回是否记了分歧。"""
        judged = [lane for lane in lanes if lane.reply_status not in (None, "parse_failed") and lane.diagnosis
                  and any(router.sink.replied(pid) for pid in lane.proposal_ids)]
        if len({lane.diagnosis for lane in judged}) <= 1:
            return False
        conflicts = [{"expert": lane.expert, "proposal_ids": list(lane.proposal_ids), "diagnosis": lane.diagnosis}
                     for lane in judged]
        router.store.set_ticket_state(ticket.ticket_id, f"conflicts_round_{binding.round}",
                                      json.dumps(conflicts, ensure_ascii=False))
        sink = self.registry.ports.get(EvidenceSink)
        if sink is None:
            return True
        try:
            sink.append(EvidenceEvent(event_id=f"consult-conflict-{ticket.ticket_id.rsplit('/', 1)[-1]}-{binding.round}",
                                      seq=0, occurred_at=binding.trigger_at, run_id=binding.run_id,
                                      trace_id=f"consult-{ticket.ticket_id.rsplit('/', 1)[-1]}",
                                      event_type="conflict_detected", result_status="conflict",
                                      summary=json.dumps(conflicts, ensure_ascii=False)[:2000]))
        except Exception:  # noqa: BLE001 — 证据写入失败不改变会诊结论
            pass
        return True

    def _timing(self, router: Any, binding: RunBinding, event: DayEvent | None, requested_at: datetime,
                returned_at: datetime, result: Mapping[str, Any] | None) -> AgentTiming:
        budget = getattr(getattr(getattr(router, "policy", None), "response_budget", None), "layer12_min", 0) or 0
        origin = event.at if event is not None else binding.trigger_at
        if origin > binding.trigger_at:  # 合并派发时取最早的事件时刻（响应指标用）
            origin = binding.trigger_at
        returned_at = max(returned_at, requested_at)
        measured = int((returned_at - requested_at).total_seconds() * 1000)
        latency = result.get("latency_ms") if result is not None else None
        return AgentTiming(origin_event_at=origin, trigger_at=binding.trigger_at,
                           effective_at=binding.trigger_at + timedelta(minutes=budget), requested_at=requested_at,
                           returned_at=returned_at, latency_ms=int(latency) if isinstance(latency, int) else measured,
                           retries=int(result.get("retries") or 0) if result is not None else 0)

    @staticmethod
    def _run_id(result: Mapping[str, Any] | None, expert: str, binding: RunBinding) -> str:
        found = result.get("agent_run_id") if result is not None else None
        if found:
            return str(found)
        return f"consult-{binding.ticket_id.rsplit('/', 1)[-1]}-{binding.round}-{expert}"

    # ---------------------------------------------------------------- 小工具
    def _source(self, router: Any) -> tuple[Any, str]:
        for mode in (router.mode, "deterministic"):
            impl = router.registry.mode_ports.get((AgentResultSource, mode, EXPERT_KEY))
            if impl is not None:
                return impl, mode
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"会诊的专家结果来源未登记（{router.mode} 与 deterministic 都没有）")

    def _find_router(self, run_id: str, ticket_id: str) -> tuple[Any, Any] | None:
        for router, runner in reversed(self._bindings):
            if router.run_id == run_id and router.store.ticket(ticket_id) is not None:
                return router, runner
        return None

    @staticmethod
    def _event(router: Any, ticket: Ticket) -> DayEvent | None:
        if not ticket.event_ids:
            return None
        found = router.store.event(ticket.event_ids[-1])
        return found["event"] if found else None

    @staticmethod
    def _payload(router: Any, runner: Any, event: DayEvent | None, binding: RunBinding) -> dict[str, Any]:
        """专家运行的注入内容：事件单本轮的事件；场景运行再带运行简报与对象表（与 B17 直派器同口径）。

        effective_at 按 policy 的响应预算给出：B23 缺省按 trigger_at 计时，提案会被直用兜底判过期。
        """
        budget = getattr(getattr(getattr(router, "policy", None), "response_budget", None), "layer12_min", 0) or 0
        payload: dict[str, Any] = {"ticket_id": binding.ticket_id, "round": binding.round,
                                   "as_of": binding.trigger_at.isoformat(), "plant_id": router.plant_id,
                                   "reason": "consultation",
                                   "effective_at": (binding.trigger_at + timedelta(minutes=budget)).isoformat()}
        if event is not None:
            payload["event"] = event.model_dump(mode="json")
        if runner is not None:
            try:
                payload["state"] = runner.brief()
            except Exception:  # noqa: BLE001 — 简报不可用时不伪造
                pass
            try:
                payload["ids"] = runner.ids
            except Exception:  # noqa: BLE001
                pass
            try:
                payload["knowledge"] = sorted(runner.knowledge)
            except Exception:  # noqa: BLE001
                pass
        else:
            state: dict[str, Any] = {}
            for provider in router.registry.hooks_for(PREVIEW_STATE_HOOK):
                try:
                    found = provider(router, binding.trigger_at)
                except Exception:  # noqa: BLE001 — 主路由状态源不可用时保留事件上下文
                    continue
                if isinstance(found, Mapping):
                    state.update({key: value for key, value in found.items() if isinstance(value, (int, float))})
            if state:
                payload["state"] = state
        return payload


def install(registry: Any, *, router: Any = None, settings: Any = None, wall: Any = utcnow) -> Consultation:
    """登记确定性结果来源、会诊钩子与提案就绪占位；绑定主路由（给了就绑，否则取已登记的值守程序）。"""
    install_sources(registry)
    consultation = Consultation(registry, settings=settings, wall=wall)
    if router is not None:
        consultation.bind(router)
    else:
        consultation.attach()
    registry.register_hook(EXPERTS_HOOK, consultation.dispatch)
    registry.register_hook(STEP_HOOK, consultation.step)
    registry.register_hook(SCENARIO_BIND_HOOK, consultation.bind)
    registry.register_hook(PROPOSALS_READY_HOOK, DirectApplyFallback(consultation))
    # 专家 propose 工具按本轮绑定取现场：只有 live 会诊前寄存过 payload 时才有内容
    registry.register_hook(ELEVATOR_CONTEXT_HOOK, consultation.elevator_context)
    registry.register_hook(HVAC_SITUATION_HOOK, consultation.hvac_situation)
    registry.register_hook(EQUIPMENT_READINGS_HOOK, consultation.equipment_readings)
    registry.register_hook(SERVICE_HOOK, consultation)
    return consultation


def register(registry: Any, settings: Any) -> None:
    """领域包入口（packs/consultation/pack.yaml）：场景注册表也登记同一套确定性来源，并承接场景结束解绑。"""
    from plantkg.agent.consultation.preview import PREVIEW_SIGNALS_HOOK, PREVIEW_STATE_HOOK, PreviewSignal
    from plantkg.core.contracts import Context
    from plantkg.core.ports import Gate, RunStore, StateProvider, TimeSeriesQuery
    from plantkg.world.scenario.runner import SCENARIO_END_HOOK, SCENARIO_HOOK

    consultation = install(registry, settings=settings)

    def replay_preview_signals(router: Any, at: datetime) -> list[PreviewSignal]:
        run = registry.resolve(RunStore).get_run(router.run_id)
        context = Context(plant_id=router.plant_id, industry_pack=settings.industry_pack, actor=None, channel="none",
                          run_id=run.run_id, trace_id=f"preview-{run.run_id}", as_of=at,
                          version_manifest_id=run.version_manifest_id)
        return _replay_plan_signals(registry.resolve(Gate).preview_inputs(context), at,
                                    router.policy.preview_horizon_min)

    registry.register_hook(PREVIEW_SIGNALS_HOOK, replay_preview_signals)

    def replay_preview_state(router: Any, at: datetime) -> dict[str, Any]:
        state: dict[str, Any] = {}
        try:
            run = registry.resolve(RunStore).get_run(router.run_id)
            context = Context(plant_id=router.plant_id, industry_pack=settings.industry_pack, actor=None, channel="none",
                              run_id=run.run_id, trace_id=f"preview-{run.run_id}", as_of=at,
                              version_manifest_id=run.version_manifest_id)
            snapshot = registry.resolve(StateProvider).at(context, at)
            packs = snapshot.pack_states
            storage = packs.get("storage", {})
            soc = storage.get("soc", storage.get("soc_end"))
            if soc is None and storage.get("capacity_kwh"):
                soc = storage.get("energy_kwh", 0) / storage["capacity_kwh"]
            if isinstance(soc, (int, float)) and not isinstance(soc, bool):
                state["soc"] = float(soc)
            if at > run.clock.start:
                from plantkg.core.contracts import Interval
                from plantkg.world.replay.clock import STEP

                window = Interval(start=max(run.clock.start, at - STEP), end=at)
                bundle = registry.resolve(TimeSeriesQuery).query(context, "active_power", window)
                records = [record for record in bundle.records
                           if record.value is not None and record.available_at <= at
                           and getattr(record.locator, "meter", None) == "transformer_load:TX1"]
                if records:
                    record = max(records, key=lambda item: item.event_time)
                    limit = next((step.params.get("limit_kw") for step in record.transform_steps
                                  if isinstance(step.params.get("limit_kw"), (int, float))), None)
                    if limit:
                        state["transformer_ratio"] = float(record.value) / float(limit)
        except Exception:  # noqa: BLE001 — 回放事实暂不可见时不触发预演
            return state
        if state:
            state["nature"] = "derived"
        return state

    registry.register_hook(PREVIEW_STATE_HOOK, replay_preview_state)

    def scenario_plugin(scenario_registry: Any) -> None:
        install_sources(scenario_registry)
        scenario_registry.register_hook(SCENARIO_END_HOOK, consultation.on_scenario_end)

    registry.register_hook(SCENARIO_HOOK, scenario_plugin)

