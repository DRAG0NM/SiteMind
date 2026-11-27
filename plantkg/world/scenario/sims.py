"""内置最简仿真器（B17 替身）：设备启停与故障、区域温度、电梯排队、空压管网。

只求“能演示、行为合理”，参数按常理设定（configured），结果标 simulated。B20（电梯群控）、B21（区域温度 RC 模型）、
B22（设备故障注入）实现同名 Simulator 后在 ScenarioWorld.simulators 里替换即可。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import SimAction
from plantkg.world.sim.base import MINUTE, WorldState
from plantkg.world.site.model import SiteModel

# 区域温控（configured）：默认设定、舒适带、是否硬约束
ZONES: dict[str, dict[str, Any]] = {
    "production": {"setpoint": 22.0, "band": (20.0, 24.0), "rh": 50.0, "hard": False},
    "admin": {"setpoint": 25.0, "band": (24.0, 26.0), "rh": 55.0, "hard": False},
    "cleanroom": {"setpoint": 22.0, "band": (21.0, 23.0), "rh": 45.0, "hard": True},
    "datacenter": {"setpoint": 24.0, "band": (18.0, 27.0), "rh": 45.0, "hard": True},
    "hazmat": {"setpoint": None, "band": None, "rh": 60.0, "hard": False},
}
COOLING_TYPES = {"ahu", "chiller", "crac"}
HVAC_TYPES = COOLING_TYPES | {"exhaust_fan"}
ELEVATOR_TYPES = {"passenger_elevator", "freight_elevator"}
CAR_PERSONS_PER_MIN, ZONE_BOOST = 3.2, 1.3  # 每台人梯每分钟运送人数（8 人/趟、2.5 分钟往返）；分区运行效率提升
FREIGHT_TASKS_PER_MIN = 0.25
TRIP_KWH, PERSONS_PER_TRIP = 0.09, 8


@dataclass
class SiteIndex:
    """实体 ID 与设备编号、区域、电梯群的对照。"""

    site: SiteModel
    battery_id: str

    def __post_init__(self) -> None:
        p = self.site.plant_id
        self.by_code = {e.code: e for e in self.site.equipment}
        self.by_eid = {e.entity_id: e for e in self.site.equipment}
        self.zone_eid = {z: f"entity://{p}/zone/{z}" for z in self.site.zones}
        self.air_eid = f"entity://{p}/air_system/AIR"
        lifts = [e for e in self.site.equipment if e.type == "passenger_elevator"]
        self.groups = {f"entity://{p}/elevator_group/admin_low": [e.code for e in lifts if "低区" in e.label],
                       f"entity://{p}/elevator_group/admin_high": [e.code for e in lifts if "高区" in e.label],
                       f"entity://{p}/elevator_group/freight": [e.code for e in self.site.equipment if e.type == "freight_elevator"]}

    def eid(self, name: str | None) -> str | None:
        """设备编号、zone:x、elevator_group:x、battery、air_system 或完整实体 ID → 实体 ID。"""
        if name is None or name.startswith("entity://"):
            return name
        p = self.site.plant_id
        if name in self.by_code:
            return self.by_code[name].entity_id
        kind, _, code = name.partition(":")
        if kind == "zone":
            return self.zone_eid[code]
        if kind == "elevator_group":
            return f"entity://{p}/elevator_group/{code}"
        if name == "battery":
            return self.battery_id
        if name == "air_system":
            return self.air_eid
        raise ValueError(f"剧本里的实体 {name} 不在装配中")

    def node_eid(self, node_id: str) -> str:
        """配电节点（电网、变压器、母线、配电柜）的实体 ID，如 TX1 → entity://{厂区}/transformer/TX1。"""
        node = self.site.nodes[node_id]
        return f"entity://{self.site.plant_id}/{node.type}/{node.id}"

    def zone_of(self, eid: str) -> str | None:
        if eid in self.by_eid:
            return self.by_eid[eid].zone
        for z, zid in self.zone_eid.items():
            if zid == eid:
                return z
        if "/elevator_group/" in eid:
            return "production" if eid.endswith("/freight") else "admin"
        return None


def active(actions: Sequence[SimAction], entity_id: str, command: str | None = None) -> list[SimAction]:
    return [a for a in actions if a.entity_id == entity_id and (command is None or a.command == command)]


def starting(actions: Sequence[SimAction], at: datetime) -> list[SimAction]:
    return [a for a in actions if a.interval.start == at]


def prune_effects(state: WorldState) -> None:
    state.effects = [e for e in state.effects if e.get("until") is None or datetime.fromisoformat(e["until"]) > state.at]


def clear_effects(state: WorldState, entity_id: str, kinds: set[str]) -> None:
    state.effects = [e for e in state.effects if not (e.get("entity_id") == entity_id and e["kind"] in kinds)]


# ---------------------------------------------------------------- 事件对世界的影响


DEFAULT_EFFECTS: dict[str, list[dict[str, Any]]] = {
    "elevator_congestion": [{"kind": "surge", "factor": 3.0, "minutes": 40}],
    "load_spike": [{"kind": "extra_load", "kw": 500.0, "minutes": 30}],
    "compressor_pressure_drop": [{"kind": "leak"}],
    "lighting_fault": [{"kind": "fault"}],
    "equipment_fault": [{"kind": "fault"}],
    "elevator_trapped": [{"kind": "fault"}, {"kind": "trapped"}],
    "heat_wave": [{"kind": "heat", "delta_c": 6.0, "minutes": 360}],
    "pv_drop": [{"kind": "pv_scale", "factor": 0.2, "minutes": 60}],
    "unknown_combo": [{"kind": "power_ratio", "entity": "LT-P", "ratio": 0.5}, {"kind": "extra_load", "entity": "AC-1", "kw": 40.0}],
}


def event_effects(event_type: str, entity_id: str | None, payload: dict[str, Any], at: datetime, index: SiteIndex) -> list[dict[str, Any]]:
    """剧本 payload.effects 优先，否则用 DEFAULT_EFFECTS；effect 的 entity 缺省取事件实体，minutes 缺省为一直持续到被清除。"""
    specs = payload.get("effects", DEFAULT_EFFECTS.get(event_type, []))
    out = []
    for spec in specs:
        spec = dict(spec)
        minutes = spec.pop("minutes", payload.get("minutes"))
        target = index.eid(spec.pop("entity", None)) or entity_id
        out.append(spec | {"entity_id": target, "since": at.isoformat(),
                           "until": (at + timedelta(minutes=minutes)).isoformat() if minutes else None})
    return out


# ---------------------------------------------------------------- 仿真器


class ProfileSim:
    """设备启停、故障与基础负荷：功率 = B18 负荷 ×（运行且无故障）+ 突增。一次性命令在动作起始分钟生效。"""

    name = "profile"

    def __init__(self, index: SiteIndex) -> None:
        self.index = index

    def init(self, state: WorldState) -> None:
        for e in self.index.site.equipment:
            state.devices[e.entity_id] = {"running": True, "fault": False}

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        for a in starting(sim_actions, state.at):
            dev = state.devices.get(a.entity_id)
            if a.command == "stop" and dev is not None:
                dev["running"] = False
            elif a.command == "start" and dev is not None:
                dev["running"] = True
            elif a.command == "repair":
                clear_effects(state, a.entity_id, {"fault", "leak", "trapped", "power_ratio", "extra_load"})
            elif a.command == "switch_circuit":  # 照明回路切到备用回路
                clear_effects(state, a.entity_id, {"fault", "power_ratio"})
                if dev is not None:
                    dev["circuit"] = "backup"
            elif a.command == "switch_standby" and dev is not None:
                target = self.index.eid(a.params.get("to"))
                dev["running"] = False
                if target in state.devices:
                    state.devices[target]["running"] = True
        inputs = state.inputs
        for code, base in inputs.equipment_kw.items():
            e = self.index.by_code.get(code)
            if e is None:  # 未计量余项
                state.power_kw[code] = base
                continue
            dev = state.devices[e.entity_id]
            dev["fault"] = bool(state.effect("fault", e.entity_id))
            on = dev["running"] and not dev["fault"]
            ratio = 1.0
            for eff in state.effect("power_ratio", e.entity_id):
                ratio *= eff["ratio"]
            ratio *= min((a.params.get("ratio", 0.0) for a in active(sim_actions, e.entity_id, "shed")), default=1.0)  # 重叠的削峰取最严一条，不连乘
            extra = sum(eff["kw"] for eff in state.effect("extra_load", e.entity_id))
            state.power_kw[code] = (base * ratio if on else 0.0) + extra
        return state


class HvacSim:
    """区域温度一阶模型：供冷时向设定值收敛（τ 15 分钟），停机时向室外温度 + 内热收敛（τ 120 分钟）。
    设定温度每低 1 ℃ 暖通功率 +10%；停机（推迟预冷）功率为 0；热浪每高出 30 ℃ 1 ℃ 功率 +4%。"""

    name = "hvac"

    def __init__(self, index: SiteIndex) -> None:
        self.index = index

    def init(self, state: WorldState) -> None:
        for z, cfg in ZONES.items():
            if z in self.index.zone_eid:
                sp = cfg["setpoint"]
                state.devices[self.index.zone_eid[z]] = {"temperature": sp if sp is not None else 27.0, "humidity": cfg["rh"],
                                                         "setpoint": sp, "mode": "normal"}

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        outdoor = state.inputs.outdoor_c + sum(e["delta_c"] for e in state.effect("heat"))
        minutes = dt / MINUTE
        for z, cfg in ZONES.items():
            zid = self.index.zone_eid.get(z)
            if zid is None:
                continue
            dev = state.devices[zid]
            sp = cfg["setpoint"]
            mode = "normal"
            for a in active(sim_actions, zid):
                if a.command == "set_setpoint":
                    sp = float(a.params["setpoint_c"])
                elif a.command == "set_mode":
                    mode = a.params["mode"]
            if mode == "precool" and sp is not None:
                sp -= 1.0
            dev["setpoint"], dev["mode"] = sp, mode
            cooling = [e for e in self.index.site.equipment if e.zone == z and e.type in COOLING_TYPES]
            factor = 0.0 if mode == "off" else 1.0
            if sp is not None and cfg["setpoint"] is not None:
                factor *= max(0.2, 1 + 0.1 * (cfg["setpoint"] - sp))
            factor *= 1 + 0.04 * max(0.0, outdoor - 30.0)
            rated = sum(e.rated_kw for e in cooling)
            power = 0.0
            for e in cooling:
                state.power_kw[e.code] = state.power_kw.get(e.code, 0.0) * factor
                power += state.power_kw[e.code]
            on = bool(cooling) and sp is not None and power > 0.15 * rated
            target, tau = (sp, 15.0) if on else (outdoor + 3.0, 120.0)
            dev["temperature"] = round(dev["temperature"] + (target - dev["temperature"]) * min(1.0, minutes / tau), 3)
            rh_target = cfg["rh"] if on else 70.0
            dev["humidity"] = round(dev["humidity"] + (rh_target - dev["humidity"]) * min(1.0, minutes / tau), 2)
        for a in starting(sim_actions, state.at):
            if a.command == "ventilate":  # 井道或区域通风：记一笔风机负荷
                state.effects.append({"kind": "extra_load", "entity_id": a.entity_id, "kw": float(a.params.get("kw", 5.0)),
                                      "since": state.at.isoformat(), "until": a.interval.end.isoformat()})
        return state


class ElevatorSim:
    """电梯群排队：到达人数来自 B18 人梯运行次数（每趟 8 人），拥堵事件乘系数；分区运行运力 ×1.3；
    故障或困人的轿厢不计运力。货梯按物料搬运任务排队。等待人·分钟累计进 sims 状态，作软代价。"""

    name = "elevator"

    def __init__(self, index: SiteIndex) -> None:
        self.index = index

    def init(self, state: WorldState) -> None:
        for gid, cars in self.index.groups.items():
            state.devices[gid] = {"queue": 0.0, "active_cars": len(cars), "mode": "normal"}
        state.sims["elevator"] = {"wait_person_min": 0.0, "wait_task_min": 0.0}

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        for a in starting(sim_actions, state.at):
            if a.command == "rescue":
                clear_effects(state, a.entity_id, {"trapped"})
        for code in (c for cars in self.index.groups.values() for c in cars):
            eid = self.index.by_code[code].entity_id
            state.devices[eid]["trapped"] = bool(state.effect("trapped", eid))
        occ = state.inputs.occupancy
        minutes = dt / MINUTE
        for gid, cars in self.index.groups.items():
            dev = state.devices[gid]
            freight = gid.endswith("/freight")
            mode, wanted = "normal", len(cars)
            for a in active(sim_actions, gid):
                if a.command == "set_mode":
                    mode = a.params["mode"]
                elif a.command == "set_active":
                    wanted = int(a.params["cars"])
            usable = [c for c in cars if not state.devices[self.index.by_code[c].entity_id]["fault"]
                      and state.devices[self.index.by_code[c].entity_id]["running"]]
            n = min(wanted, len(usable))
            surge = 1.0
            for e in state.effect("surge", gid):
                surge *= e["factor"]
            if freight:
                arrivals = occ["tasks:material_handling"] * surge * minutes
                capacity = n * FREIGHT_TASKS_PER_MIN * minutes
            else:
                arrivals = occ["trips:passenger_elevator"] * PERSONS_PER_TRIP / 2 * surge * minutes
                capacity = n * CAR_PERSONS_PER_MIN * (ZONE_BOOST if mode == "zone" else 1.0) * minutes
            served = min(dev["queue"] + arrivals, capacity)
            dev["queue"] = round(max(0.0, dev["queue"] + arrivals - capacity), 2)
            dev["active_cars"], dev["mode"] = n, mode
            state.sims["elevator"]["wait_task_min" if freight else "wait_person_min"] += dev["queue"] * minutes
            if not freight and surge > 1:
                extra_kw = max(0.0, served - arrivals / surge) / PERSONS_PER_TRIP * TRIP_KWH * 60 / max(minutes, 1)
                for c in usable[:n]:
                    state.power_kw[c] = state.power_kw.get(c, 0.0) + extra_kw / max(n, 1)
            for c in usable[n:]:
                state.power_kw[c] = state.power_kw.get(c, 0.0) * 0.3  # 停梯节能只剩待机
        return state


class AirSystemSim:
    """空压管网：泄漏时压力每分钟降 0.02 bar（不低于 5.5），在运空压机功率 +25%；无泄漏时向 7.0 bar 回升。"""

    name = "air"

    def __init__(self, index: SiteIndex) -> None:
        self.index = index

    def init(self, state: WorldState) -> None:
        state.devices[self.index.air_eid] = {"pressure_bar": 7.0}

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        for a in starting(sim_actions, state.at):
            if a.command == "isolate_leak":
                for e in list(state.effect("leak")):
                    clear_effects(state, e["entity_id"], {"leak"})
        dev = state.devices[self.index.air_eid]
        leaking = bool(state.effect("leak"))
        minutes = dt / MINUTE
        p = dev["pressure_bar"]
        dev["pressure_bar"] = round(max(5.5, p - 0.02 * minutes) if leaking else min(7.0, p + 0.05 * minutes), 3)
        if leaking:
            for e in self.index.site.equipment:
                if e.type == "air_compressor":
                    state.power_kw[e.code] = state.power_kw.get(e.code, 0.0) * 1.25
        return state


SIM_HOOK = "scenario_simulator"  # 场景注册表里的仿真器工厂 fn(index) -> 仿真器；与默认仿真器同名时原位替换，否则追加
# 仿真器可带 stage 属性：缺省为 physics；"observe"（传感器观测层，D59）一律排在所有物理仿真之后


def default_simulators(index: SiteIndex, registry: Any = None) -> list:
    sims = [ProfileSim(index), HvacSim(index), ElevatorSim(index), AirSystemSim(index)]
    for factory in registry.hooks_for(SIM_HOOK) if registry is not None else ():
        sim = factory(index)
        names = [s.name for s in sims]
        if sim.name in names:
            sims[names.index(sim.name)] = sim
        else:
            sims.append(sim)
    sims.sort(key=lambda s: getattr(s, "stage", "physics") == "observe")  # 稳定排序：physics 原序不变，observe 置后
    return sims
