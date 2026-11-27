"""区域温度仿真器（B21）：替换 B17 sims.HvacSim 的同名实现（name = "hvac"）。

每区一阶 RC、比例控制供冷；供冷电功率写回 state.power_kw 里同区的供冷设备，B17 的分钟账本据此汇总暖通电耗。
读本分钟已生效的动作：set_setpoint（含 B47 临时规则，到期由动作区间自然失效）、set_mode（normal/precool/off）、
ventilate（井道或区域通风，记一笔风机负荷）。温度越界分钟数与暖通电耗累计进 state.sims["thermal"]。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from plantkg.core.contracts import SimAction
from plantkg.world.sim.base import WorldState

from .model import COOLING_TYPES, FEEDFORWARD, HVAC_TYPES, RH_TAU_MIN, RH_WHEN_OFF, ZoneSpec, control, load_zones

OCCUPANCY_KEYS = {"admin": "occupancy:admin", "production": "occupancy:production"}


def _active(actions: Sequence[SimAction], entity_id: str) -> list[SimAction]:
    return [a for a in actions if a.entity_id == entity_id]


def _starting(actions: Sequence[SimAction], at: datetime) -> list[SimAction]:
    return [a for a in actions if a.interval.start == at]


class ThermalSim:
    """index 为 B17 的 SiteIndex（要 site、zone_eid）；zones 缺省读 configs/hvac/thermal.yaml。"""

    name = "hvac"

    def __init__(self, index: Any, zones: Mapping[str, ZoneSpec] | None = None) -> None:
        self.index = index
        specs = dict(zones if zones is not None else load_zones())
        self.specs: dict[str, ZoneSpec] = {z: s for z, s in specs.items() if z in index.zone_eid}
        self.zones = list(self.specs)
        self.zids = [index.zone_eid[z] for z in self.zones]
        self.cooling = [[e for e in index.site.equipment if e.zone == z and e.type in COOLING_TYPES] for z in self.zones]
        self.c = np.array([self.specs[z].c_kj_per_k for z in self.zones], dtype=float)
        self.ua = np.array([self.specs[z].ua_kw_per_k for z in self.zones], dtype=float)
        fan = next((e for e in index.site.equipment if e.type == "exhaust_fan" and e.zone != "hazmat"), None)
        self.fan_eid = fan.entity_id if fan else None
        self.feedforward = np.array([1.0 if self.specs[z].hard else FEEDFORWARD for z in self.zones], dtype=float)
        self.cop = np.array([self.specs[z].cop for z in self.zones], dtype=float)
        self.base_ratio = np.array([self.specs[z].base_ratio for z in self.zones], dtype=float)
        self.setpoint = np.array([self.specs[z].setpoint_c for z in self.zones], dtype=float)

    def init(self, state: WorldState) -> None:
        for z in self.zones:
            spec = self.specs[z]
            state.devices[self.index.zone_eid[z]] = {"temperature": round(spec.setpoint_c, 3), "humidity": spec.rh_c,
                                                     "setpoint": spec.setpoint_c, "mode": "normal", "temp_rule": None}
        state.sims["thermal"] = {"comfort_minutes": {z: 0 for z in self.zones}, "humidity_minutes": {z: 0 for z in self.zones},
                                 "hvac_kwh": {z: 0.0 for z in self.zones}, "outdoor_c": None, "ignored": [], "hard_zones": sorted(
                                     z for z in self.zones if self.specs[z].hard)}

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        secs, minutes = dt.total_seconds(), dt.total_seconds() / 60.0
        outdoor = self.outdoor_c(state)
        local = state.at.astimezone(ZoneInfo(self.index.site.plant_tz))
        temperature = np.array([state.devices[zid]["temperature"] for zid in self.zids], dtype=float)
        setpoint, mode, rules = self.setpoint.copy(), ["normal"] * len(self.zones), [None] * len(self.zones)
        for i, zid in enumerate(self.zids):
            hard = self.specs[self.zones[i]].hard
            for a in _active(sim_actions, zid):
                if hard and (a.command == "set_setpoint" or (a.command == "set_mode" and a.params.get("mode", "normal") in ("off", "precool"))):
                    if a.interval.start == state.at:  # 硬约束区域不接受停机、预冷与设定温度改写
                        state.sims["thermal"]["ignored"].append({"at": state.at.isoformat(), "entity_id": zid, "command": a.command,
                                                                 "params": a.params, "reason": "硬约束区域不接受设定温度改写与停机、预冷"})
                elif a.command == "set_setpoint":
                    setpoint[i] = float(a.params["setpoint_c"])
                    rules[i] = a.params.get("temp_rule")
                elif a.command == "set_mode":
                    mode[i] = str(a.params.get("mode", "normal"))
        precool = np.array([m == "precool" for m in mode])
        setpoint = np.where(precool, setpoint - 1.0, setpoint)  # 预冷：设定温度低 1 ℃，把冷量提前买进去
        allowed = np.array([m != "off" and self.specs[z].in_cooling_hours(local) for m, z in zip(mode, self.zones)])
        runnable = self.runnable(state)  # 停机或故障的设备不提供容量
        rated = np.array([sum(e.rated_kw for e, on in zip(items, ok) if on) for items, ok in zip(self.cooling, runnable)], dtype=float)
        serving = allowed & (rated > 0)  # 冷源全停时除湿也停
        gains = self.zone_gains(state)
        gain = np.array([gains[z] for z in self.zones], dtype=float)
        temperature, power, _ = control(temperature, setpoint, outdoor, gain, rated, self.ua, self.cop, self.c,
                                        self.base_ratio, allowed=allowed, secs=secs, feedforward=self.feedforward)

        thermal = state.sims["thermal"]
        thermal["outdoor_c"] = round(outdoor, 3)
        for i, z in enumerate(self.zones):
            spec, dev = self.specs[z], state.devices[self.zids[i]]
            dev["temperature"] = round(float(temperature[i]), 3)
            dev["setpoint"], dev["mode"], dev["temp_rule"] = round(float(setpoint[i]), 3), mode[i], rules[i]
            target = spec.rh_c if serving[i] else RH_WHEN_OFF
            dev["humidity"] = round(float(dev["humidity"]) + (target - float(dev["humidity"])) * min(1.0, minutes / RH_TAU_MIN), 2)
            if spec.in_cooling_hours(local) and not spec.in_comfort(dev["temperature"]):  # 只在供冷时段内计越界
                thermal["comfort_minutes"][z] += 1
            if spec.in_cooling_hours(local) and not spec.in_humidity_band(dev["humidity"]):
                thermal["humidity_minutes"][z] += 1
            thermal["hvac_kwh"][z] = round(thermal["hvac_kwh"][z] + float(power[i]) * minutes / 60.0, 6)
            if rated[i] > 0:  # 供冷设备功率按可用额定的份额分摊，账本按设备编号汇总；停机的设备保留 ProfileSim 的 0
                for e, on in zip(self.cooling[i], runnable[i]):
                    if on:
                        state.power_kw[e.code] = round(float(power[i]) * e.rated_kw / rated[i], 3)
        for a in _starting(sim_actions, state.at):
            if a.command == "ventilate":  # 井道或区域通风：3 kW 级风机，负荷挂在排风机上记入暖通类别（站内没有排风机才挂原对象）
                state.effects.append({"kind": "extra_load", "entity_id": self.fan_eid or a.entity_id, "kw": float(a.params.get("kw", 3.0)),
                                      "since": state.at.isoformat(), "until": a.interval.end.isoformat()})
        return state

    def hard_violations(self, state: WorldState, at: datetime) -> list[tuple[str, str]]:
        """at 这一分钟刚算完的硬约束区域湿度越界（供冷时段内）：[(区域实体, 说明)]，TrialRunner 逐分钟取用。"""
        local = at.astimezone(ZoneInfo(self.index.site.plant_tz))
        out = []
        for z in self.zones:
            spec = self.specs[z]
            rh = float(state.devices[self.index.zone_eid[z]]["humidity"])
            if spec.hard and spec.in_cooling_hours(local) and not spec.in_humidity_band(rh):
                out.append((self.index.zone_eid[z], f"{z} 湿度 {rh:.1f} %RH 超出 [{spec.rh_band[0]}, {spec.rh_band[1]}]"))
        return out

    def zone_gains(self, state: WorldState) -> dict[str, float]:
        """本分钟各区得热（kW）：在岗人员显热 + 区域内非暖通设备功率进入室内热量的份额。"""
        out = {}
        for z in self.zones:
            spec = self.specs[z]
            occupancy = state.inputs.occupancy.get(OCCUPANCY_KEYS.get(z, ""), 0.0) if state.inputs is not None else 0.0
            equipment = sum(state.power_kw.get(e.code, 0.0) for e in self.index.site.equipment
                            if e.zone == z and e.type not in HVAC_TYPES)
            out[z] = round(occupancy * spec.people_kw + spec.equipment_heat_ratio * equipment, 3)
        return out

    def runnable(self, state: WorldState) -> list[list[bool]]:
        """本分钟哪些供冷设备可用：停机或带故障的设备不提供制冷容量（ProfileSim 已把它们的基线负荷记 0）。"""
        out = []
        for items in self.cooling:
            out.append([bool((state.devices.get(e.entity_id) or {}).get("running", True))
                        and not bool((state.devices.get(e.entity_id) or {}).get("fault", False)) for e in items])
        return out

    def zone_power(self, state: WorldState) -> dict[str, float]:
        """本分钟各区供冷电功率（kW）。"""
        return {z: round(sum(state.power_kw.get(e.code, 0.0) for e in items), 3) for z, items in zip(self.zones, self.cooling)}

    def outdoor_c(self, state: WorldState) -> float:
        """本分钟的室外温度（含热浪等事件对气温的叠加），温度模型与专家视图都用这个值。"""
        return float(state.inputs.outdoor_c) + sum(float(e.get("delta_c", 0.0)) for e in state.effect("heat"))

    def situation(self, state: WorldState) -> dict[str, Any]:
        """给暖通专家提案用的当分钟视图：区域温度、设定、模式、供冷功率与得热，带区域实体 ID。
        首分钟派发事件时 inputs 还没备好（brief 会在推进前补上），这里按缺省值给出，不抛异常。"""
        gains, power = self.zone_gains(state), self.zone_power(state)
        return {"at": state.at, "outdoor_c": self.outdoor_c(state) if state.inputs is not None else None,
                "zones": {z: {"entity_id": self.index.zone_eid[z], **state.devices[self.index.zone_eid[z]],
                              "power_kw": power[z], "gain_kw": gains[z]} for z in self.zones}}


__all__ = ["ThermalSim"]
