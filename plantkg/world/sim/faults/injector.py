"""设备故障注入与空压遥测（B22）。

- `FaultInjector` 是 B17 场景世界的 Simulator：按剧本时刻（或按种子随机）让设备读数偏离。
  支持空压泄漏、照明回路失电、电机过载。泄漏的信号形状取 MetroPT-3 官方泄漏时段，按本厂空压机
  额定参数缩放后注入；注入记录标 `derived`，来源写“MetroPT-3 measured（地铁空压机，非本厂设备）”，
  缩放步骤写进 `transform_steps`。
- `EquipmentTelemetry` 也是 Simulator：读本分钟的设备功率与事件影响，产生电流、油温、管网压力读数，
  供设备监测专家做阈值 + 残差研判。读数标 `simulated`（场景仿真结果，不是现场实测）。

写在 `WorldState.sims["injections"]` 与 `WorldState.sims["equipment"]["samples"]` 里的状态随快照复制，
同一剧本同一种子两次一致。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from plantkg.core.contracts import SimAction
from plantkg.world.sim.base import MINUTE, WorldState
from plantkg.world.site.model import Equipment, SiteModel

from .metropt3 import Sample, summarize, scale_steps

InjectionKind = Literal["compressor_leak", "lighting_circuit_loss", "motor_overload"]
LEAK_NOTE = "空压泄漏：MetroPT-3 官方泄漏时段 2020-05-29 23:30～05-30 06:00 的信号形状，按本厂额定参数缩放"
_VOLTAGE_V, _POWER_FACTOR = 380.0, 0.9


@dataclass(frozen=True)
class Injection:
    """一条注入：kind、设备编号（如 AC-2 / LT-A03）、注入时刻。"""

    kind: InjectionKind
    entity: str
    at: datetime
    params: dict[str, Any] = field(default_factory=dict)


def random_injections(site: SiteModel, start: datetime, end: datetime, *, seed: int, leak_per_day: int = 1) -> list[Injection]:
    """按种子随机排注入：每个完整日一次空压泄漏，时段 09:00～17:00；另有一次照明回路失电。
    同一 seed 结果一致，便于回放。"""
    compressors = [e.code for e in site.equipment if e.type == "air_compressor"]
    lights = [e.code for e in site.equipment if e.type == "lighting"]
    if not compressors or not lights:
        return []
    rng = random.Random(f"b22-faults:{seed}")
    out: list[Injection] = []
    day = start
    while day + timedelta(days=1) <= end:
        for _ in range(leak_per_day):
            at = day + timedelta(minutes=9 * 60 + rng.randrange(8 * 60))
            out.append(Injection("compressor_leak", rng.choice(compressors), at, {"seed": seed}))
        out.append(Injection("lighting_circuit_loss", rng.choice(lights), day + timedelta(minutes=10 * 60 + rng.randrange(7 * 60)),
                             {"seed": seed}))
        day += timedelta(days=1)
    return sorted(out, key=lambda i: i.at)


class FaultInjector:
    """B17 Simulator：到点把注入写成 WorldState.effects，事件与告警仍由剧本/仿真产生。"""

    name = "equipment_faults"

    def __init__(self, site: SiteModel, injections: list[Injection] | tuple[Injection, ...] = ()) -> None:
        self.site = site
        self.injections = sorted(injections, key=lambda i: i.at)
        self.by_code = {e.code: e for e in site.equipment}
        self.records: list[dict] = []

    def init(self, state: WorldState) -> None:
        state.sims.setdefault("injections", [])

    def step(self, state: WorldState, sim_actions: list[SimAction] | tuple[SimAction, ...], dt: timedelta) -> WorldState:
        for injection in self.injections:
            if not (state.at <= injection.at < state.at + dt):
                continue
            equipment = self.by_code.get(injection.entity)
            if equipment is None:
                raise ValueError(f"注入对象 {injection.entity} 不在装配中")
            effects, record = self._apply(equipment, injection, state.at)
            state.effects.extend(effects)
            state.state_version += 1
            state.sims.setdefault("injections", []).append(record)
            self.records.append(record)
        return state

    def _apply(self, equipment: Equipment, injection: Injection, at: datetime) -> tuple[list[dict], dict]:
        base = {"entity_id": equipment.entity_id, "entity": equipment.code, "since": at.isoformat(), "until": None}
        if injection.kind == "compressor_leak":
            effect = base | {"kind": "leak"}
            record = {"injection_id": f"inj-{len(self.records) + 1:03d}", "kind": "compressor_leak", "entity": equipment.code,
                      "at": at.isoformat(), "nature": "derived", "source": "MetroPT-3 measured",
                      "source_note": "地铁空压机，非本厂设备；只借用泄漏时段的信号形状",
                      "transform_steps": [step.model_dump(mode="json") for step in scale_steps(equipment.rated_kw)],
                      "note": LEAK_NOTE}
            return [effect], record
        if injection.kind == "lighting_circuit_loss":
            effect = base | {"kind": "fault"}
            record = {"injection_id": f"inj-{len(self.records) + 1:03d}", "kind": "lighting_circuit_loss", "entity": equipment.code,
                      "at": at.isoformat(), "nature": "synthetic", "source": "b22-fault-injector",
                      "transform_steps": [], "note": "照明回路失电（按常理编写的注入，由能源专家处置）"}
            return [effect], record
        factor = float(injection.params.get("factor", 1.3))
        effect = base | {"kind": "overload", "factor": factor}
        record = {"injection_id": f"inj-{len(self.records) + 1:03d}", "kind": "motor_overload", "entity": equipment.code,
                  "at": at.isoformat(), "nature": "synthetic", "source": "b22-fault-injector", "transform_steps": [],
                  "note": f"电机过载注入：电流乘 {factor}"}
        return [effect], record


class EquipmentTelemetry:
    """B17 Simulator：从本分钟功率、事件影响产生空压机读数。放在仿真器列表最后，读到泄漏后的功率。"""

    name = "equipment"

    def __init__(self, site: SiteModel, air_entity_id: str, *, keep_minutes: int = 240, oil_tau_min: float = 20.0) -> None:
        self.site = site
        self.air_entity_id = air_entity_id
        self.compressors = tuple(e for e in site.equipment if e.type == "air_compressor")
        self.keep_minutes = keep_minutes
        self.oil_tau_min = oil_tau_min

    def init(self, state: WorldState) -> None:
        state.sims.setdefault("equipment", {"samples": []})
        for e in self.compressors:
            state.devices.setdefault(e.entity_id, {}).update({"current_a": 0.0, "oil_c": 65.0, "on": True})

    def step(self, state: WorldState, sim_actions: list[SimAction] | tuple[SimAction, ...], dt: timedelta) -> WorldState:
        air = state.devices.get(self.air_entity_id, {})
        pressure = float(air.get("pressure_bar", 7.0))
        leaking = bool(state.effect("leak"))
        samples: list[dict] = state.sims.setdefault("equipment", {"samples": []})["samples"]
        for e in self.compressors:
            dev = state.devices.setdefault(e.entity_id, {})
            power = float(state.power_kw.get(e.code, 0.0))
            fault = bool(state.effect("fault", e.entity_id))
            running = bool(dev.get("running", True)) and not fault
            overload = max((float(x.get("factor", 1.0)) for x in state.effect("overload", e.entity_id)), default=1.0)
            current = power * 1000 / (math.sqrt(3) * _VOLTAGE_V * _POWER_FACTOR) * overload if running else 0.0
            load_ratio = min(1.2, power / e.rated_kw) if e.rated_kw else 0.0
            target = 62.0 + 12.0 * load_ratio + (11.0 if leaking else 0.0)
            oil = float(dev.get("oil_c", 65.0)) + (target - float(dev.get("oil_c", 65.0))) * min(1.0, dt / MINUTE / self.oil_tau_min)
            dev.update({"current_a": round(current, 2), "oil_c": round(oil, 2), "on": bool(running and current > 2.0), "running": running})
            samples.append({"code": e.code, "at": state.at.isoformat(), "current_a": round(current, 2), "oil_c": round(oil, 2),
                            "pressure_bar": round(pressure, 3), "on": bool(running and current > 2.0)})
        cutoff = state.at - timedelta(minutes=self.keep_minutes)
        state.sims["equipment"]["samples"] = [s for s in samples if datetime.fromisoformat(s["at"]) >= cutoff]
        return state

    @staticmethod
    def window(state: WorldState, code: str, end: datetime, minutes: int) -> list[dict]:
        """取 [end−minutes, end) 内某台空压机的读数。"""
        start = end - timedelta(minutes=minutes)
        return [s for s in state.sims.get("equipment", {}).get("samples", [])
                if s.get("code") == code and start <= datetime.fromisoformat(s["at"]) < end]

    @staticmethod
    def summarize(samples: list[dict], *, window: str = "") -> dict:
        """读数窗口 → 运行占比、电流均值、油温、压力斜率；性质标 simulated。"""
        if len(samples) < 2:
            return {"window": window, "nature": "simulated", "samples": len(samples), "running_ratio": 0.0, "current_mean_a": 0.0,
                    "oil_mean_c": 0.0, "oil_start_c": 0.0, "oil_end_c": 0.0, "pressure_start_bar": 0.0, "pressure_end_bar": 0.0,
                    "pressure_drop_bar_per_h": 0.0}
        stats = summarize([Sample(at=datetime.fromisoformat(s["at"]), current_a=s["current_a"], oil_c=s["oil_c"],
                                  pressure_bar=s["pressure_bar"], on=s["on"]) for s in samples], window=window, sampling_s=60.0)
        return stats.to_dict() | {"nature": "simulated"}

    @staticmethod
    def reading_state(samples: list[dict], *, window: str = "") -> dict:
        return {"window": window, "sampling_s": 60, "nature": "simulated", "samples": samples}
