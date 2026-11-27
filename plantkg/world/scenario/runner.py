"""场景世界运行（B17）：仿真时钟（1 分钟子步）、ScenarioExecutor、分钟级分叉、TrialRunner 与 RunTrace。

仿真时钟独立于 B07 的 15 分钟回放时钟，只由 ScenarioRunner 推进。每分钟：注入事件与值班操作 → 各仿真器 step →
ScenarioExecutor 按 B08 compute_ledger 结算一条分钟账本 → 记状态；每 15 分钟槽末求和成 ScenarioSlotSummary 并写场景账本表。
知识发布（上传资料、人工纠错）时在当前分钟分叉出子运行：复制快照与分叉前账本，ForkHook 交接业务状态，未关闭事件单重新派发。
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import random
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import yaml

from plantkg.core.config import DEFAULT_CONFIG_DIR, load_settings
from plantkg.core.contracts import (
    Actor, AgentRunRecord, Context, DayDecision, DayEvent, ErrorCode, ExpertProposal, Interval, KnowledgeVersionEntry, PlantkgError, ProposalCost,
    QaEntry, RunChainEntry, RunInfo, ScenarioSlotSummary, SimAction, StateChange, StateFrame, StateSnapshot, StatusChange, StorageParams, TrialResult,
    TrialViolation, WorkOrder,
)
from plantkg.core.ledger import LedgerMeta
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import SqliteVersionStore, utcnow
from plantkg.world.sim.base import MINUTE, SLOT, MinuteInputs, WorldState
from plantkg.world.site.model import SiteModel

from .coordinator import TimeCoordinator
from .dispatch import PROTECTED_FLAGS, DirectDispatcher, TempRuleBook, TicketBook, TicketState, temp_rule_from
from .experts import EXPERTS, DeterministicExperts
from .inputs import ScenarioInputProvider
from .ledger import Grid, MinuteRecord, check_slot_summary, minute_ledger, summarize_slot
from .script import Scenario, ScriptEvent
from .sims import ZONES, SiteIndex, default_simulators, event_effects, prune_effects
from .store import ScenarioStore

ACTOR = Actor(actor_id="b17-scenario-runner", roles=("operator",))
TRANSFORMER_ID = "TX1"  # 状态帧展示的变压器（《愿景》T1）；潮汐与限额都取分钟账本的同一口径
FORK_HOOK = "scenario_fork"  # Registry.register_hook 的名字：B27、B47 在这里登记 ForkHook
SCENARIO_END_HOOK = "scenario.end"  # Registry.register_hook 的名字：剧本跑完后触发一次 fn(runner)，B29 在这里解绑运行
SCENARIO_CONFIRM_HOOK = "scenario.confirm"  # 值班确认：B47 先处理自己的操作工单，没接时走直派器
SCENARIO_MINUTE_HOOK = "scenario.minute_layer"  # 分钟层：事件注入后、本分钟结算前调 fn(runner, at)，B19 能源分钟层在权威入口接这里
SCENARIO_SENSOR_TICK = "scenario.sensor_tick"  # 观测层每分钟钩子：fn(runner, at)，B22 用来把试探结论写回 sensor_case（#236）
INSTRUCTION_HOOK = "router.instruction"  # 值班指令处理器（B47 登记）：协同组由值守层转发的唯一生效路径


# ---------------------------------------------------------------- 世界装配


def _detach_inputs(value: MinuteInputs) -> MinuteInputs:
    """快照私有的分钟输入：两处 dict 浅拷贝即可（字段都是标量），不用 deepcopy（#perf）。"""
    return MinuteInputs(at=value.at, equipment_kw=dict(value.equipment_kw), pv_kw=value.pv_kw,
                        price_cny_per_kwh=value.price_cny_per_kwh, outdoor_c=value.outdoor_c,
                        occupancy=dict(value.occupancy))


@dataclass
class ScenarioWorld:
    scenario: Scenario
    site: SiteModel
    index: SiteIndex
    inputs: ScenarioInputProvider
    storage: StorageParams
    charge_kw: float
    discharge_kw: float
    grid: Grid
    simulators: list
    frozen_inputs: dict[str, dict[datetime, MinuteInputs]] = field(default_factory=dict)  # 快照 → 不可变的分钟输入视图

    def freeze_inputs(self, snapshot_id: str, start: datetime,
                      inputs: Callable[[datetime], MinuteInputs] | None = None) -> dict[datetime, MinuteInputs]:
        """把快照时刻到剧本结束的全部分钟输入浅拷贝定格（两个 dict 独立拷贝，字段都是标量），绑定到该快照；
        同一快照只定格一次，之后输入源刷新不影响它。"""
        if snapshot_id not in self.frozen_inputs:
            minutes = int((self.scenario.end - start) / MINUTE)
            source = inputs or self.inputs.minute
            self.frozen_inputs[snapshot_id] = {t: _detach_inputs(source(t))
                                               for t in (start + m * MINUTE for m in range(minutes))}
        return self.frozen_inputs[snapshot_id]

    @classmethod
    def build(cls, scenario: Scenario, inputs: ScenarioInputProvider, simulators: Sequence | None = None,
              registry: Registry | None = None) -> ScenarioWorld:
        site = inputs.site
        st = yaml.safe_load(scenario.resolve(scenario.sources.storage).read_text(encoding="utf-8"))["storage"]
        storage = StorageParams(capacity_kwh=st["capacity_kwh"], eta_c=st["eta_c"], eta_d=st["eta_d"], soc_min=st["soc_min"], soc_max=st["soc_max"])
        index = SiteIndex(site, st["entity_id"])
        return cls(scenario=scenario, site=site, index=index, inputs=inputs, storage=storage, charge_kw=float(st["charge_kw"]),
                   discharge_kw=float(st["discharge_kw"]), grid=Grid(index, scenario.params.capacity_kw),
                   simulators=list(simulators) if simulators is not None else default_simulators(index, registry or default_registry()))

    def zone_flags(self, entity_id: str) -> list[str]:
        zone = self.index.zone_of(entity_id)
        return list(self.site.zones.get(zone, {}).get("flags", ())) if zone else []

    def ids(self) -> dict[str, str]:
        out = {e.code: e.entity_id for e in self.site.equipment}
        out |= {f"zone:{z}": eid for z, eid in self.index.zone_eid.items()}
        out |= {gid.rsplit("/", 1)[1]: gid for gid in self.index.groups}
        out |= {"battery": self.index.battery_id, "air_system": self.index.air_eid}
        return out

    def initial_state(self) -> tuple[WorldState, float]:
        s = self.scenario
        state = WorldState(at=s.start, devices={}, rng=random.Random(s.seed).getstate())
        state.sims["actions"] = []
        for sim in self.simulators:
            sim.init(state)
        energy = s.initial.soc * self.storage.capacity_kwh
        state.devices[self.index.battery_id] = {"soc": round(s.initial.soc, 6), "mode": "idle", "power_kw": 0.0}
        return state, energy

    def advance(self, state: WorldState, energy_kwh: float, meta: LedgerMeta,
                inputs: Callable[[datetime], MinuteInputs] | None = None) -> tuple[MinuteRecord, float]:
        """推进一分钟：物理仿真器 step → 储能按 SOC 与“先弃光再削减放电”裁剪 → 分钟账本 → observe 仿真器 → 推钟。
        observe（stage="observe"，如传感器观测层）读的是本分钟结算后的最终物理状态，只读、不计入本分钟功率与账本。
        inputs 缺省读正式输入源；试算传入冻结的输入视图。"""
        t = state.at
        state.inputs = (inputs or self.inputs.minute)(t)
        prune_effects(state)
        acts = active_actions(state, t)
        state.power_kw = {}
        r = state.random()
        state.sims["noise"] = r.random()  # 仿真器可读的本分钟随机数（随快照复制，同一种子同一结果）
        state.keep_random(r)
        observe = [sim for sim in self.simulators if getattr(sim, "stage", "physics") == "observe"]
        for sim in self.simulators:
            if sim not in observe:
                state = sim.step(state, acts, MINUTE)
        bat = self.index.battery_id
        request = 0.0
        for a in acts:
            if a.entity_id == bat and a.command in ("charge", "discharge"):
                request = float(a.params.get("power_kw", 0.0)) * (1 if a.command == "charge" else -1)
        pv = state.inputs.pv_kw
        for e in state.effect("pv_scale"):
            pv *= e["factor"]
        load = sum(state.power_kw.values())
        sp = self.storage
        e_min, e_max = sp.soc_min * sp.capacity_kwh, sp.soc_max * sp.capacity_kwh
        charge = min(max(request, 0.0), self.charge_kw, max(0.0, (e_max - energy_kwh) / sp.eta_c * 60))
        discharge = min(max(-request, 0.0), self.discharge_kw, max(0.0, (energy_kwh - e_min) * sp.eta_d * 60), max(0.0, load - pv))
        record = minute_ledger(t, pv_kw=pv, power_kw=state.power_kw, charge_kw=charge, discharge_kw=discharge,
                               price=state.inputs.price_cny_per_kwh, storage=sp, energy_start_kwh=energy_kwh, meta=meta,
                               index=self.index, grid=self.grid)
        energy = record.entry.energy_end_kwh
        tx_limit = self.grid.limits.get(TRANSFORMER_ID)
        if tx_limit:
            # 方案 2（#216-2）：变压器有功直接取分钟账本的变压器潮流（LV1 子树负荷 − 光伏 − 放电 + 充电），
            # 负载率按同一有效限额算（剧本 capacity_kw 覆盖优先），不再另算一套口径。
            power_kw = float(record.flows_kw.get(TRANSFORMER_ID, 0.0))
            state.devices[self.index.node_eid(TRANSFORMER_ID)] = {
                "power_kw": round(power_kw, 3), "load_ratio": round(power_kw / tx_limit, 4), "nature": "simulated"}
        state.devices[bat] = {"soc": round(energy / sp.capacity_kwh, 6), "mode": "charge" if charge else "discharge" if discharge else "idle",
                              "power_kw": round(charge - discharge, 3)}
        for sim in observe:
            state = sim.step(state, acts, MINUTE)
        state.at = t + MINUTE
        return record, energy


def active_actions(state: WorldState, t: datetime) -> list[SimAction]:
    out = []
    for item in state.sims.get("actions", []):
        a = SimAction.model_validate(item["action"])
        if a.interval.start < t + MINUTE and a.interval.end > t:
            out.append(a)
    return out


def _capacity_node(detail: str) -> str | None:
    """容量违规文案“<at> <节点> <flow> kW > 容量 …”里的节点名，用于相对基线的同节点同分钟比较。"""
    parts = detail.split()
    return parts[1] if len(parts) > 1 else None


_CAPACITY_FLOW = re.compile(r"([\d.]+) kW > 容量")
_BAND_VALUE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:℃|%RH)")
_BAND_RANGE = re.compile(r"\[(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\]")


def _violation_severity(violation: TrialViolation) -> float | None:
    """同一（类型, 实体, 分钟）上“是否加重”的可比量：容量取流量 kW，硬区取超出带外的距离；解析不了返回 None。"""
    if violation.kind == "capacity":
        flow = _CAPACITY_FLOW.search(violation.detail)
        return float(flow.group(1)) if flow else None
    value, band = _BAND_VALUE.search(violation.detail), _BAND_RANGE.search(violation.detail)
    if violation.kind == "hard_zone" and value and band:
        measured, lo, hi = float(value.group(1)), float(band.group(1)), float(band.group(2))
        return max(lo - measured, measured - hi, 0.0)
    return None


class ScenarioExecutor:
    """场景执行入口：只接受 RunInfo.kind=scenario，不复用 B16 的提交路径。账本口径 = B08 compute_ledger，按分钟。"""

    def __init__(self, world: ScenarioWorld) -> None:
        self.world = world

    def meta(self, run: RunInfo) -> LedgerMeta:
        return LedgerMeta(ledger_id=f"scenario:{run.run_id}:minutes", kind="execution", run_id=run.run_id, plan_id=f"scenario:{run.run_id}",
                          plan_revision=0, version_manifest_id=run.version_manifest_id, source_refs=(self.world.inputs.provider_id,))

    def execute_minute(self, run: RunInfo, state: WorldState, energy_kwh: float,
                       inputs: Callable[[datetime], MinuteInputs] | None = None) -> tuple[MinuteRecord, float]:
        if run.kind != "scenario":
            raise PlantkgError(ErrorCode.PRECONDITION, f"ScenarioExecutor 只接受场景运行，{run.run_id} 是 {run.kind}")
        if run.status != "running":
            raise PlantkgError(ErrorCode.STATE_CONFLICT, f"运行 {run.run_id} 处于 {run.status}，不能推进")
        return self.world.advance(state, energy_kwh, self.meta(run), inputs=inputs)


# ---------------------------------------------------------------- 存储与知识版本


@dataclass
class ScenarioDB:
    writer: Any
    versions: SqliteVersionStore
    runs: SqliteRunStore
    store: ScenarioStore
    plant_id: str

    @classmethod
    def from_writer(cls, writer: Any, plant_id: str) -> ScenarioDB:
        """writer 由装配层给出（已执行迁移，含 B04 版本与运行表、B17 场景表）。"""
        versions = SqliteVersionStore(writer, plant_id)
        for prefix in ("scenario-knowledge:", "scenario-policy:"):
            versions.register_verifier(prefix, lambda c: "complete")
        return cls(writer=writer, versions=versions, runs=SqliteRunStore(writer, plant_id, versions), store=ScenarioStore(writer, plant_id),
                   plant_id=plant_id)

    def publish_knowledge(self, tags: Sequence[str], policy: Any, reason: str) -> str:
        """知识发布：准备 → 校验 → 激活一份清单（rules 组件 = 知识标签，policy 组件 = 裁决与响应预算）。返回清单 ID。"""
        body = {"tags": sorted(tags)}
        components = {
            "rules": {"ref": "scenario-knowledge:" + ("+".join(sorted(tags)) or "empty"), "content_hash": "sha256:" + _sha(body)},
            "policy": {"ref": "scenario-policy:" + policy.policy_version, "content_hash": "sha256:" + _sha(policy.model_dump(mode="json"))},
        }
        manifest = self.versions.prepare(components, reason)
        if manifest.status == "prepared":
            manifest = self.versions.check_integrity(manifest.version_manifest_id)
        if manifest.status != "active":
            current = None
            try:
                current = self.versions.active().version_manifest_id
            except PlantkgError:
                current = None
            manifest = self.versions.activate(current, manifest.version_manifest_id, ACTOR, reason)
        return manifest.version_manifest_id

    def close(self) -> None:
        self.writer.close()


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------- 运行轨迹


@dataclass
class RunTrace:
    scenario_id: str
    capacity_kwh: float | None = None  # 储能容量：摘要里的 SOC 前缀字段一律换算成比例
    runs: list[RunChainEntry] = field(default_factory=list)
    events: list[DayEvent] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    states: list[tuple[datetime, dict]] = field(default_factory=list)  # 每分钟（step 之后）的设备状态
    ledger: list[MinuteRecord] = field(default_factory=list)
    slots: list[ScenarioSlotSummary] = field(default_factory=list)
    slot_problems: list[str] = field(default_factory=list)
    frames: list[StateFrame] = field(default_factory=list)
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    proposals: list[ExpertProposal] = field(default_factory=list)
    decisions: list[DayDecision] = field(default_factory=list)
    tickets: dict[str, dict] = field(default_factory=dict)
    work_orders: dict[str, WorkOrder] = field(default_factory=dict)
    temp_rules: dict[str, Any] = field(default_factory=dict)
    knowledge_versions: list[KnowledgeVersionEntry] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)  # 准入、迟到入历史、转人工、抽查、硬约束拦截

    @property
    def run_id(self) -> str:
        return self.runs[-1].run_id

    def state_at(self, at: datetime) -> dict:
        for t, s in self.states:
            if t == at:
                return s
        raise KeyError(at)

    def ledger_summary(self) -> dict:
        es = [m.entry for m in self.ledger]
        tot = lambda a: round(sum(getattr(e, a) for e in es), 3)  # noqa: E731
        costs = [e.cost_cny for e in es]
        start_kwh = es[0].energy_start_kwh if es else None
        end_kwh = es[-1].energy_end_kwh if es else None
        # soc_ 前缀字段同为比例（0～1）；电量原值另用 energy_ 前缀字段给出
        soc_start = round(start_kwh / self.capacity_kwh, 4) if start_kwh is not None and self.capacity_kwh else None
        return {
            "minutes": len(es), "minutes_balanced": sum(m.balanced for m in self.ledger), "slots": len(self.slots),
            "slots_minutes_balanced": sum(s.minutes_balanced for s in self.slots), "slot_problems": len(self.slot_problems),
            "energy_kwh": {k: tot(f"{k}_kwh") for k in ("load", "pv_available", "pv_used", "pv_curtailed", "grid_import", "charge", "discharge")},
            "cost_cny": None if any(c is None for c in costs) else round(sum(costs), 2),
            "peak_minute_kw": round(max((s.peak_minute_kw for s in self.slots), default=0.0), 1),
            "max_demand_15m_kw": round(max((s.demand_15m_kw for s in self.slots), default=0.0), 1),
            "capacity_violation_minutes": sum(1 for m in self.ledger if m.capacity_violations),
            "soc_start": soc_start, "soc_end": es[-1].soc_end if es else None,
            "energy_start_kwh": round(start_kwh, 1) if start_kwh is not None else None,
            "energy_end_kwh": round(end_kwh, 1) if end_kwh is not None else None,
        }

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for n in self.notes:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        return {
            "scenario_id": self.scenario_id, "run_id": self.run_id,
            "runs": [r.model_dump(mode="json") for r in self.runs],
            "events": len(self.events), "tickets": {k: v["status"] for k, v in self.tickets.items()},
            "actions": len([a for a in self.actions if a.get("executed", True)]), "work_orders": {k: v.status for k, v in self.work_orders.items()},
            "temp_rules": len(self.temp_rules), "knowledge_versions": [k.version for k in self.knowledge_versions],
            "agent_runs": len(self.agent_runs), "notes": kinds, "ledger": self.ledger_summary(),
        }


def tracked(state: WorldState) -> dict:
    """本分钟的设备状态快照：字段都是标量，逐实体浅拷贝即可（#perf）。"""
    return {entity_id: dict(fields) for entity_id, fields in state.devices.items()}


def build_frame(states: Sequence[tuple[datetime, dict]]) -> StateFrame:
    """15 分钟全量帧 + 帧内按分钟的变化（B46 state_series）。"""
    t0, first = states[0]
    changes = []
    prev = first
    for t, cur in states[1:]:
        for eid, fields in cur.items():
            before = prev.get(eid, {})
            for k, v in fields.items():
                if before.get(k) != v:
                    changes.append(StateChange(at=t, entity_id=eid, field=k, value=v))
        prev = cur
    return StateFrame(at=t0, devices=copy.deepcopy(first), changes=tuple(changes))


def state_from_frames(frames: Sequence[StateFrame], at: datetime) -> dict:
    """由 15 分钟帧与分钟变化重建任一分钟的设备状态（展示页离线重建用同一算法）。"""
    frame = next(f for f in frames if f.at <= at < f.at + SLOT)
    devices = copy.deepcopy(frame.devices)
    for ch in frame.changes:
        if ch.at <= at:
            devices.setdefault(ch.entity_id, {})[ch.field] = ch.value
    return devices


# ---------------------------------------------------------------- 运行


class ScenarioRunner:
    """ScenarioRunner.run(scenario, until) → RunTrace。mode：live / trial / evaluation（决定专家结果来源与是否等墙钟）。

    group：independent 走直派器（独立组对照）；coordinated 走 DISPATCHER_HOOK 登记的派发器（协同组，值守层）。
    """

    def __init__(self, world: ScenarioWorld, db: ScenarioDB, registry: Registry | None = None, *, mode: str = "evaluation",
                 wall: Callable[[], datetime] = utcnow, hooks: Sequence[Any] = (), group: str = "independent") -> None:
        self.world, self.db = world, db
        self.registry = registry or default_registry()
        self.mode = mode
        self.policy = world.scenario.policy_config()
        self.coordinator = TimeCoordinator(self.registry, mode, self.policy, wall=wall)
        self.executor = ScenarioExecutor(world)
        self.tickets, self.temp_rules = TicketBook(), TempRuleBook()
        self.dispatcher = self._dispatcher(group)
        self.hooks = [self.tickets, self.temp_rules, self.dispatcher, *hooks]
        self.trace = RunTrace(scenario_id=world.scenario.scenario_id, capacity_kwh=world.storage.capacity_kwh)
        self.ids = world.ids()
        self.run_info: RunInfo | None = None  # 当前运行（分叉后是子运行）
        self._end_hook_fired = False  # 结束钩子只对同一 runner 触发一次
        self.edit_lock = threading.RLock()  # 推进一分钟、分叉与人工取消/撤销动作互斥，顺序以拿到锁的先后为准（#245）
        self._seq = 0
        self._ids: dict[str, int] = {}
        self.coordinator.next_id = self.next_id

    def _dispatcher(self, group: str) -> DirectDispatcher:
        routes = self.world.scenario.dispatch
        if group == "independent":
            return DirectDispatcher(self, routes)
        factories = self.registry.hooks_for(DISPATCHER_HOOK)
        if group != "coordinated" or not factories:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"运行分组 {group} 不可用（coordinated 需要启用值守层领域包）")
        return factories[-1](self, routes)

    # ---- 启动与推进
    def start(self) -> RunInfo:
        s = self.world.scenario
        self.knowledge = set(s.knowledge)
        manifest_hooks = self.registry.hooks_for("scenario.initial_manifest")
        manifest = manifest_hooks[-1](self) if manifest_hooks else None
        if manifest is None:
            manifest = self.db.publish_knowledge(sorted(self.knowledge), self.policy, f"场景 {s.scenario_id} 起始知识")
        self.knowledge_version = self.db.versions.knowledge_version(manifest)
        sources_of = getattr(self.world.inputs, "data_sources", None)  # 协议外的实现可不提供，回退空
        self.run_info = self.db.runs.create_run(self._context(manifest), "scenario", Interval(start=s.start, end=s.end), seed=s.seed,
                                           inputs=self._inputs(), energy_end_target_kwh=s.initial.energy_end_target_kwh,
                                           data_sources=tuple(sources_of()) if sources_of else ())
        for register_runner in self.registry.hooks_for("evolution.register_scenario_runner"):
            register_runner(self)
        self.run_info = self.db.runs.append_event(self.run_info.run_id, "started", ACTOR, {"mode": self.mode}, to_status="running")
        self.state, self.energy = self.world.initial_state()
        self.state.sims.setdefault("sensors", {})["run_id"] = self.run_info.run_id  # 观测总线按运行隔离（#186）
        self.trace.runs.append(RunChainEntry(run_id=self.run_info.run_id, knowledge_version=self.knowledge_version,
                                             data_sources=self.run_info.data_sources))
        self._kv(s.start, manifest, "起始知识")
        for started in self.registry.hooks_for("scenario.started"):  # 起始清单发布后登记场景校验器（P1-B2）
            started(self)
        self.queue = [("event", s.instant(e.at), e) for e in s.expanded_events()] + \
                     [("operator", s.instant(a.at), a) for a in s.operator_actions]
        self.queue.sort(key=lambda x: (x[1], x[0] == "event"))
        self.slot_minutes: list[MinuteRecord] = []
        self.slot_states: list[tuple[datetime, dict]] = []
        self.warned: dict[str, datetime] = {}
        for item in s.initial_actions:  # 剧本开局的设备动作（如备用机先停机），走与运行期一致的 apply 记录路径
            at = self.state.at
            self.apply(SimAction(entity_id=self.world.index.eid(item.entity), command=item.command, params=dict(item.params),
                                 interval=Interval(start=at, end=at + item.minutes * MINUTE)),
                       source={"scenario": "initial"})
        return self.run_info

    def run_until(self, until: datetime | None = None) -> RunTrace:
        end = min(until or self.world.scenario.end, self.world.scenario.end)
        while self.state.at < end:
            with self.edit_lock:
                self.step()
        if self.state.at >= self.world.scenario.end and self.run_info.status == "running":
            self.run_info = self.db.runs.append_event(self.run_info.run_id, "finished", ACTOR, self.trace.ledger_summary(), to_status="finished")
            if not self._end_hook_fired:
                self._end_hook_fired = True
                for hook in self.registry.hooks_for(SCENARIO_END_HOOK):
                    try:
                        hook(self)
                    except Exception:  # noqa: BLE001 — 结束钩子失败不影响运行结果
                        pass
        return self.trace

    def run(self, scenario: Scenario | None = None, until: datetime | None = None) -> RunTrace:
        """ScenarioRunner.run(scenario, until) → RunTrace（任务卡接口）。"""
        if scenario is not None and scenario is not self.world.scenario:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "换剧本请新建 ScenarioWorld 与 ScenarioRunner")
        if self.run_info is None:
            self.start()
        return self.run_until(until)

    @property
    def now(self) -> datetime:
        return self.state.at

    def step(self) -> None:
        t = self.state.at
        for hook in self.registry.hooks_for("scenario.knowledge_step"):
            hook(self, t)
        if (t - self.world.scenario.start) % SLOT == timedelta(0):
            self.slot_minutes, self.slot_states = [], []
        while self.queue and self.queue[0][1] < t + MINUTE:
            kind, at, item = self.queue.pop(0)
            self._inject(item, at) if kind == "event" else self._operate(item, at)
        self.dispatcher.step(t)
        for hook in self.registry.hooks_for(SCENARIO_MINUTE_HOOK):  # 分钟层：事件注入后、本分钟结算前给当分钟动作（#188）
            hook(self, t)
        for hook in self.registry.hooks_for(SCENARIO_SENSOR_TICK):  # 观测层结论落库（#236）：不占 B19 分钟层钩子
            hook(self, t)
        input_hooks = self.registry.hooks_for("scenario.minute_inputs")
        def minute_inputs(at):
            value = self.world.inputs.minute(at)
            for hook in input_hooks:
                value = hook(self, at, value)
            return value
        record, self.energy = self.executor.execute_minute(self.run_info, self.state, self.energy,
                                                            inputs=minute_inputs if input_hooks else None)
        snap = tracked(self.state)
        self.trace.ledger.append(record)
        self.trace.states.append((t, snap))
        self.slot_minutes.append(record)
        self.slot_states.append((t, snap))
        self._warn_capacity(t, record)
        if (t + MINUTE - self.world.scenario.start) % SLOT == timedelta(0):
            self._close_slot(t + MINUTE - SLOT)

    def _close_slot(self, slot_start: datetime) -> None:
        summary = summarize_slot(slot_start, self.slot_minutes)
        problems = check_slot_summary(summary)
        self.trace.slot_problems += problems
        frame = build_frame(self.slot_states)
        self.trace.slots.append(summary)
        self.trace.frames.append(frame)
        self.db.store.write_slot(self.run_info.run_id, self.slot_minutes, summary, frame)

    def _warn_capacity(self, t: datetime, record: MinuteRecord) -> None:
        ratio = self.world.scenario.params.capacity_warn_ratio
        if any(a.entity_id == self.world.index.battery_id and a.command == "discharge" for a in active_actions(self.state, self.state.at)):
            return  # 储能已在放电削峰：不重复告警
        for node, limit in self.world.grid.limits.items():
            flow = record.flows_kw.get(node, 0.0)
            if flow > ratio * limit and (node not in self.warned or t - self.warned[node] >= timedelta(minutes=30)):
                self.warned[node] = t
                at = t + MINUTE
                ev = ScriptEvent(at=at.isoformat(), type="sim_limit", severity=3, source="sim_limit",
                                 summary=f"{node} 负载 {flow:.0f} kW 达容量 {limit:.0f} kW 的 {flow / limit:.0%}",
                                 payload={"node": node, "flow_kw": round(flow, 1), "limit_kw": limit, "effects": []})
                self.queue.insert(0, ("event", at, ev))

    # ---- 事件与值班操作
    def _event(self, at: datetime, event_type: str, source: str, entity_id: str | None, severity: int, summary: str, payload: dict,
               event_id: str | None = None) -> DayEvent:
        self._seq += 1
        ev = DayEvent(event_id=event_id or f"EV-{self._seq:04d}", at=at, event_type=event_type, source=source, entity_id=entity_id,
                      severity=severity, summary=summary or event_type, payload=payload, nature=self.world.scenario.nature)
        self.trace.events.append(ev)
        self.db.store.write_records(self.run_info.run_id, [("event", ev.event_id, at, ev.model_dump(mode="json"))])
        return ev

    def _inject(self, se: ScriptEvent, at: datetime) -> None:
        eid = self.world.index.eid(se.entity_id) if se.entity_id and se.source != "sim_limit" else se.entity_id
        ev = self._event(at, se.type, se.source, eid, se.severity, se.summary, se.payload, se.id)
        effects = event_effects(se.type, eid, se.payload, at, self.world.index) if se.severity > 0 else []  # 噪声告警没有物理影响
        if effects:
            self.state.effects.extend(effects)
            self.state.state_version += 1
        self.dispatcher.admit(ev)

    def _operate(self, op: Any, at: datetime) -> None:
        p = op.payload
        if op.kind == "instruction":
            ev = self._event(at, "operator_instruction", "operator_message", None, 2, p["text"], p)
            if getattr(self.dispatcher, "router", None) is not None and self.registry.hooks_for(INSTRUCTION_HOOK):
                # 协同组：剧本指令与事件入口同走 B27→B47，只保留这一条生效路径
                self.dispatcher.admit(ev)
                return
            # 兜底（独立组未经值守层派发）：剧本侧按确定性解析直接转临时规则，标 synthetic
            self.note("instruction_fallback", at, {"event_id": ev.event_id, "path": "b17_scenario", "nature": "synthetic"})
            rule, actions = temp_rule_from(p, at, self.world.scenario, self.world.index, ev.event_id, self.next_id("TR"))
            end = self.world.scenario.end
            if rule.effective.end > end:  # 跨天/越过剧本结束：不登记规则、不下发动作，记 rejected 后继续运行
                self.note("rejected", at, {"event_id": ev.event_id, "rule_text": rule.text, "zone": p["zone"],
                                           "entity_id": rule.entity_ids[0],
                                           "reason": f"临时规则结束 {rule.effective.end.isoformat()} 超过剧本结束 {end.isoformat()}，临时规则未生效"})
                return
            hard = sorted({f for a in actions for f in set(self.world.zone_flags(a.entity_id)) & PROTECTED_FLAGS})
            delta = None if hard else self.estimate_cost(actions, rule.effective.end)
            if delta is None:  # 硬约束区或试算不可行：不登记规则、不下发动作
                self.note("rejected", at, {"event_id": ev.event_id, "rule_text": rule.text, "zone": p["zone"], "entity_id": rule.entity_ids[0],
                                           "reason": f"硬约束：区域标记 {hard} 不能改设定，临时规则未生效" if hard else "试算不可行，临时规则未生效"})
                return
            rule = rule.model_copy(update={"estimated_cost_cny": round(delta, 2)})
            self.temp_rules.add(rule)
            self.trace.temp_rules[rule.rule_id] = rule
            self.db.store.write_records(self.run_info.run_id, [("temp_rule", rule.rule_id, at, rule.model_dump(mode="json"))])
            for a in actions:
                self.apply(a, {"temp_rule": rule.rule_id, "event_id": ev.event_id})
        elif op.kind == "upload":
            self._event(at, "document_upload", "operator_message", None, 1, p.get("summary", p.get("document", "上传资料")), p)
            providers = self.registry.hooks_for("scenario.document_upload")
            if providers:
                providers[-1](self, at, p)
            else:
                self.publish(at, p.get("adds", []), p.get("summary", p.get("document", "上传资料")))
        elif op.kind == "confirm":
            ev = self._event(at, "operator_confirm", "operator_message", None, 1, f"确认 {p['event']} 的操作工单", p)
            handled = False
            for hook in self.registry.hooks_for(SCENARIO_CONFIRM_HOOK):
                try:
                    handled = bool(hook(self, at, p["event"], ev)) or handled
                except Exception:  # noqa: BLE001 — 处理器异常不撤销已处理的确认，免得双重执行
                    continue
            if not handled:
                self.dispatcher.confirm(p["event"], at)
        elif op.kind == "correct":
            self._event(at, "operator_correct", "operator_message", None, 1, p.get("summary", "人工纠错"), p)
            self.dispatcher.close(p["event"], at, "人工纠错：" + p.get("summary", ""))
            providers = self.registry.hooks_for("scenario.document_upload")
            if providers:
                providers[-1](self, at, p)
            else:
                self.publish(at, p.get("adds", []), p.get("summary", "人工纠错"))

    # ---- 动作
    def apply(self, action: SimAction, source: dict | None = None) -> None:
        """apply(SimAction)：设备动作直接作用到仿真，不经 Plan 与 B15。生效区间内每分钟由仿真器读取。"""
        if self.world.index.zone_of(action.entity_id) is None and action.entity_id != self.world.index.battery_id \
                and action.entity_id != self.world.index.air_eid and "/elevator_group/" not in action.entity_id:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"动作对象 {action.entity_id} 不在场景装配中")
        action_id = f"A-{len(self.trace.actions) + 1:05d}"
        self.state.sims["actions"].append({"action": action.model_dump(mode="json"), "source": source or {}, "action_id": action_id})
        self.state.state_version += 1
        rec = {"action": action.model_dump(mode="json"), "source": source or {}, "applied_at": self.now.isoformat(), "executed": True,
               "action_id": action_id}
        self.trace.actions.append(rec)
        self.db.store.write_records(self.run_info.run_id, [("action", action_id, action.interval.start, rec)])
        for hook in self.registry.hooks_for("scenario.action_recorded"):
            hook(self, rec)

    # ---- 知识发布与分叉
    def publish(self, at: datetime, adds: Sequence[str], summary: str) -> RunInfo:
        tags = self.knowledge | set(adds)
        manifest = self.db.publish_knowledge(sorted(tags), self.policy, summary or "知识发布")
        return self.fork(at, manifest, tags, summary)

    def fork(self, at: datetime, manifest_id: str | None = None, knowledge: set[str] | None = None, summary: str = "") -> RunInfo:
        """在当前分钟分叉：子运行 forked_from=父运行、forked_at=at，复制快照与分叉前账本，ForkHook 交接，未关闭事件单重新派发。"""
        with self.edit_lock:
            return self._fork(at, manifest_id, knowledge, summary)

    def _fork(self, at: datetime, manifest_id: str | None, knowledge: set[str] | None, summary: str) -> RunInfo:
        if at != self.state.at:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"只能在当前仿真分钟 {self.state.at.isoformat()} 分叉")
        parent = self.run_info
        manifest_id = manifest_id or parent.version_manifest_id
        self.db.store.write_slot(parent.run_id, self.slot_minutes, None)  # 槽内已结算的分钟先落父运行
        snap = self.snapshot()
        self.db.store.save_snapshot(parent.run_id, at, snap.model_dump(mode="json"))
        self.knowledge = set(knowledge if knowledge is not None else self.knowledge)
        child = self.db.runs.create_run(self._context(manifest_id), "scenario", parent.clock, forked_from=parent.run_id, forked_at=at,
                                        seed=self.world.scenario.seed, inputs=self._inputs() | {"fork_snapshot": snap.snapshot_id},
                                        energy_end_target_kwh=parent.energy_end_target_kwh, data_sources=parent.data_sources)
        self.db.store.copy_before(parent.run_id, child.run_id, at)
        self.db.runs.append_event(parent.run_id, "forked", ACTOR, {"child": child.run_id, "at": at.isoformat()}, to_status="finished")
        child = self.db.runs.append_event(child.run_id, "started", ACTOR, {"forked_from": parent.run_id}, to_status="running")
        self.db.store.save_snapshot(child.run_id, at, snap.model_dump(mode="json"))
        for hook in [*self.hooks, *self.registry.hooks_for(FORK_HOOK)]:
            (hook.on_fork if hasattr(hook, "on_fork") else hook)(parent, child, at)
        self.run_info = child
        self.state.sims.setdefault("sensors", {})["run_id"] = child.run_id  # 分叉后观测总线换到子运行（#186）
        self.knowledge_version = self.db.versions.knowledge_version(manifest_id)
        self.trace.runs.append(RunChainEntry(run_id=child.run_id, forked_from=parent.run_id, forked_at=at,
                                             knowledge_version=self.knowledge_version, data_sources=child.data_sources))
        if manifest_id != parent.version_manifest_id:
            self._kv(at, manifest_id, summary)
        self.dispatcher.redispatch(at)
        for rule in self.temp_rules.rules.values():
            self.db.store.write_records(child.run_id, [("temp_rule", rule.rule_id, rule.created_at, rule.model_dump(mode="json"))])
        return child

    def snapshot(self) -> StateSnapshot:
        """当前分钟开始时的快照（设备、队列、动作、随机数状态与储能电量），供分叉与 TrialRunner。"""
        snapshot_id = f"{self.run_info.run_id}@{self.state.at.isoformat()}"
        hooks = self.registry.hooks_for("scenario.minute_inputs")
        as_of = self.state.at
        def frozen_inputs(at):
            value = self.world.inputs.minute(at)
            for hook in hooks:
                value = hook(self, at, value, as_of=as_of)
            return value
        self.world.freeze_inputs(snapshot_id, as_of, frozen_inputs if hooks else None)  # 同时定格版本与外部输入
        return StateSnapshot(run_id=self.run_info.run_id, snapshot_id=snapshot_id,
                             event_seq=int((self.state.at - self.world.scenario.start) / MINUTE), as_of=self.state.at,
                             version_manifest_id=self.run_info.version_manifest_id,
                             pack_states={"world": copy.deepcopy(self.state.to_json()),  # 深拷贝：正式运行继续推进不改写快照
                                          "storage": {"energy_kwh": self.energy}})

    def estimate_cost(self, actions: Sequence[SimAction], until: datetime) -> float | None:
        """临时规则代价：同一快照、同一输入，分别试算有无该动作到规则结束，费用差（元）。
        只有候选新增/加重的硬约束违规才返回 None；既有越界不阻断（#173 相对语义）。"""
        if until <= self.state.at:
            return 0.0
        trial = TrialRunner(self.world)
        horizon = Interval(start=self.state.at, end=until)
        snap = self.snapshot()
        result = trial.trial(snap, actions, horizon)
        if not result.feasible:
            return None
        baseline = result.baseline_soft_cost.cny if result.baseline_soft_cost is not None else 0.0
        return result.soft_cost.cny - baseline

    # ---- 供直派器调用的记录
    def next_id(self, prefix: str) -> str:
        """按运行链计数的编号（工单、提案、Agent 运行、临时规则），同一剧本同一种子两次一致。"""
        self._ids[prefix] = self._ids.get(prefix, 0) + 1
        return f"{prefix}-{self._ids[prefix]:04d}"

    def brief(self) -> dict:
        sp = self.world.storage
        if self.state.inputs is None:  # 首分钟的事件在 advance 之前派发：先备好当分钟输入，不推进仿真时钟
            self.state.inputs = self.world.inputs.minute(self.now)
        brief = {"soc": self.energy / sp.capacity_kwh, "soc_min": sp.soc_min, "at": self.now.isoformat(),
                 "admin_temp": self.state.devices[self.world.index.zone_eid["admin"]]["temperature"],
                 "battery_nodes": list(self.world.grid.source_path)}
        for sim in self.world.simulators:  # 区域温度仿真器给暖通专家的当分钟视图
            if hasattr(sim, "situation"):
                brief["situation"] = sim.situation(self.state) | {"at": self.now.isoformat()}
        return brief

    def note(self, kind: str, at: datetime, record: dict) -> None:
        rec = {"kind": kind, "at": at.isoformat(), **record}
        self.trace.notes.append(rec)
        self.db.store.write_records(self.run_info.run_id, [("history" if kind == "history" else kind, f"{kind}-{len(self.trace.notes):05d}", at, rec)])

    def save_ticket(self, st: TicketState, at: datetime) -> None:
        # #217-3：状态迁移只追加 status_history（初始 open 一条），同一状态重复保存不追加。
        tid = st.ticket.ticket_id
        history = list((self.trace.tickets.get(tid) or {}).get("status_history") or ())
        if not history:
            history = [{"status": "open", "at": st.ticket.opened_at.isoformat()}]
        if history[-1].get("status") != st.ticket.status:
            history.append({"status": st.ticket.status, "at": at.isoformat()})
        rec = st.ticket.model_dump(mode="json") | {"origin_event_at": st.origin_event_at.isoformat(), "trigger_at": st.trigger_at.isoformat(),
                                                    "pending": list(st.pending), "status_history": history}
        self.trace.tickets[tid] = rec
        self.db.store.write_records(self.run_info.run_id, [("ticket", tid, st.ticket.opened_at, rec)])

    def save_work_order(self, wo: WorkOrder) -> None:
        # #217-3：proposed（创建）/confirmed/expired/done 等迁移只在状态变化时追加，不按分钟写。
        previous = self.trace.work_orders.get(wo.work_order_id)
        history = list(wo.status_history) or (list(previous.status_history) if previous is not None else [])
        if not history:
            history = [StatusChange(status="proposed", at=wo.created_at)]
        if history[-1].status != wo.status:
            history.append(StatusChange(status=wo.status, at=self.now))
        wo = wo.model_copy(update={"status_history": tuple(history)})
        self.trace.work_orders[wo.work_order_id] = wo
        self.db.store.write_records(self.run_info.run_id, [("work_order", wo.work_order_id, wo.created_at, wo.model_dump(mode="json"))])

    def record_agent_run(self, record: AgentRunRecord) -> None:
        self.trace.agent_runs.append(record)
        self.db.store.write_records(self.run_info.run_id, [("agent_run", record.agent_run_id, record.binding.trigger_at, record.model_dump(mode="json"))])

    def record_qa(self, entry: QaEntry) -> None:
        """工作台预设问答落库（B23 目前没有生产端时的最简写入路径）：day.json 的 qa 小节从这里导出。"""
        self.db.store.write_records(self.run_info.run_id, [("qa", entry.qa_id, entry.at, entry.model_dump(mode="json"))])

    def record_proposal(self, proposal: ExpertProposal) -> None:
        if any(existing.proposal_id == proposal.proposal_id for existing in self.trace.proposals):
            return
        self.trace.proposals.append(proposal)
        self.db.store.write_records(self.run_info.run_id, [("proposal", proposal.proposal_id, proposal.binding.trigger_at, proposal.model_dump(mode="json"))])

    def record_decision(self, decision: DayDecision) -> None:
        self.trace.decisions.append(decision)
        self.db.store.write_records(self.run_info.run_id, [("decision", decision.decision_id, decision.at, decision.model_dump(mode="json"))])
        for hook in self.registry.hooks_for("scenario.decision_recorded"):
            hook(self, decision)

    # ---- 内部
    def _context(self, manifest_id: str) -> Context:
        s = self.world.scenario
        return Context(plant_id=self.db.plant_id, industry_pack="manufacturing", actor=ACTOR, channel="offline", trace_id=f"scenario-{s.scenario_id}",
                       as_of=s.start, version_manifest_id=manifest_id, run_mode=self.mode)

    def _inputs(self) -> dict:
        s = self.world.scenario
        return {"scenario_id": s.scenario_id, "scenario_sha256": s.sha256, "inputs": self.world.inputs.provider_id, "mode": self.mode,
                "knowledge": sorted(self.knowledge)}

    def _kv(self, at: datetime, manifest_id: str, summary: str) -> None:
        entry = KnowledgeVersionEntry(version=self.knowledge_version, version_manifest_id=manifest_id, activated_at=at, summary=summary)
        self.trace.knowledge_versions.append(entry)
        self.db.store.write_records(self.run_info.run_id, [("knowledge_version", f"v{entry.version}", at, entry.model_dump(mode="json"))])


DISPATCHER_HOOK = "scenario_dispatcher"  # 协同组派发器的工厂 fn(runner, routes)；值守层经 SCENARIO_HOOK 登记，本包不 import 值守层
SCENARIO_HOOK = "scenario_plugin"  # 领域包在 install 里登记 fn(registry)：往场景注册表登记真实专家（deterministic，键=专家名）与 SIM_HOOK 仿真器


def default_registry(plugins: Registry | None = None, packs: Sequence[str] | None = None) -> Registry:
    """开发与评测装配：先让领域包的 SCENARIO_HOOK 登记真实专家与仿真器，其余专家用确定性替身；live 缺实现时回退。

    plugins 是已加载领域包的注册表（装配层有就传进来）；缺省按 packs（再缺省取当前配置）加载一份，只为取钩子。
    trial/evaluation 结果源随装配注册表带过来（B33 缓存等），live 源不复制：隔离回测不调模型。
    """
    source = plugins or pack_registry(packs)
    registry = Registry()
    registry.live_fallback = True
    for (port, mode, key), impl in source.mode_ports.items():
        if port is AgentResultSource and mode in ("trial", "evaluation"):
            registry.register_mode(port, mode, impl, key=key)
    for plugin in source.hooks_for(SCENARIO_HOOK):
        plugin(registry)
    experts = DeterministicExperts()
    for name in EXPERTS:
        if (AgentResultSource, "deterministic", name) not in registry.mode_ports:
            registry.register_mode(AgentResultSource, "deterministic", experts, key=name)
    return registry


def pack_registry(packs: Sequence[str] | None = None) -> Registry:
    """加载启用的领域包，供场景装配取 SCENARIO_HOOK。packs 缺省时每次按当前配置取，同一组领域包只加载一次。"""
    return _load_packs(tuple(packs) if packs is not None else tuple(load_settings().packs))


@functools.lru_cache(maxsize=8)
def _load_packs(packs: tuple[str, ...]) -> Registry:
    settings = load_settings(overrides={"packs": list(packs)})
    packs_dir = DEFAULT_CONFIG_DIR.parent / "plantkg" / "packs"
    registry = Registry()
    registry.load(sorted(p for p in packs_dir.iterdir() if p.is_dir()), packs, settings)
    return registry


# ---------------------------------------------------------------- 隔离试算


class TrialRunner:
    """在固定快照上隔离试算候选动作：深拷贝状态与随机数，输入取同一份，推进到 horizon 结束（截到剧本结束）。
    先在同一快照上跑空候选基线，再跑候选；只有相对基线新增或加重的硬约束违规（容量逐分钟、洁净车间与数据机房
    等硬性区域温度、安全标记）才判不可行，基线本来就有的违规记入 `baseline_violations`（#173 相对语义）。
    不推进正式时钟、不写正式账本、不调 ForkHook、不触发发布或人工确认回调。"""

    def __init__(self, world: ScenarioWorld) -> None:
        self.world = world

    def trial(self, snapshot: StateSnapshot, candidate: Sequence[SimAction], horizon: Interval) -> TrialResult:
        """候选相对空候选基线：只把新增或加重的违规判为不可行。horizon 超过剧本结束时只跑到剧本结束。

        观测仿真器不进试算：试算若写 ObservationBus，未来读数会冒充当前观测（#186）。"""
        end = min(horizon.end, self.world.scenario.end)
        effective = Interval(start=horizon.start, end=end)
        simulators = self.world.simulators
        self.world.simulators = [s for s in simulators if getattr(s, "stage", "physics") != "observe"]
        try:
            baseline_violations, baseline_cost, _ = self._run(snapshot, (), effective)
            violations, soft_cost, entries = self._run(snapshot, candidate, effective)
        finally:
            self.world.simulators = simulators
        baseline_index: dict[tuple, tuple[float | None, str]] = {}
        for v in baseline_violations:
            key = (v.kind, v.entity_id, v.at)
            severity = _violation_severity(v)
            previous = baseline_index.get(key)
            if previous is None or (severity is not None and (previous[0] is None or severity > previous[0])):
                baseline_index[key] = (severity, v.detail)
        added: list[TrialViolation] = []
        for v in violations:
            key = (v.kind, v.entity_id, v.at)
            if key not in baseline_index:
                added.append(v)
                continue
            baseline_severity, baseline_detail = baseline_index[key]
            severity = _violation_severity(v)
            if baseline_severity is not None and severity is not None:
                if severity > baseline_severity + 1e-6:  # 同一节点同一分钟恶化（如容量流量变大、温度更偏离带）
                    added.append(v)
            elif v.detail != baseline_detail:  # 量值解析不了时按 detail 比较，宁可保守
                added.append(v)
        existing_entities = {(v.kind, v.entity_id) for v in baseline_violations}
        remaining_minutes = len({v.at for v in violations if (v.kind, v.entity_id) in existing_entities})
        return TrialResult(feasible=not added, violations=tuple(added), soft_cost=soft_cost, minute_ledger=tuple(entries),
                           baseline_violations=tuple(baseline_violations), baseline_soft_cost=baseline_cost,
                           remaining_violation_minutes=remaining_minutes)

    def _run(self, snapshot: StateSnapshot, candidate: Sequence[SimAction],
             horizon: Interval) -> tuple[list[TrialViolation], ProposalCost, list]:
        w = self.world
        state = WorldState.from_json(copy.deepcopy(snapshot.pack_states["world"]))
        energy = float(snapshot.pack_states["storage"]["energy_kwh"])
        if horizon.start < state.at:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "试算区间不能早于快照时刻")
        violations: list[TrialViolation] = []
        for a in candidate:
            flags = set(w.zone_flags(a.entity_id))
            if "safety" in flags or state.devices.get(a.entity_id, {}).get("trapped"):
                violations.append(TrialViolation(kind="safety", entity_id=a.entity_id, at=a.interval.start,
                                                 detail=f"{a.command} 作用于带安全标记的对象，只能走操作工单"))
            if (a.command in ("stop", "shed", "set_setpoint") or a.params.get("mode") in ("off", "precool")) and flags & {"no_shed", "hard_climate", "no_power_cut"}:
                violations.append(TrialViolation(kind="hard_zone", entity_id=a.entity_id, at=a.interval.start,
                                                 detail=f"{a.command} 违反区域硬要求 {sorted(flags)}"))
            state.sims.setdefault("actions", []).append({"action": a.model_dump(mode="json"), "source": {"trial": snapshot.snapshot_id}})
        meta = LedgerMeta(ledger_id=f"trial:{snapshot.snapshot_id}", kind="execution", run_id=snapshot.run_id, plan_id=f"trial:{snapshot.snapshot_id}",
                          plan_revision=0, version_manifest_id=snapshot.version_manifest_id, source_refs=(w.inputs.provider_id,))
        entries, cny, comfort, hard_seen = [], 0.0, 0.0, set()
        wait0 = state.sims.get("elevator", {}).get("wait_person_min", 0.0)
        view = w.freeze_inputs(snapshot.snapshot_id, snapshot.as_of)  # 快照绑定的输入视图：跨试算实例、不同 horizon 都是同一份
        def inputs(t: datetime) -> MinuteInputs:
            if t not in view:  # 冻结输入只到剧本结束：越界给可读错误，而不是 KeyError 中止运行
                raise PlantkgError(ErrorCode.INVALID_REQUEST,
                                   f"试算输入缺少 {t.isoformat()} 分钟（冻结区间到 {w.scenario.end.isoformat()}）")
            return copy.deepcopy(view[t])
        while state.at < horizon.end:
            t = state.at
            record, energy = w.advance(state, energy, meta, inputs)
            if t < horizon.start:
                continue
            entries.append(record.entry)
            cny += record.entry.cost_cny or 0.0
            for v in record.capacity_violations:
                violations.append(TrialViolation(kind="capacity", entity_id=_capacity_node(v), at=t, detail=v))
            for z, cfg in ZONES.items():
                zid = w.index.zone_eid.get(z)
                if zid is None or cfg["band"] is None:
                    continue
                temp = state.devices[zid]["temperature"]
                lo, hi = cfg["band"]
                if not lo <= temp <= hi:
                    if cfg["hard"] and z not in hard_seen:
                        hard_seen.add(z)
                        violations.append(TrialViolation(kind="hard_zone", entity_id=zid, at=t, detail=f"{z} 温度 {temp:.1f} ℃ 超出 [{lo}, {hi}]"))
                    elif not cfg["hard"]:
                        comfort += 1
            for sim in w.simulators:  # 仿真器可自带硬约束（如硬区湿度），逐分钟查，只看本次 horizon 内
                for entity_id, detail in getattr(sim, "hard_violations", lambda *_: ())(state, t):
                    violations.append(TrialViolation(kind="hard_zone", entity_id=entity_id, at=t, detail=detail))
        wait = state.sims.get("elevator", {}).get("wait_person_min", 0.0) - wait0
        return violations, ProposalCost(cny=round(cny, 4), wait_min=round(max(wait, 0.0), 2), comfort_min=comfort), entries
