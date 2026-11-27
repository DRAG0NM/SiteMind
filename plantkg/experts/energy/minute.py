"""B19 分钟层接进 B17 权威入口（#188 Plan A）。

`RealtimeController` 不变，包成场景注册表的 `scenario.minute_layer` 钩子：B17 CLI
（`python -m plantkg.world.scenario run`）与 live 脚本在事件注入之后、本分钟结算之前调用它，用储能充放
把购电压回目标线，经 `runner.apply` 下发当分钟动作。

这两个入口没有 B09 日前计划，目标线取“能源事件发生前的实际购电”（`configured`）：平时不对储能下指令，
事件过响应预算（`PolicyConfig.response_budget.layer12_min`）后把购电压回事件前水平，连续 5 分钟回到
目标线内即解除保持。B19 `run_day` 那条入口用 15 分钟计划目标线并在整格边界滚动重解，两条入口共用同一个
`RealtimeController`，差异只在目标线来源（见 `plantkg/world/scenario/README.md`）。

储能由分钟层独占：事件级能源提案里的电池充放由 `MinuteLayerSource` 过滤掉，照明削峰、回路切换照常。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from plantkg.core.ports import AgentResultSource
from plantkg.world.scenario.runner import SCENARIO_MINUTE_HOOK, ScenarioRunner
from plantkg.world.sim.base import SLOT

from .realtime import TOLERANCE_KW, RealtimeController, SlotTarget

ENERGY_EVENTS = frozenset({"load_spike", "sim_limit", "pv_drop", "heat_wave", "capacity_shed_request"})
MIN_POWER_KW = 0.05  # 低于这个功率不登记动作，与分钟层自己的一致
RELEASE_MINUTES = 5  # 连续这么多分钟回到目标线内就解除保持


def _ceil_minute(t: datetime) -> datetime:
    floor = t.replace(second=0, microsecond=0)
    return floor if floor == t else floor + timedelta(minutes=1)


class MinuteLayerSource:
    """场景装配里的能源专家来源：电池充放由分钟层统一控制，事件级提案里的电池动作过滤掉，其余照常执行。"""

    def __init__(self, inner, battery_id: str) -> None:
        self.inner, self.battery_id = inner, battery_id
        self.source_kind, self.name = inner.source_kind, getattr(inner, "name", "energy")

    def get(self, kind, binding, payload):
        out = dict(self.inner.get(kind, binding, payload))
        proposal = dict(out.get("proposal") or {})
        actions = proposal.get("actions") or []
        kept = [a for a in actions if not (a["entity_id"] == self.battery_id and a["command"] in ("charge", "discharge"))]
        if len(kept) != len(actions):
            proposal["actions"] = kept
            proposal["diagnosis"] = (proposal.get("diagnosis") or "") + "（储能充放由分钟层统一控制，事件级提案不重复下发）"
            out["proposal"] = proposal
        return out


@dataclass
class MinuteLayer:
    """B17 每分钟的能源分钟层：同一份 RealtimeController，按事件前购电目标线跟踪储能。"""

    runner: ScenarioRunner
    controller: RealtimeController
    run_id: str = ""
    seen_events: int = 0
    target_kw: float | None = None  # 事件保持期的目标购电；None 表示平时不干预
    in_target: int = 0

    def __call__(self, runner: ScenarioRunner, at: datetime) -> None:
        if runner.run_info.run_id != self.run_id:  # 分叉：控制器继续跟踪，事件计数按新运行重来
            self.run_id, self.seen_events = runner.run_info.run_id, len(runner.trace.events)
        minute_state = self._minute_state(runner, at)
        self._respond(runner, minute_state)
        target = self._target(at, minute_state)
        action = self.controller.step(minute_state, target)
        power = float(action.params.get("power_kw", 0.0))
        if power > MIN_POWER_KW:
            runner.apply(action, source={"expert": "energy", "layer": "minute", "entry": "b17-command"})
            runner.note("energy_minute", at, {"command": action.command, "power_kw": power, "line_kw": round(target.import_line_kw, 3)})
        self._release()

    # ---- 事件响应预算：事件后第 layer12_min 个整分钟起才做偏差修正
    def _respond(self, runner: ScenarioRunner, minute_state: dict) -> None:
        budget = runner.policy.response_budget.layer12_min
        for event in runner.trace.events[self.seen_events:]:
            if event.severity > 0 and event.event_type in ENERGY_EVENTS:
                new_from = _ceil_minute(event.at + timedelta(minutes=budget))
                if self.target_kw is None:  # 进入保持期：目标线 = 事件前的实际购电
                    prev = self.controller.history[-1]["import_without_storage_kw"] if self.controller.history else None
                    self.target_kw = float(prev) if prev is not None else float(minute_state["load_kw"] - minute_state["pv_kw"])
                    self.in_target, self.controller.respond_from = 0, new_from
                else:  # 已在保持期：新的同类告警不推迟已经排定的响应，否则持续过载会被一路延后
                    self.controller.respond_from = min(self.controller.respond_from or new_from, new_from)
                runner.note("energy_response", event.at, {"event_id": event.event_id, "event_type": event.event_type,
                                                          "target_kw": round(self.target_kw, 3),
                                                          "effective_at": self.controller.respond_from.isoformat()})
        self.seen_events = len(runner.trace.events)

    def _minute_state(self, runner: ScenarioRunner, at: datetime) -> dict:
        state = runner.state
        fresh = runner.world.inputs.minute(at)
        load = sum(state.power_kw.values()) if state.power_kw else sum(fresh.equipment_kw.values())
        last = runner.trace.ledger[-1].entry if runner.trace.ledger else None
        settled = last if last is not None and last.interval.end == at else None
        return {"at": at, "load_kw": load,
                "pv_kw": settled.pv_available_kwh * 60 if settled else fresh.pv_kw,
                "price_cny_per_kwh": settled.price_cny_per_kwh if settled else fresh.price_cny_per_kwh,
                "energy_kwh": runner.energy}

    def _target(self, at: datetime, minute_state: dict) -> SlotTarget:
        """平时目标线就是当前购电（不动储能）；事件保持期压回事件前购电（configured）。"""
        slot = at - timedelta(minutes=at.minute % 15, seconds=at.second, microseconds=at.microsecond)
        if self.target_kw is None:
            return SlotTarget(start=slot, end=slot + SLOT, load_kw=float(minute_state["load_kw"]),
                              pv_kw=float(minute_state["pv_kw"]), price_cny_per_kwh=minute_state.get("price_cny_per_kwh"))
        return SlotTarget(start=slot, end=slot + SLOT, load_kw=self.target_kw, pv_kw=0.0,
                          price_cny_per_kwh=minute_state.get("price_cny_per_kwh"))

    def _release(self) -> None:
        if self.target_kw is None or not self.controller.history:
            return
        if self.controller.history[-1]["import_without_storage_kw"] - self.target_kw <= TOLERANCE_KW:
            self.in_target += 1
            if self.in_target >= RELEASE_MINUTES:
                self.target_kw, self.in_target = None, 0
        else:
            self.in_target = 0


def install_minute_layer(runner: ScenarioRunner) -> MinuteLayer:
    """接到这个运行上：注册每步钩子，并让能源专家来源不再下发电池充放（储能由分钟层独占）。"""
    world = runner.world
    controller = RealtimeController(battery_id=world.index.battery_id, storage=world.storage,
                                    charge_kw=world.charge_kw, discharge_kw=world.discharge_kw,
                                    end_target_kwh=world.scenario.initial.energy_end_target_kwh,
                                    end_at=world.scenario.end)
    layer = MinuteLayer(runner, controller, run_id=runner.run_info.run_id, seen_events=len(runner.trace.events))
    runner.registry.register_hook(SCENARIO_MINUTE_HOOK, layer)
    slot = (AgentResultSource, "deterministic", "energy")
    inner = runner.registry.mode_ports.get(slot)
    if inner is not None and not isinstance(inner, MinuteLayerSource):
        runner.registry.mode_ports[slot] = MinuteLayerSource(inner, world.index.battery_id)
    return layer
