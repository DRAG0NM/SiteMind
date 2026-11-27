"""B19 能源专家的一天：预测 → 共享规划（B09）→ 门控（B15）→ 场景执行（B17）→ 账本（B08）。

`run_day(site, date)`：
1. 由 B18 装配与场景剧本构造预测（B14 契约，derived）；
2. B09 共享规划求解，B11 显式期末目标 `RunInfo.energy_end_target_kwh / 容量`，B08 复算计划账本；
3. 计划登记、门控评估；需要人工确认时由演示复核员确认（门控决定、批准与执行依据都落库）；
4. 交 B17 场景世界执行：分钟层 `RealtimeController` 每分钟把购电压在 15 分钟层目标线以下；
   每个整格边界用分钟账本的实际 SOC 滚动重解（事件后从下一个整格起，单次 5 秒内）；
5. 返回计划、门控决定、逐分钟/逐槽账本与重解记录。

储能由分钟层独占；事件级放电提案（`EnergyExpert`）在独立场景运行与 MCP 工具里保留，避免两条路径
对同一分钟给出互相覆盖的充放指令。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from plantkg.core.contracts import (
    Actor,
    Context,
    DataSourceRef,
    ErrorCode,
    ForecastBundle,
    Interval,
    Plan,
    PlantkgError,
    Role,
    RunInfo,
    ScenarioSlotSummary,
    StateSnapshot,
    StorageParams,
)
from plantkg.experts.energy.assembly import DEFAULT_DB, DemoAssembly, build_demo_assembly
from plantkg.experts.energy.forecast import build_forecasts
from plantkg.experts.energy.materials import build_metadata, scenario_calibrations
from plantkg.experts.energy.minute import ENERGY_EVENTS, MinuteLayerSource
from plantkg.experts.energy.planner import (
    OPERATION_START,
    DayPlan,
    ScenarioPacks,
    baseline_cost,
    load_scenario_packs,
    plan_day,
    reserve_param_ref,
    slot_powers,
    slot_series,
    with_published_tariff,
)
from plantkg.experts.energy.proposal import energy_state_hook
from plantkg.experts.energy.realtime import RealtimeController, SlotTarget
from plantkg.core.ports import AgentResultSource, VersionStore
from plantkg.core.registry import Registry
from plantkg.decision.usecases.demo_inputs import PROFILES
from plantkg.world.scenario.inputs import SiteInputProvider
from plantkg.world.scenario.runner import RunTrace, ScenarioRunner, ScenarioWorld, default_registry
from plantkg.world.scenario.script import Scenario, load_scenario
from plantkg.world.sim.base import MINUTE, SLOT

ACTOR = Actor(actor_id="b19-energy-day", roles=(Role.operator,))
JOB_EFFECT = "b19-job"
REPLAN_MIN_REMAINING = 2 * SLOT
DEFAULT_SCENARIO = Path(__file__).resolve().parents[3] / "configs" / "scenarios" / "factory_demo_day.yaml"


@dataclass(frozen=True)
class Replan:
    at: datetime
    energy_kwh: float  # 分钟账本在整格边界上的实际 SOC 对应电量
    plan_id: str
    solver_status: str
    elapsed_s: float
    end_target_kwh: float
    end_floor_kwh: float
    initial_soc: float


@dataclass
class DayRun:
    scenario: Scenario
    world: ScenarioWorld
    db: object
    inputs: object
    packs: ScenarioPacks
    assembly: DemoAssembly
    plan: DayPlan
    forecasts: ForecastBundle
    decision: object
    executable: Plan
    plan_run: RunInfo
    ctx: Context
    day_start: datetime
    day_end: datetime
    end_target_kwh: float
    controller: RealtimeController
    replans: list[Replan] = field(default_factory=list)
    registry: Registry | None = None  # 装配方（bootstrap）的注册表：状态钩子也登记到它上面，MCP 提案工具才读得到运行状态
    replan_failures: list[dict] = field(default_factory=list)  # 重解不可行：时刻、原因、当时储能离期末目标的缺口
    notes: list[str] = field(default_factory=list)
    trace: RunTrace | None = None
    runner: ScenarioRunner | None = None
    current_plan: DayPlan | None = None
    _slot_data: dict[datetime, tuple[float, float]] = field(default_factory=dict)
    _slot_power: dict[datetime, dict[str, float]] = field(default_factory=dict)
    _fork_at: datetime | None = None
    _forked: bool = False
    _hooked: set[str] = field(default_factory=set)
    _seen_events: int = 0
    _source_written: set[str] = field(default_factory=set)
    _job_start: dict[str, datetime] = field(default_factory=dict)  # 已开始工序的实际开始时刻

    # ---- 计划与门控（run_day 已完成，这里只读）
    @property
    def plan_ledger(self):
        return self.plan.ledger

    @property
    def storage(self) -> StorageParams:
        return self.packs.storage.ledger_params()

    # ---- 执行
    def execute(self, until: datetime | None = None, *, start_at: datetime | None = None,
                fork_at: datetime | None = None) -> RunTrace:
        if self.runner is None:
            self._start()
        if start_at is not None:
            self.fast_forward(start_at)
        until = min(until or self.day_end, self.scenario.end)
        if until <= self.runner.now:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "执行截止必须晚于起点")
        if fork_at is not None and fork_at != _floor_minute(fork_at):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"分叉时刻 {fork_at.isoformat()} 必须对齐到整分钟")
        if fork_at is not None and not self.runner.now < fork_at < until:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"分叉时刻 {fork_at.isoformat()} 须在本次执行区间内（晚于起点、早于截止）")
        self._fork_at = fork_at
        while self.runner.now < until:
            self._minute()
        self.trace = self.runner.trace
        self._write_sources()
        return self.trace

    def fast_forward(self, at: datetime) -> None:
        """把仿真时钟跳到 `at`（通常是看效果的窗口起点），储能电量取日前计划在该槽的插值。

        跳过的分钟不落账本、不注入事件（剧本事件按 >= at 保留），所以账本从窗口起点开始；设备状态取装配
        初值。用于“只看 07:30～08:30”的演示，全天赋能仍从 00:00 跑。
        """
        runner = self.runner
        if at <= runner.now:
            return
        slot = at - timedelta(minutes=at.minute % 15, seconds=at.second, microseconds=at.microsecond)
        entry = next((e for e in self.plan.ledger.entries if e.interval.start == slot), None)
        if entry is None:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"起点 {at.isoformat()} 不在日前计划区间内")
        frac = (at - slot) / SLOT
        energy = entry.energy_start_kwh + (entry.energy_end_kwh - entry.energy_start_kwh) * frac
        runner.state.at = at
        runner.energy = energy
        runner.state.devices[runner.world.index.battery_id] = {
            "soc": round(energy / self.packs.storage.capacity_kwh, 6), "mode": "idle", "power_kw": 0.0}
        runner.queue = [item for item in runner.queue if item[1] >= at]
        runner.slot_minutes, runner.slot_states = [], []
        self.notes.append(f"从 {at.astimezone(self.scenario.tz).strftime('%H:%M')} 起跑：储能 {energy:.1f} kWh 取日前计划插值")

    def _start(self) -> None:
        registry = default_registry(self.registry)
        # 分钟层独占储能：能源专家仍接事件、开事件单，但它提案里的电池充放动作被过滤，照明削峰等照常执行
        slot = (AgentResultSource, "deterministic", "energy")
        registry.mode_ports[slot] = MinuteLayerSource(registry.mode_ports[slot], self.world.index.battery_id)
        self.runner = ScenarioRunner(self.world, self.db, registry=registry, mode="evaluation")
        self.runner.start()
        self._register_state_hook()
        self._write_sources()
        self.current_plan = self.plan
        self._slot_data.update(slot_series(self.forecasts, self.plan.plan.valid_interval))
        self._slot_power.update(slot_powers(self.plan.plan))

    def _minute(self) -> None:
        runner = self.runner
        t = runner.now
        self._register_state_hook()  # 剧本里的知识发布也会分叉出子运行，事件单绑的是当前运行
        if self._fork_at is not None and not self._forked and t == self._fork_at:
            runner.publish(t, adds=["energy_realtime_sop"], summary="能源实时操作要点发布（知识版本分叉）")
            self._forked = True
            self._register_state_hook()
            self._write_sources()
        if t.minute % 15 == 0 and t > self.day_start and t < self.day_end:
            self._replan(t)
        if t.minute % 15 == 0:
            self._run_jobs(t)
        target = self._target_for(t)
        minute_state = self._minute_state(t)
        action = self.controller.step(minute_state, target)
        if action.params.get("power_kw", 0.0) > 0.05:
            for hook in runner.registry.hooks_for("scenario.energy_action_basis"):
                hook(runner, action, self.current_plan.plan, minute_state, target, self.controller.history[-1])
            runner.apply(action, source={"expert": "energy", "layer": "minute",
                                         "plan_id": self.current_plan.plan.plan_id,
                                         "plan_start": self.current_plan.plan.valid_interval.start.isoformat()})
            runner.note("energy_minute", t, {"command": action.command, "power_kw": action.params["power_kw"],
                                             "line_kw": round(target.import_line_kw, 3)})
        runner.step()
        self._after_step()

    def _after_step(self) -> None:
        runner = self.runner
        events = runner.trace.events
        budget = runner.policy.response_budget.layer12_min
        for event in events[self._seen_events:]:
            if event.severity > 0 and event.event_type in ENERGY_EVENTS:
                self.controller.respond_from = _ceil_minute(event.at + timedelta(minutes=budget))
                runner.note("energy_response", event.at, {"event_id": event.event_id, "event_type": event.event_type,
                                                          "effective_at": self.controller.respond_from.isoformat()})
        self._seen_events = len(events)

    def _register_state_hook(self) -> None:
        if self.runner is not None and self.runner.run_info.run_id not in self._hooked:
            self._hooked.add(self.runner.run_info.run_id)
            hook = energy_state_hook(self.runner)
            for registry in (self.runner.registry, self.registry):
                if registry is not None:
                    registry.register_hook(f"energy_state:{self.runner.run_info.run_id}", hook)

    # ---- 工序（B13）：开始时刻取当前计划，开始后每个整格往世界里注入一格负荷
    def _planned_start(self, operation_id: str) -> datetime | None:
        return next((a.interval.start for a in self.current_plan.plan.actions
                     if a.quantity == OPERATION_START and a.value > 0.5 and a.object.endswith("/" + operation_id)), None)

    def _run_jobs(self, t: datetime) -> None:
        runner = self.runner
        for op in self.packs.operations:
            start = self._job_start.get(op.operation_id) or self._planned_start(op.operation_id)
            if start is None or start > t:
                continue
            self._job_start[op.operation_id] = start
            k = int((t - start) / SLOT)
            if 0 <= k < op.slots:  # 工序负荷在厂区基础负荷之外追加（synthetic），由承接设备的 extra_load 体现
                runner.state.effects.append({"kind": "extra_load", "entity_id": runner.world.index.eid(self.packs.equipment_code),
                                             "kw": op.profile_kw[k], "since": t.isoformat(), "until": (t + SLOT).isoformat(),
                                             "source": JOB_EFFECT})
                runner.note("energy_job", t, {"operation_id": op.operation_id, "slot": k, "kw": round(op.profile_kw[k], 3),
                                              "started_at": start.isoformat()})

    def _extra_load_kw(self, at: datetime) -> float:
        """当前仍生效的事件附加负荷（突增等）在 `at` 所在槽的功率；到期的不算，工序自己注入的不算（计划里单独排）。"""
        total = 0.0
        for e in self.runner.state.effects:
            if e["kind"] == "extra_load" and e.get("source") != JOB_EFFECT and (
                    e.get("until") is None or datetime.fromisoformat(e["until"]) > at):
                total += e["kw"]
        return total

    # ---- 滚动重解
    def _replan(self, t: datetime) -> None:
        # 滚动重解的计划有意不走 B15 门控与 PlanLedgerStore：分钟层实时跟踪不能等人工确认，日前计划才是被门控批准的那版。
        runner = self.runner
        horizon = Interval(start=t, end=self.day_end)
        if horizon.end - horizon.start < REPLAN_MIN_REMAINING:
            return
        ctx = self._context(t)
        snapshot = StateSnapshot(run_id=runner.run_info.run_id, snapshot_id=f"{runner.run_info.run_id}@{t.isoformat()}",
                                 event_seq=int((t - self.scenario.start) / MINUTE), as_of=t,
                                 version_manifest_id=runner.run_info.version_manifest_id,
                                 pack_states={"storage": {"energy_kwh": runner.energy}})
        forecasts = build_forecasts(self.inputs, horizon, ctx, extra_load_kw=self._extra_load_kw)
        operations, fixed = self._remaining_operations(t)
        try:
            packs = self.packs
            if self.registry and VersionStore in self.registry.ports:
                versions = self.registry.resolve(VersionStore)
                hooks = self.registry.hooks_for("knowledge.b30.rules")
                if hooks and versions.writer is self.db.writer:
                    packs = with_published_tariff(packs, hooks[-1](ctx.version_manifest_id, t, t), horizon)
            plan = plan_day(packs, forecasts, snapshot, ctx, horizon, energy_start_kwh=runner.energy,
                            end_target_kwh=self.end_target_kwh, operations=operations, fixed=fixed)
        except PlantkgError as exc:  # 重解失败保留上一版目标线，分钟层继续跟踪并如实记录
            gap = max(0.0, self.end_target_kwh - runner.energy)
            self.replan_failures.append({"at": t.isoformat(), "error": str(exc), "energy_kwh": round(runner.energy, 3),
                                         "end_gap_kwh": round(gap, 3)})
            self.notes.append(f"{t.astimezone(self.scenario.tz).strftime('%H:%M')} 滚动重解失败：{exc}")
            runner.note("energy_replan", t, {"ok": False, "error": str(exc)})
            return
        self.current_plan = plan
        # 目标线按事件前的正常负荷：预测里的事件附加负荷让计划提前安排储能与购电，突增本身仍由分钟层削
        self._slot_data.update({slot: (load - self._extra_load_kw(slot), pv)
                                for slot, (load, pv) in slot_series(forecasts, horizon).items()})
        self._slot_power.update(slot_powers(plan.plan))
        record = Replan(at=t, energy_kwh=runner.energy, plan_id=plan.plan.plan_id, solver_status=plan.result.solver_status,
                        elapsed_s=plan.elapsed_s, end_target_kwh=plan.end_target_kwh, end_floor_kwh=plan.end_floor_kwh,
                        initial_soc=runner.energy / self.packs.storage.capacity_kwh)
        self.replans.append(record)
        runner.note("energy_replan", t, {"ok": True, "plan_id": plan.plan.plan_id, "energy_kwh": round(runner.energy, 3),
                                         "end_target_kwh": plan.end_target_kwh, "end_floor_kwh": plan.end_floor_kwh,
                                         "elapsed_s": round(plan.elapsed_s, 3)})

    def _remaining_operations(self, t: datetime) -> tuple[list, frozenset[str]]:
        """重解时的待排工序：没开始的照常带窗口；已开始的按已执行进度固定（只剩未执行的形状、从 t 起不可移动）；已完成的不再排。"""
        operations, fixed = [], set()
        for op in self.packs.operations:
            start = self._job_start.get(op.operation_id) or self._planned_start(op.operation_id)
            if start is None or start >= t:
                operations.append(op)
                continue
            self._job_start[op.operation_id] = start
            done = int((t - start) / SLOT)
            if done >= op.slots:
                continue
            shape = dataclasses.replace(op.shape, fractions=op.shape.fractions[done:])
            operations.append(dataclasses.replace(op, shape=shape, original_start=t))
            fixed.add(op.operation_id)
        return operations, frozenset(fixed)

    def _minute_state(self, t: datetime) -> dict:
        state = self.runner.state
        fresh = self.inputs.minute(t)
        load = sum(state.power_kw.values()) if state.power_kw else sum(fresh.equipment_kw.values())
        # 光伏用上一分钟账本里已结算的实际可发功率（含事件对出力的缩放），基础输入不含事件影响
        last = self.runner.trace.ledger[-1].entry if self.runner.trace.ledger else None
        settled = last if last is not None and last.interval.end == t else None
        return {"at": t, "load_kw": load,
                "pv_kw": settled.pv_available_kwh * 60 if settled else fresh.pv_kw,
                "price_cny_per_kwh": settled.price_cny_per_kwh if settled else fresh.price_cny_per_kwh,
                "energy_kwh": self.runner.energy}

    def _target_for(self, t: datetime) -> SlotTarget:
        slot = t - timedelta(minutes=t.minute % 15, seconds=t.second, microseconds=t.microsecond)
        data = self._slot_data.get(slot)
        if data is None:
            profile = self.inputs.slot_profile(slot)
            data = (profile.load_kw, profile.pv_kw)
        power = self._slot_power.get(slot, {})
        price = self.inputs.minute(t).price_cny_per_kwh
        return SlotTarget(start=slot, end=slot + SLOT, load_kw=data[0] + power.get("operation_kw", 0.0), pv_kw=data[1],
                          charge_kw=power.get("charge_kw", 0.0), discharge_kw=power.get("discharge_kw", 0.0),
                          price_cny_per_kwh=price)

    def _context(self, as_of: datetime) -> Context:
        run = self.runner.run_info
        return Context(plant_id=self.db.plant_id, industry_pack="manufacturing", actor=ACTOR, channel="offline",
                       trace_id=f"scenario-{self.scenario.scenario_id}", as_of=as_of,
                       version_manifest_id=run.version_manifest_id, run_id=run.run_id)

    # ---- 查询与导出
    def slot_summaries(self, start: datetime | None = None, end: datetime | None = None) -> list[ScenarioSlotSummary]:
        """按分钟账本跨运行汇总成 15 分钟槽（分叉把一个槽拆成父/子两段时也能合回一整槽）。"""
        trace = self.trace or (self.runner.trace if self.runner else None)
        minutes = [m for m in trace.ledger if (start is None or m.entry.interval.start >= start)
                   and (end is None or m.entry.interval.start < end)]
        groups: dict[datetime, list] = {}
        for m in minutes:
            t = m.entry.interval.start
            key = t - timedelta(minutes=t.minute % 15)
            groups.setdefault(key, []).append(m)
        from plantkg.world.scenario.ledger import summarize_slot
        return [summarize_slot(key, rows) for key, rows in sorted(groups.items())]

    def minutes(self, start: datetime, end: datetime) -> list:
        trace = self.trace or (self.runner.trace if self.runner else None)
        return [m for m in trace.ledger if start <= m.entry.interval.start < end]

    def end_report(self) -> dict:
        """期末储能与目标的对照，及重解不可行时记下的缺口（kWh）。"""
        trace = self.trace or (self.runner.trace if self.runner else None)
        last = trace.ledger[-1].entry if trace and trace.ledger else None
        final = last.energy_end_kwh if last else None
        reached = last is not None and last.interval.end >= self.day_end
        return {"end_target_kwh": self.end_target_kwh, "final_energy_kwh": None if final is None else round(final, 3),
                "reached_day_end": reached,
                "end_gap_kwh": None if final is None else round(max(0.0, self.end_target_kwh - final), 3),
                "replan_failures": list(self.replan_failures)}

    def verify_soc(self, start: datetime | None = None, end: datetime | None = None, tol: float = 1e-6) -> dict:
        """独立核对分钟账本（账本平衡由构造保证，这里从控制器参数重算）：
        SOC 变化 == charge*eta_c - discharge/eta_d、SOC 在上下界内、充放功率不超上限、相邻分钟电量衔接。"""
        sp = self.packs.storage
        e_min, e_max = sp.soc_min * sp.capacity_kwh, sp.soc_max * sp.capacity_kwh
        problems = []
        previous = None
        rows = self.minutes(start or self.day_start, end or self.day_end)
        for m in rows:
            e = m.entry
            at = e.interval.start.isoformat()
            if abs((e.energy_end_kwh - e.energy_start_kwh) - (e.charge_kwh * sp.eta_c - e.discharge_kwh / sp.eta_d)) > tol:
                problems.append(f"{at} SOC 变化与充放电不符")
            if not (e_min - tol <= e.energy_end_kwh <= e_max + tol):
                problems.append(f"{at} 储能 {e.energy_end_kwh:.3f} kWh 越出 [{e_min:.0f}, {e_max:.0f}]")
            if e.charge_kwh * 60 > self.world.charge_kw + tol or e.discharge_kwh * 60 > self.world.discharge_kw + tol:
                problems.append(f"{at} 充放功率超上限")
            if previous is not None and e.interval.start == previous.interval.end and abs(e.energy_start_kwh - previous.energy_end_kwh) > tol:
                problems.append(f"{at} 与上一分钟电量不衔接")
            previous = e
        return {"ok": not problems, "checked_minutes": len(rows), "problems": problems[:5]}

    def window_cost(self, start: datetime, end: datetime) -> dict:
        """窗口内实际购电费用与“不充不放”基线费用的差额（元）。"""
        actual = baseline = 0.0
        discharge_kwh = charge_kwh = import_kwh = 0.0
        for m in self.minutes(start, end):
            e = m.entry
            price = e.price_cny_per_kwh or 0.0
            actual += e.cost_cny or 0.0
            without = e.grid_import_kwh + e.discharge_kwh - e.charge_kwh
            baseline += max(0.0, without) * price
            discharge_kwh += e.discharge_kwh
            charge_kwh += e.charge_kwh
            import_kwh += e.grid_import_kwh
        return {"actual_cny": round(actual, 2), "no_storage_cny": round(baseline, 2),
                "saved_cny": round(baseline - actual, 2), "discharge_kwh": round(discharge_kwh, 2),
                "charge_kwh": round(charge_kwh, 2), "grid_import_kwh": round(import_kwh, 2)}

    def _write_sources(self) -> None:
        trace = self.trace or (self.runner.trace if self.runner else None)
        for entry in (trace.runs if trace else ()):
            if entry.run_id in self._source_written:
                continue
            self.db.store.write_records(entry.run_id, [("sources", "inputs", self.day_start, {
                "items": self.inputs.source_table(), "nature_note": "仿真场景（外部工厂实测缩放底座 + 仿真分项）"})])
            self._source_written.add(entry.run_id)

    # ---- 场景运行身份
    def run_chain(self) -> list[dict]:
        return [entry.model_dump(mode="json") for entry in (self.trace.runs if self.trace else [])]


def open_demo_inputs(scenario: Scenario, *, site=None, profile: str = "dev", records: Sequence = ()):
    """场景输入 + 电价（B12 规则与计价基础）+ B22 记录接收。"""
    from plantkg.packs.tariff import load_tariff_config, load_tariff_rules, price_schedule
    from plantkg.decision.planner.rules import priced_rules
    from plantkg.decision.usecases.demo_inputs import DemoScenarioInputProvider

    if site is not None and not hasattr(site, "plant_id"):
        from plantkg.world.site.model import SiteModel
        site = SiteModel.load(site)
    rules = load_tariff_rules(scenario.resolve(scenario.sources.tariff_rules), scenario.sources.tariff_rules)
    pack, demo = load_tariff_config(scenario.resolve(scenario.sources.tariff_config), scenario.sources.tariff_config)

    def prices(slots):
        if not slots:
            return []
        horizon = Interval(start=slots[0].start, end=slots[-1].end)
        return [(p.price_cny_per_kwh, p.period) for p in price_schedule(priced_rules(rules, scenario.start, horizon),
                                                                       pack.base_prices, slots)]

    base = SiteInputProvider(scenario, prices, site=site,
                             price_note={"source": f"tariff:{Path(scenario.sources.tariff_rules).stem}", "demo_base_price": demo})
    return DemoScenarioInputProvider(base, profile=profile, records=records)


def run_day(site=None, date: str | None = None, *, scenario: Scenario | None = None, assembly: DemoAssembly | None = None,
            db_path=None, inputs=None, profile: str = "dev", reserve_ratio: float = 0.0,
            solver_budget=None, records: Sequence = (), registry: Registry | None = None) -> DayRun:
    """日前计划 + 门控 + 计划账本。`site` 为 B18 SiteModel（或路径）；`date` 为厂区当地日期。"""
    if profile not in PROFILES:  # 演示装配（合成材料、演示复核员）不能以 production 或未知 profile 名义运行
        raise PlantkgError(ErrorCode.PRECONDITION, f"run_day 是演示装配，只允许 profile {sorted(PROFILES)}，不接受 {profile!r}")
    scenario = scenario or load_scenario(DEFAULT_SCENARIO)
    local_start = _local_midnight(date or scenario.date, scenario)
    day_start = local_start
    day_end = day_start + timedelta(days=1)
    if day_start != scenario.start:
        raise PlantkgError(ErrorCode.INVALID_REQUEST,
                           f"run_day 的日期 {local_start.date()} 必须是剧本首日 {scenario.start.astimezone(scenario.tz).date()}")
    inputs = inputs or open_demo_inputs(scenario, site=site, profile=profile, records=records)
    if inputs.profile not in PROFILES:
        raise PlantkgError(ErrorCode.PRECONDITION, f"输入源 profile {inputs.profile!r} 不在演示装配允许的 {sorted(PROFILES)} 内")
    plant_id = inputs.base.site.plant_id
    assembly = assembly or build_demo_assembly(db_path or DEFAULT_DB, plant_id)
    packs = load_scenario_packs(scenario)
    capacity = packs.storage.capacity_kwh
    start_kwh = scenario.initial.soc * capacity
    end_target = scenario.initial.energy_end_target_kwh
    if end_target is None:
        end_target = start_kwh
    # 整日期末目标固定进 RunInfo（分叉时原样复制），重解一律显式传它
    scenario = scenario.model_copy(update={"initial": scenario.initial.model_copy(
        update={"energy_end_target_kwh": end_target})})

    db = assembly.db
    manifest = db.publish_knowledge(sorted(scenario.knowledge), scenario.policy_config(), f"B19 {scenario.scenario_id} 日前计划")
    ctx = Context(plant_id=plant_id, industry_pack="manufacturing", actor=ACTOR, channel="offline",
                  trace_id=f"scenario-{scenario.scenario_id}", as_of=day_start, version_manifest_id=manifest)
    plan_run = db.runs.create_run(ctx, "scenario", Interval(start=day_start, end=day_end), seed=scenario.seed,
                                  inputs={"scenario_id": scenario.scenario_id, "scenario_sha256": scenario.sha256,
                                          "inputs": inputs.provider_id, "purpose": "day_ahead_plan"},
                                  energy_end_target_kwh=end_target,
                                  data_sources=(DataSourceRef(source="b18-factory-site", dataset_version=inputs.base.site.config_sha256),))
    ctx = ctx.model_copy(update={"run_id": plan_run.run_id})
    snapshot = StateSnapshot(run_id=plan_run.run_id, snapshot_id=f"{plan_run.run_id}@{day_start.isoformat()}", event_seq=0,
                             as_of=day_start, version_manifest_id=manifest,
                             pack_states={"storage": {"energy_kwh": start_kwh}})
    horizon = Interval(start=day_start, end=day_end)
    forecasts = build_forecasts(inputs, horizon, ctx)
    plan = plan_day(packs, forecasts, snapshot, ctx, horizon, energy_start_kwh=start_kwh,
                    end_target_kwh=end_target, solver_budget=solver_budget)
    assembly.receipts.save(ctx, plan.receipt)
    assembly.ledgers.save(ctx, plan.ledger)

    baseline_plan = plan.plan.model_copy(update={"plan_id": f"{plan.plan.plan_id}-baseline", "actions": (),
                                                 "objective_terms": {}})
    base_cny = baseline_cost(forecasts, plan.prices, packs.storage, start_kwh, horizon, packs.operations)
    input_identity = f"scenario://{scenario.scenario_id}/{local_start.date()}"
    verified = packs.config_refs | {reserve_param_ref(manifest)}
    metadata = build_metadata(request=plan.request, forecasts=forecasts.forecasts, rules=packs.applicable_rules(horizon),
                              verified_refs=verified, plan_cost_cny=plan.ledger.total_cost_cny or 0.0,
                              baseline_cny=base_cny, baseline_plan=baseline_plan, input_identity=input_identity,
                              calibrations=scenario_calibrations(forecasts.forecasts))
    assembly.gate.register(plan.plan, ctx, metadata)
    decision = assembly.gate.assess(plan.plan.plan_id, 0, ctx)
    if decision.outcome == "review_required":
        # 门控已生成待确认动作：演示复核员确认，再读回批准后的计划（批准与执行依据都已落库）
        action = assembly.actions.get(decision.pending_action_id)
        assembly.actions.confirm(action.action_id, _reviewer(), "演示复核：接受场景预测与演示材料",
                                 action.revision, "b19-demo-confirm-" + plan.plan.plan_id)
        assembly.gate.load(plan.plan.plan_id)
    elif decision.outcome != "auto_confirmed":
        raise PlantkgError(ErrorCode.PRECONDITION, "门控拒绝了日前计划：" + "；".join(decision.reasons[:3]))
    executable = assembly.gate.executable(plan.plan.plan_id)

    world = ScenarioWorld.build(scenario, inputs)
    controller = RealtimeController(battery_id=world.index.battery_id, storage=world.storage,
                                    charge_kw=world.charge_kw, discharge_kw=world.discharge_kw,
                                    end_target_kwh=end_target, end_at=day_end)
    day = DayRun(scenario=scenario, world=world, db=db, inputs=inputs, packs=packs, assembly=assembly, plan=plan,
                 forecasts=forecasts, decision=decision, executable=executable, plan_run=plan_run, ctx=ctx,
                 day_start=day_start, day_end=day_end, end_target_kwh=end_target, controller=controller, registry=registry)
    day._slot_data.update(slot_series(forecasts, horizon))
    day._slot_power.update(slot_powers(plan.plan))
    return day


def _reviewer() -> Actor:
    return Actor(actor_id="b19-demo-reviewer", roles=(Role.reviewer, Role.operator))


def _floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _ceil_minute(t: datetime) -> datetime:
    floor = t.replace(second=0, microsecond=0)
    return floor if floor == t else floor + timedelta(minutes=1)


def _local_midnight(text: str, scenario: Scenario) -> datetime:
    day = datetime.fromisoformat(str(text)).replace(tzinfo=scenario.tz)
    return day.astimezone(scenario.start.tzinfo)
