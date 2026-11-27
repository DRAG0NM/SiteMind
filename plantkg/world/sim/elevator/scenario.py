"""B17 场景世界的电梯仿真器（B20）：分钟步进版，接账本负荷分项。

SimPy 版（sim.py）用于演示与策略对照；这里把同样的策略落到 B17 的 `Simulator.step` 上：每个 1 分钟子步
读人流/物料任务曲线与已生效动作，更新排队、候梯人·分钟、拥堵时长与电梯功率。状态全在 WorldState 里，
随快照深拷贝，分叉与试算不会带出隐藏的进程状态。结果是 simulated，参数 configured。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.core.contracts import SimAction
from plantkg.world.site.model import SiteModel

from .config import ElevatorConfig

CAR_PERSONS_PER_MIN = 3.2  # 每台人梯每分钟运送人数（8 人/趟、2.5 分钟往返）
PERSONS_PER_TRIP = 8
FREIGHT_TASKS_PER_MIN = 0.25
ZONE_BOOST = 1.3
BATCH_BOOST = 1.5  # 合批：一趟平均带 1.5 件，运力乘 1.5，同样的任务量往返趟数除以 1.5
QUEUE_CONGESTION = 20
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _clear_effects(state: Any, entity_id: str, kinds: set[str]) -> None:
    state.effects = [e for e in state.effects if not (e.get("entity_id") == entity_id and e["kind"] in kinds)]


def _active(actions: Sequence[SimAction], entity_id: str) -> list[SimAction]:
    return [a for a in actions if a.entity_id == entity_id]


def _starting(actions: Sequence[SimAction], at: datetime) -> list[SimAction]:
    return [a for a in actions if a.interval.start == at]


class ScenarioElevatorSim:
    """电梯群控的分钟步进实现。人梯与货梯各用一套策略：人梯按高峰模式与分区，货梯按优先级与批次。"""

    name = "elevator"

    def __init__(self, site: SiteModel) -> None:
        self.config = ElevatorConfig.from_site(site)
        self.groups = {
            self.config.group_id("admin_low"): [c.code for c in self.config.cars("low")],
            self.config.group_id("admin_high"): [c.code for c in self.config.cars("high")],
            self.config.group_id("freight"): [c.code for c in self.config.cars("freight")],
        }
        self.car_entities = {c.code: c.entity_id for c in self.config.cars()}

    def init(self, state: Any) -> None:
        for group_id, cars in self.groups.items():
            state.devices[group_id] = {"queue": 0.0, "active_cars": len(cars), "mode": "normal"}
        for code, entity_id in self.car_entities.items():
            state.devices[entity_id] = {"trapped": False, "running": True, "fault": False}
        state.sims["elevator"] = {"wait_person_min": 0.0, "wait_task_min": 0.0, "congestion_min": 0.0,
                                  "energy_kwh": 0.0, "safety_events": []}

    def step(self, state: Any, sim_actions: Sequence[SimAction], dt: Any) -> Any:
        minutes = dt.total_seconds() / 60
        local = state.at.astimezone(LOCAL_TZ)
        minute_of_day = local.hour * 60 + local.minute
        for action in _starting(sim_actions, state.at):
            self._apply_start(state, action)
        for code, entity_id in self.car_entities.items():
            trapped = bool(state.effect("trapped", entity_id))
            state.devices[entity_id]["trapped"] = trapped
            if trapped and not any(e["entity_id"] == entity_id for e in state.sims["elevator"]["safety_events"]):
                state.sims["elevator"]["safety_events"].append(
                    {"kind": "trapped", "entity_id": entity_id, "at": state.at.isoformat(), "severity": 5,
                     "nature": "simulated", "summary": f"{code} 困人"})
        groups = ((self.config.group_id("admin_low"), True), (self.config.group_id("admin_high"), True),
                  (self.config.group_id("freight"), False))
        congested = False
        for group_id, passenger in groups:
            self._step_group(state, sim_actions, minutes, minute_of_day, group_id, passenger=passenger)
            congested = congested or state.devices[group_id]["queue"] >= QUEUE_CONGESTION
        if congested:
            state.sims["elevator"]["congestion_min"] += minutes
        return state

    def _apply_start(self, state: Any, action: SimAction) -> None:
        if action.command == "rescue" and state.effect("trapped", action.entity_id):
            _clear_effects(state, action.entity_id, {"trapped"})
            state.sims["elevator"]["safety_events"].append(
                {"kind": "rescued", "entity_id": action.entity_id, "at": state.at.isoformat(), "severity": 5,
                 "nature": "simulated", "summary": "困人救援完成"})

    def _step_group(self, state: Any, sim_actions: Sequence[SimAction], minutes: float, minute_of_day: int,
                    group_id: str, *, passenger: bool) -> None:
        device = state.devices[group_id]
        cars = self.groups[group_id]
        mode = self._peak_mode(minute_of_day) if passenger else "normal"
        active = len(cars) if not passenger or mode != "off_peak" else max(1, len(cars) // 2)
        batch = batching = False
        for action in _active(sim_actions, group_id):
            if action.command == "set_mode":
                mode = action.params.get("mode", mode)
            elif action.command == "set_active":
                active = int(action.params.get("cars", active))
            elif action.command == "set_batch":
                batch = bool(action.params.get("enabled", True))
        usable = [code for code in cars if not state.devices[self.car_entities[code]]["trapped"]
                  and state.devices[self.car_entities[code]].get("running", True)
                  and not state.devices[self.car_entities[code]].get("fault", False)]
        active = max(0, min(active, len(usable)))
        usable = usable[:active]
        surge = 1.0
        for effect in state.effect("surge", group_id):
            surge *= float(effect.get("factor", 1.0))
        occupancy = state.inputs.occupancy
        if passenger:
            arrivals = float(occupancy.get("trips:passenger_elevator", 0.0)) * PERSONS_PER_TRIP / 2 * surge * minutes
            capacity = active * CAR_PERSONS_PER_MIN * (ZONE_BOOST if mode == "zone" else 1.0) * minutes
        else:
            arrivals = float(occupancy.get("tasks:material_handling", 0.0)) * surge * minutes
            batching = batch or mode == "batch"
            boost = BATCH_BOOST if batching or mode == "priority" else 1.0
            if mode == "takeover":  # 其余货梯接管故障梯的任务：运力补回整组台数
                boost = len(cars) / active if active else 1.0
            capacity = active * FREIGHT_TASKS_PER_MIN * boost * minutes
        served = min(device["queue"] + arrivals, capacity)
        device["queue"] = round(max(0.0, device["queue"] + arrivals - capacity), 3)
        device["active_cars"], device["mode"] = active, mode
        if passenger:
            state.sims["elevator"]["wait_person_min"] += device["queue"] * minutes
        else:
            state.sims["elevator"]["wait_task_min"] += device["queue"] * minutes
        self._write_power(state, group_id, usable, served, minutes, passenger, batching)

    def _write_power(self, state: Any, group_id: str, usable: list[str], served: float, minutes: float, passenger: bool,
                     batching: bool = False) -> None:
        if passenger:
            energy = served / PERSONS_PER_TRIP * self.config.trip_kwh
        else:
            energy = served * self.config.freight_trip_kwh / (BATCH_BOOST if batching else 1.0)
        state.sims["elevator"]["energy_kwh"] += energy
        # 直接覆盖 ProfileSim 按 B18 曲线写入的电梯功率：电梯负荷由本仿真按人流与任务算出，叠加会重复计入
        running_kw = energy * 60 / minutes / len(usable) if usable else 0.0  # 运行能耗平摊到每台启用梯，功率积分才等于账本能耗
        for code in usable:
            state.power_kw[code] = self.config.standby_kw + running_kw
        for code in self.groups[group_id]:
            if code not in usable:
                state.power_kw[code] = round(self.config.standby_kw * 0.3, 3)  # 停梯只剩待机

    @staticmethod
    def _peak_mode(minute_of_day: int) -> str:
        if 7 * 60 <= minute_of_day < 9 * 60:
            return "peak_up"
        if 11 * 60 + 45 <= minute_of_day < 13 * 60 + 15:
            return "lunch"
        if 17 * 60 + 30 <= minute_of_day < 18 * 60 + 30:
            return "peak_down"
        if minute_of_day < 6 * 60:
            return "off_peak"
        return "normal"
