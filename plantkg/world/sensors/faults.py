"""传感器故障注入与试探动作（B22 / D59、D61）。

- `SensorFaultInjector` 是 B17 场景世界的 Simulator：按剧本时刻激活卡死、漂移、掉线、跳变、偏置 5 种
  传感器故障，写进 `WorldState.sims["sensor_faults"]`，与设备故障（`sim/faults`）分开。
- `TrialActionSim` 给带 `params.trial` 的可撤回动作加短时负荷与管网压力反应，让“启动备用空压机”这类
  试探在真实状态上产生可观测变化；SensorLayer 只读真实状态与故障表，不知道试探是 Agent 发起的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from plantkg.core.contracts import SimAction
from plantkg.world.sim.base import WorldState

SensorFaultKind = Literal["stuck", "drift", "offline", "spike", "bias"]
_KINDS = {"stuck", "drift", "offline", "spike", "bias"}


@dataclass(frozen=True)
class SensorFault:
    """一条传感器故障：传感器 ID、种类、起止时刻与参数（卡死的值/漂移速率/跳变幅度/偏置）。"""

    sensor_id: str
    kind: SensorFaultKind
    start: datetime
    end: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"传感器故障种类 {self.kind} 不在 {sorted(_KINDS)}")


class SensorFaultInjector:
    """B17 Simulator：到点把故障写进 state.sims["sensor_faults"]["active"]，结束即撤掉。"""

    name = "sensor_faults"

    def __init__(self, faults: list[SensorFault] | tuple[SensorFault, ...] = ()) -> None:
        self.faults = sorted(faults, key=lambda f: f.start)
        self.events: list[dict] = []

    def init(self, state: WorldState) -> None:
        state.sims.setdefault("sensor_faults", {"active": [], "events": []})

    def step(self, state: WorldState, sim_actions: list[SimAction] | tuple[SimAction, ...], dt: timedelta) -> WorldState:
        box = state.sims.setdefault("sensor_faults", {"active": [], "events": []})
        previous = {f["fault_id"]: f for f in box.get("active", [])}
        active = []
        for index, fault in enumerate(self.faults):
            if state.at < fault.start or (fault.end is not None and state.at >= fault.end):
                continue
            fault_id = f"sf-{index + 1:02d}"
            entry = {"fault_id": fault_id, "sensor_id": fault.sensor_id, "kind": fault.kind,
                     "since": fault.start.isoformat(), "until": fault.end.isoformat() if fault.end else None, "params": dict(fault.params)}
            if fault.kind == "stuck" and fault_id in previous and "frozen" in previous[fault_id]:
                entry["frozen"] = previous[fault_id]["frozen"]
            active.append(entry)
        box["active"] = active
        now_ids = {f["fault_id"] for f in active}
        for entry in active:
            if entry["fault_id"] not in {e.get("fault_id") for e in box["events"]}:
                box["events"].append({"at": state.at.isoformat(), "event": "activated", **entry})
                self.events.append({"at": state.at.isoformat(), "event": "activated", **entry})
        for fault_id in set(previous) - now_ids:
            box["events"].append({"at": state.at.isoformat(), "event": "cleared", "fault_id": fault_id})
        return state


class TrialActionSim:
    """B17 Simulator：带 params.trial 的 start/switch_standby 在动作区间内加负荷与压力反应。
    这是可撤回试探的“世界响应”，不是执行器；动作本身仍由 B17 apply 写入。
    目标上一分钟已在运行时启动是空操作，不加负荷与压力反应（否则世界的变化不是启动造成的）。"""

    name = "trial_actions"

    def init(self, state: WorldState) -> None:
        pass

    def step(self, state: WorldState, sim_actions: list[SimAction] | tuple[SimAction, ...], dt: timedelta) -> WorldState:
        box = state.sims.setdefault("trial_actions", {"running": {}})
        was_running = dict(box["running"])
        box["running"] = {eid: bool(dev.get("running")) for eid, dev in state.devices.items() if "running" in dev}
        for action in sim_actions:
            if action.interval.start != state.at or not action.params.get("trial"):
                continue
            if action.command not in ("start", "switch_standby"):
                continue
            if was_running.get(action.params.get("to") or action.entity_id, True):
                continue
            kw = float(action.params.get("kw", 30.0))
            boost = float(action.params.get("pressure_boost_bar", 0.2))
            until = action.interval.end.isoformat()
            state.effects.append({"kind": "extra_load", "entity_id": action.entity_id, "kw": kw, "since": state.at.isoformat(),
                                  "until": until, "source": "trial"})
            state.effects.append({"kind": "pressure_boost", "entity_id": action.entity_id, "bar": boost, "since": state.at.isoformat(),
                                  "until": until, "source": "trial"})
        return state
