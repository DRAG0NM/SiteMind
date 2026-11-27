"""值守层的试算台（B17 → B27）：登记 router.trial、router.snapshot、router.capacity_trip。

严重程度入口要在“当前快照”上跑未保护试算。场景运行驱动值守层的装配登记自己的钩子即可覆盖（钩子取最后登记的）；
没有运行中的场景时，这里按默认剧本快进到事件当天同一分钟（日内时刻对齐，数据 simulated），叠上事件的物理影响后取快照。
观测仿真器不进试算台：试算不写 ObservationBus，否则未来读数会冒充当前观测。
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import Interval, SimAction, StateSnapshot, TrialResult
from plantkg.core.ledger import LedgerMeta
from plantkg.core.registry import Registry
from plantkg.world.sensors import ObservationBus

from .runner import ScenarioWorld, TrialRunner
from .sims import event_effects

DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)


class TrialBench:
    def __init__(self, build: Callable[[], ScenarioWorld]) -> None:
        self._build = build
        self._world: ScenarioWorld | None = None
        self._cursor: tuple[Any, float] | None = None  # 快进到的位置；事件时间多半递增，不用每次从零点推

    @property
    def world(self) -> ScenarioWorld:
        if self._world is None:
            world = self._build()
            world.simulators = [s for s in world.simulators if getattr(s, "stage", "physics") != "observe"]
            self._world = world
        return self._world

    def _advance_to(self, at: datetime) -> tuple[Any, float]:
        world = self.world
        if self._cursor is None or self._cursor[0].at > at:
            self._cursor = world.initial_state()
        state, energy = self._cursor
        meta = LedgerMeta(ledger_id="trial-bench", kind="execution", run_id="trial-bench", plan_id="trial-bench", plan_revision=0,
                          version_manifest_id="trial-bench", source_refs=(world.inputs.provider_id,))
        while state.at < at:
            _, energy = world.advance(state, energy, meta)
        self._cursor = (state, energy)
        return state, energy

    def snapshot(self, event: Any = None) -> StateSnapshot:
        """事件当天同一分钟的快照；事件的物理影响（payload.effects 或类型默认影响）叠在快照上，不进入快进位置。"""
        world = self.world
        start = world.scenario.start
        when = event.at if event is not None else start
        at = start + timedelta(minutes=int(((when - start) % DAY) / MINUTE))
        state, energy = self._advance_to(at)
        data = copy.deepcopy(state.to_json())
        snapshot_id = f"trial-bench@{at.isoformat()}"
        if event is not None and event.severity > 0:
            effects = event_effects(event.event_type, event.entity_id, event.payload, at, world.index)
            data["effects"] = [*data.get("effects", []), *effects]
            snapshot_id += f":{event.event_id}"
        return StateSnapshot(run_id="trial-bench", snapshot_id=snapshot_id, event_seq=int((at - start) / MINUTE), as_of=at,
                             version_manifest_id="trial-bench", pack_states={"world": data, "storage": {"energy_kwh": energy}})

    def trial(self, snapshot: StateSnapshot, candidate: Sequence[SimAction], horizon: Interval) -> TrialResult:
        """时域按时长算，从快照时刻起：快照已对齐到剧本日内，事件的真实日期不进试算。"""
        world = self.world
        rebased = Interval(start=snapshot.as_of, end=min(snapshot.as_of + (horizon.end - horizon.start), world.scenario.end))
        minutes = int((rebased.end - rebased.start) / MINUTE)
        world.frozen_inputs[snapshot.snapshot_id] = {t: world.inputs.minute(t) for t in (rebased.start + m * MINUTE for m in range(minutes))}
        try:
            return TrialRunner(world).trial(snapshot, candidate, rebased)
        finally:
            world.frozen_inputs.pop(snapshot.snapshot_id, None)

    def capacity_trip(self, entity: str, ratio: float, minutes: int) -> bool:
        """观测层：变压器下各路配电柜功率读数之和，连续 minutes 分钟达到限额的 ratio。没有读数按未跳闸（不扣光伏，偏保守）。"""
        world = self.world
        site = world.site
        code = entity.rsplit("/", 1)[-1]
        limit = world.grid.limits.get(code)
        box = ObservationBus.latest(site.plant_id)
        if limit is None or not box:
            return False
        readings = box["snapshot"].get("readings", {})
        under = [sid for sid, r in readings.items() if r.get("metric") == "power_kw" and r.get("entity") in site.nodes
                 and code in site.path(r["entity"])[1:]]
        feeders = [sid for sid in under
                   if not any(o != sid and readings[o]["entity"] in site.path(readings[sid]["entity"])[1:] for o in under)]
        series = [box["history"].get(sid, [])[-minutes:] for sid in feeders]
        if not series or any(len(s) < minutes for s in series):
            return False
        return all(sum(float(s[i]["value"] or 0.0) for s in series) >= ratio * limit for i in range(minutes))


def install(registry: Registry, build: Callable[[], ScenarioWorld]) -> TrialBench:
    bench = TrialBench(build)
    registry.register_hook("router.snapshot", bench.snapshot)
    registry.register_hook("router.trial", bench.trial)
    registry.register_hook("router.capacity_trip", bench.capacity_trip)
    return bench
