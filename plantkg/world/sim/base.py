"""仿真端口（B17）：场景世界的状态、分钟输入与 Simulator 协议。

场景仿真的时钟只由 B17 的 ScenarioRunner 推进，内部 1 分钟子步。各领域仿真器（电梯 B20、温度 B21、故障 B22）
实现 `Simulator.step(state, sim_actions, dt) -> state`：读本分钟输入与已生效的动作，改设备状态并写本分钟功率。
状态可整体深拷贝（分叉、试算），随机数状态随快照保存，所以同一快照、同一种子推进的结果一致。
"""
from __future__ import annotations

import copy
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from plantkg.core.contracts import SimAction

MINUTE = timedelta(minutes=1)
SLOT = timedelta(minutes=15)


@dataclass(frozen=True)
class MinuteInputs:
    """一分钟的外部输入（ScenarioInputProvider 给出）。kW 为该分钟平均功率。"""

    at: datetime
    equipment_kw: dict[str, float]  # 设备编号（及未计量余项）→ 基础负荷
    pv_kw: float
    price_cny_per_kwh: float | None
    outdoor_c: float
    occupancy: dict[str, float]  # occupancy:admin / occupancy:production / tasks:material_handling（每分钟）


@dataclass
class WorldState:
    """场景世界在某一分钟开始时的状态。devices 里的字段进 state_series；sims 是各仿真器的私有状态。"""

    at: datetime
    devices: dict[str, dict[str, Any]]
    sims: dict[str, dict[str, Any]] = field(default_factory=dict)
    effects: list[dict[str, Any]] = field(default_factory=list)  # 生效中的事件影响（故障、突增、热浪……）
    rng: Any = None  # random.Random.getstate()
    state_version: int = 0  # 事件或动作改动离散状态时 +1；迟到结果按它判过期
    # 以下是本分钟的中间量，不进快照
    inputs: MinuteInputs | None = None
    power_kw: dict[str, float] = field(default_factory=dict)

    def random(self) -> random.Random:
        r = random.Random()
        r.setstate(_tuple(self.rng))
        return r

    def keep_random(self, r: random.Random) -> None:
        self.rng = r.getstate()

    def copy(self) -> WorldState:
        return WorldState(at=self.at, devices=copy.deepcopy(self.devices), sims=copy.deepcopy(self.sims),
                          effects=copy.deepcopy(self.effects), rng=self.rng, state_version=self.state_version)

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "devices": self.devices, "sims": self.sims, "effects": self.effects,
                "rng": _list(self.rng), "state_version": self.state_version}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WorldState:
        return cls(at=datetime.fromisoformat(data["at"]), devices=copy.deepcopy(data["devices"]), sims=copy.deepcopy(data["sims"]),
                   effects=copy.deepcopy(data["effects"]), rng=_tuple(data["rng"]), state_version=data["state_version"])

    def effect(self, kind: str, entity_id: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.effects if e["kind"] == kind and (entity_id is None or e.get("entity_id") == entity_id)]


def _list(value: Any) -> Any:
    return [_list(v) for v in value] if isinstance(value, (tuple, list)) else value


def _tuple(value: Any) -> Any:
    return tuple(_tuple(v) for v in value) if isinstance(value, (tuple, list)) else value


@runtime_checkable
class Simulator(Protocol):
    """一个领域仿真器。step 读 state.inputs 与本分钟生效的 sim_actions，原地更新并返回 state。"""

    name: str

    def init(self, state: WorldState) -> None: ...

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState: ...
