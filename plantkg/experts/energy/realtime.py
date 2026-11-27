"""B19 分钟层：`RealtimeController.step(minute_state, slot_target) → SimAction`。

15 分钟层（B09 重解后的计划）给每个整格一条“购电目标线”
`目标线 = 预测负荷 − 预测光伏 + 计划充电 − 计划放电`；
分钟层每分钟按实际负荷与光伏算出“不动储能时的购电”，用充放电把它压回目标线：

- 实际高于目标线 → 放电（受功率上限、SOC 下限约束）；
- 实际低于目标线且本格计划在充电 → 按差额补齐充电（低价时段按计划补充电）；
- 期末兜底：最后 END_GUARD 内放电不低于期末目标（`end_target_kwh`），储能低于目标时到最后一刻补电；
- 光伏富余（负荷 − 光伏 < 0）时按剩余空间吸收，避免弃光；
- 达不到的部分如实留在账本里，不虚报。

事件的响应预算由调用方给出：`respond_from` 之前只按计划动作，不做偏差修正，所以响应预算为 1 分钟时
事件后第 1 个整分钟才放电，之前的分钟记录不变。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from plantkg.core.contracts import Interval, SimAction, StorageParams

TOLERANCE_KW = 0.5
END_GUARD = timedelta(hours=1)  # 距日末这段时间内，放电不得把储能压到期末目标以下


@dataclass(frozen=True)
class SlotTarget:
    """15 分钟层给本整格的目标：目标线、计划充放与（可选）响应生效时刻。"""

    start: datetime
    end: datetime
    load_kw: float  # 15 分钟层预测负荷
    pv_kw: float  # 15 分钟层预测光伏
    charge_kw: float = 0.0  # 计划充电
    discharge_kw: float = 0.0  # 计划放电
    price_cny_per_kwh: float | None = None
    active_from: datetime | None = None  # 事件响应预算后的生效时刻

    @property
    def import_line_kw(self) -> float:
        return max(0.0, self.load_kw - self.pv_kw + self.charge_kw - self.discharge_kw)


@dataclass
class RealtimeController:
    """只关注储能的分钟层控制器。`minute_state` 至少给 at / load_kw / pv_kw / energy_kwh。"""

    battery_id: str
    storage: StorageParams
    charge_kw: float
    discharge_kw: float
    respond_from: datetime | None = None  # 事件响应预算后的第一个生效分钟；None 表示无待响应事件
    end_target_kwh: float | None = None  # 整日期末目标；与 end_at 一起给放电加期末下限
    end_at: datetime | None = None
    history: list[dict] = field(default_factory=list)

    def storage_bounds(self) -> tuple[float, float]:
        return self.storage.soc_min * self.storage.capacity_kwh, self.storage.soc_max * self.storage.capacity_kwh

    def step(self, minute_state: Mapping, slot_target: SlotTarget) -> SimAction:
        at: datetime = minute_state["at"]
        load = float(minute_state["load_kw"])
        pv = float(minute_state["pv_kw"])
        energy = float(minute_state["energy_kwh"])
        e_min, e_max = self.storage_bounds()
        guard = self.end_at is not None and self.end_target_kwh is not None and self.end_at - END_GUARD <= at < self.end_at
        if guard:
            e_min = max(e_min, min(self.end_target_kwh, e_max))  # 只用于放电下限：充电仍按 e_max

        gate = max([d for d in (slot_target.active_from, self.respond_from) if d is not None], default=None)
        if gate is not None and at < gate:
            charge, discharge, why = self._plan_power(slot_target, energy, e_min, e_max)
        else:
            charge, discharge, why = self._track(load, pv, slot_target, energy, e_min, e_max)
        if guard and discharge <= TOLERANCE_KW and energy < self.end_target_kwh:
            # 期末补电：留到最后一刻（之后全速充也刚好够）才补，免得提前多充
            later_kwh = (((self.end_at - at) / timedelta(minutes=1)) - 1) * self.charge_kw * self.storage.eta_c / 60
            late_charge = (self.end_target_kwh - energy - later_kwh) / self.storage.eta_c * 60
            if late_charge > charge:
                charge, why = min(late_charge, self.charge_kw, max(0.0, (e_max - energy) / self.storage.eta_c * 60)), "期末补电：向期末目标靠拢"

        command = "charge" if charge > TOLERANCE_KW else "discharge" if discharge > TOLERANCE_KW else "idle"
        power = charge if command == "charge" else discharge if command == "discharge" else 0.0
        self.history.append({"at": at.isoformat(), "command": command, "power_kw": round(power, 3),
                             "import_without_storage_kw": round(load - pv, 3), "line_kw": round(slot_target.import_line_kw, 3),
                             "energy_kwh": round(energy, 3), "reason": why})
        return SimAction(entity_id=self.battery_id, command=command, params={"power_kw": round(power, 3), "layer": "minute"},
                         interval=Interval(start=at, end=at + timedelta(minutes=1)))

    # ---- 目标线跟踪
    def _track(self, load: float, pv: float, target: SlotTarget, energy: float,
               e_min: float, e_max: float) -> tuple[float, float, str]:
        # 不动储能时的购电与实际所需储能供给（正=放电，负=充电）
        need_kw = (load - pv) - target.import_line_kw
        if need_kw > TOLERANCE_KW:
            discharge = min(need_kw, self.discharge_kw, max(0.0, (energy - e_min) * self.storage.eta_d * 60))
            return 0.0, discharge, "实际购电高于目标线，放电削峰"
        if need_kw < -TOLERANCE_KW:
            surplus_pv = load - pv < 0
            if target.charge_kw <= TOLERANCE_KW and not surplus_pv:
                return 0.0, 0.0, "本格计划不充电、光伏无富余：只跟踪放电侧"
            room = max(0.0, (e_max - energy) / self.storage.eta_c * 60)  # kWh → 该分钟可充功率上限（kW）
            charge = min(-need_kw, self.charge_kw, room)
            return charge, 0.0, "实际低于目标线，按计划补充电" if target.charge_kw > 0 else "光伏富余，充电吸收"
        return 0.0, 0.0, "实际购电已在目标线上"

    # ---- 响应预算之前：只执行 15 分钟层的计划功率
    def _plan_power(self, target: SlotTarget, energy: float, e_min: float, e_max: float) -> tuple[float, float, str]:
        if target.charge_kw > TOLERANCE_KW:
            room = max(0.0, (e_max - energy) / self.storage.eta_c * 60)
            return min(target.charge_kw, self.charge_kw, room), 0.0, "响应预算内：按计划充电"
        if target.discharge_kw > TOLERANCE_KW:
            available = max(0.0, (energy - e_min) * self.storage.eta_d * 60)
            return 0.0, min(target.discharge_kw, self.discharge_kw, available), "响应预算内：按计划放电"
        return 0.0, 0.0, "响应预算内：按计划待机"


def target_from_slot(slot_start: datetime, *, load_kw: float, pv_kw: float, charge_kw: float, discharge_kw: float,
                     price_cny_per_kwh: float | None = None, active_from: datetime | None = None) -> SlotTarget:
    return SlotTarget(start=slot_start, end=slot_start + timedelta(minutes=15), load_kw=load_kw, pv_kw=pv_kw,
                      charge_kw=charge_kw, discharge_kw=discharge_kw, price_cny_per_kwh=price_cny_per_kwh,
                      active_from=active_from)
