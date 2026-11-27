"""暖通专家（B21）：事件单 → 提案（预冷、推迟预冷、临时上调设定温度、井道通风）。

规则按常理编写（configured），代价用同一把尺子：cny 按峰谷价折算的电费差（负数为省），comfort_min 是按
本区一阶 RC 模型估的温度越界分钟。洁净车间与数据机房是硬约束（温湿度不能动），削峰时一律不动，也不在提案里出现。

接口：`HvacExpert.propose(event, state) -> ExpertProposal`。event 为事件单（event_type / entity_id / payload），
state 为当分钟的世界视图：{"at", "outdoor_c", "zones": {区域: {"temperature", "setpoint", "mode", "power_kw", "gain_kw"}}}，
缺的键按区域配置补默认值。ticket_id 由会诊调用方给（绑定里的 ticket_id），未给时用 state/event 的，再缺用占位。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from plantkg.core.contracts import ExpertProposal, Interval, ProposalCost, ProposedAction
from plantkg.world.scenario.sims import DEFAULT_EFFECTS
from plantkg.world.sim.thermal import ZoneSpec, load_zones

DEFER_MIN = 15  # 推迟预冷至少 15 分钟：让出峰段容量；事件持续更久时按事件持续时间
PRECOOL_MIN = 60  # 高温前预冷 1 小时
SHED_MIN = 30  # 容量受限时临时上调设定温度 30 分钟
SETPOINT_RAISE_C = 1.0
DEFAULT_PRICE_CNY_PER_KWH = 0.65  # 演示峰价，真正的电费由 B08/B16 账本结算
HARD_NOTE = "洁净车间与数据机房是硬约束（温湿度不能动），不参与削峰"


@dataclass
class HvacExpert:
    """zones 缺省读 configs/hvac/thermal.yaml；price_cny_per_kwh 只用于把让出的负荷折成元。"""

    zones: Mapping[str, ZoneSpec] = field(default_factory=load_zones)
    price_cny_per_kwh: float = DEFAULT_PRICE_CNY_PER_KWH

    # ---------------------------------------------------------------- 入口

    def propose(self, event: Mapping[str, Any], state: Mapping[str, Any], *, ticket_id: str | None = None) -> ExpertProposal:
        kind = str(event.get("event_type", ""))
        at = _instant(state.get("at"))
        tid = ticket_id or state.get("ticket_id") or event.get("ticket_id") or "TK-hvac"
        outdoor = float(state.get("outdoor_c") or 30.0)  # 首分钟视图可能还没有室外温度
        if kind in ("load_spike", "sim_limit", "pv_drop"):
            return self._defer_precool(event, state, at, outdoor, tid)
        if kind == "heat_wave":
            return self._precool(event, state, at, outdoor, tid)
        if kind == "capacity_shed_request":
            return self._raise_setpoint(event, state, at, outdoor, tid)
        if kind == "elevator_trapped":
            return self._ventilate(event, state, at, tid)
        return self._proposal(tid, actions=(), cny=0.0, comfort=0.0, confidence=0.5,
                              diagnosis="不属暖通领域：区域温度、预冷与错峰、舒适度")

    # ---------------------------------------------------------------- 动作

    def _defer_precool(self, event, state, at, outdoor, tid) -> ExpertProposal:
        """推迟预冷：峰段让出该区域的供冷，冷量之后补；温度会先上浮。"""
        zone = self._pick_zone(event, state)
        if zone is None:
            return self._proposal(tid, confidence=0.4, diagnosis=f"{HARD_NOTE}；没有可让容量的区域")
        view = self._view(zone, state)
        spec = self.zones[zone]
        room = spec.comfort_c[1] - 0.5
        if view["temperature"] >= room:
            return self._proposal(tid, confidence=0.65,
                                  diagnosis=f"{spec.label} 已 {view['temperature']:.1f} ℃，接近 {spec.comfort_c[1]:.0f} ℃ 上限，不能再推迟供冷")
        minutes = max(DEFER_MIN, _event_minutes(event))  # 动作窗口盖住事件持续时间，否则峰值落在窗口之后
        end_c, comfort = _drift(spec, view["temperature"], outdoor, view["gain_kw"], minutes)
        if comfort > 0 and minutes > DEFER_MIN:  # 整段推迟会越界时退回最短窗口
            minutes = DEFER_MIN
            end_c, comfort = _drift(spec, view["temperature"], outdoor, view["gain_kw"], minutes)
        shed = view["power_kw"]
        action = _act(view["entity_id"], "set_mode", at, minutes, mode="off")
        return self._proposal(
            tid, actions=(action,), cny=-shed * minutes / 60 * self.price_cny_per_kwh, comfort=comfort, confidence=0.8,
            diagnosis=f"{spec.label}推迟预冷 {minutes} 分钟：峰段让出 {shed:.0f} kW，温度由 {view['temperature']:.1f} 升到 "
                      f"{end_c:.1f} ℃（估越界 {comfort} 分钟）")

    def _precool(self, event, state, at, outdoor, tid) -> ExpertProposal:
        """预冷：高温来临前把设定温度压低 1 ℃，把冷量提前买进去。硬约束区域用各自的设定温度。"""
        area = self._pick_zone(event, state)
        if area is None:
            return self._proposal(tid, confidence=0.4, diagnosis=f"{HARD_NOTE}；没有可预冷的区域")
        view = self._view(area, state)
        spec = self.zones[area]
        action = _act(view["entity_id"], "set_mode", at, PRECOOL_MIN, mode="precool")
        extra = view["power_kw"] * 0.5 * PRECOOL_MIN / 60 * self.price_cny_per_kwh
        return self._proposal(
            tid, actions=(action,), cny=extra, comfort=0, confidence=0.7,
            diagnosis=f"{spec.label}高温前预冷 {PRECOOL_MIN} 分钟（设定 {spec.setpoint_c - 1:.0f} ℃），把冷量提前买进去、"
                      f"峰段负荷下移；机组已到容量上限时省得有限。{HARD_NOTE}")

    def _raise_setpoint(self, event, state, at, outdoor, tid) -> ExpertProposal:
        """容量受限：把非硬约束区域的设定温度临时上调 1 ℃，比直接停机温和。"""
        actions, comfort, cny, names = [], 0, 0.0, []
        for zone, spec in self.zones.items():
            if spec.hard:
                continue
            view = self._view(zone, state)
            base = view["setpoint"] + (1.0 if view["mode"] == "precool" else 0.0)  # 预冷期间视图设定已减 1 ℃，还原
            raise_c = min(SETPOINT_RAISE_C, spec.comfort_c[1] - base)
            if raise_c <= 0.05:
                continue
            _, comfort_min = _drift(spec, view["temperature"], outdoor, view["gain_kw"], SHED_MIN)
            actions.append(_act(view["entity_id"], "set_setpoint", at, SHED_MIN, setpoint_c=round(base + raise_c, 2)))
            comfort += comfort_min
            cny -= view["power_kw"] * 0.4 * SHED_MIN / 60 * self.price_cny_per_kwh
            names.append(spec.label)
        if not actions:
            return self._proposal(tid, confidence=0.4, diagnosis=f"{HARD_NOTE}；其余区域已在舒适上限，无法再上调")
        return self._proposal(tid, actions=tuple(actions), cny=cny, comfort=comfort, confidence=0.7,
                              diagnosis=f"削峰：{'、'.join(names)}设定温度临时上调 {SETPOINT_RAISE_C:.0f} ℃、{SHED_MIN} 分钟；{HARD_NOTE}")

    def _ventilate(self, event, state, at, tid) -> ExpertProposal:
        entity = str(event.get("entity_id") or "")
        if not entity:
            return self._proposal(tid, confidence=0.3, diagnosis="困人事件没有轿厢编号，无法定位井道")
        confirmed = bool(state.get("confirmed", False))
        action = _act(entity, "ventilate", at, 60, readiness="ready" if confirmed else "needs_human", kw=3.0)
        return self._proposal(tid, actions=(action,), cny=0.0, comfort=0.0, confidence=0.9, safety=("people_in_car",),
                              diagnosis="给井道通风，降低轿厢内温升；是否执行由值班人员确认" + ("" if confirmed else "（尚未确认人在轿厢）"))

    # ---------------------------------------------------------------- 视图与选择

    def _view(self, zone: str, state) -> dict[str, Any]:
        """区域当前值 + 区域配置；缺的键按配置补，温度缺失时用设定温度。entity_id 由调用方给（区域实体）。"""
        spec = self.zones[zone]
        given = (state.get("zones") or {}).get(zone) or {}
        entity_id = given.get("entity_id") or (f"entity://{state['plant_id']}/zone/{zone}" if state.get("plant_id") else None)
        if entity_id is None:
            raise ValueError(f"区域 {zone} 缺 entity_id：state['zones'][{zone!r}] 须带区域实体 ID（或给 state['plant_id']）")
        return {"entity_id": entity_id, "temperature": float(given.get("temperature", spec.setpoint_c)),
                "setpoint": float(given.get("setpoint", spec.setpoint_c)), "mode": str(given.get("mode", "normal")),
                "power_kw": float(given.get("power_kw", 0.0)), "gain_kw": float(given.get("gain_kw", _default_gain(spec)))}

    def _pick_zone(self, event, state) -> str | None:
        """事件对象是某区域就用它；否则优先有预冷时段的区域（行政楼这类），再退到当前供冷功率最大的非硬约束区域。
        恒温区（生产车间）不排在前面：它是工艺恒温，先动办公楼。"""
        entity = str(event.get("entity_id") or "")
        named = entity.rsplit("/zone/", 1)[-1] if "/zone/" in entity else ""
        candidates = [z for z, s in self.zones.items() if not s.hard]
        if named in candidates:
            return named
        scored = []
        for zone in candidates:
            view = self._view(zone, state)
            if view["temperature"] < self.zones[zone].comfort_c[1] - 0.5:
                scheduled = self.zones[zone].cooling_hours is not None
                scored.append((scheduled, view["power_kw"], zone))
        return max(scored)[2] if scored else None

    def _proposal(self, tid, *, actions=(), cny=0.0, comfort=0.0, confidence=0.5, diagnosis="", safety=()) -> ExpertProposal:
        return ExpertProposal(expert="hvac", ticket_id=tid, actions=tuple(actions),
                              cost=ProposalCost(cny=round(cny, 2), comfort_min=int(comfort)),
                              confidence=round(confidence, 2), diagnosis=diagnosis, safety_tags=tuple(safety))


def _act(entity_id: str, command: str, start: datetime, minutes: int, readiness: str = "ready", **params) -> ProposedAction:
    return ProposedAction(entity_id=entity_id, command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)), readiness=readiness)


def _drift(spec: ZoneSpec, temperature: float, outdoor_c: float, gain_kw: float, minutes: int) -> tuple[float, int]:
    """停机 minutes 分钟后的温度与越界分钟（一阶热平衡的解析解，不含容量饱和）。"""
    equilibrium = outdoor_c + gain_kw / spec.ua_kw_per_k if spec.ua_kw_per_k > 0 else outdoor_c
    tau = spec.c_kj_per_k / spec.ua_kw_per_k if spec.ua_kw_per_k > 0 else math.inf
    comfort = 0
    for minute in range(1, max(0, minutes) + 1):
        value = equilibrium + (temperature - equilibrium) * math.exp(-minute * 60.0 / tau)
        if not spec.in_comfort(value):
            comfort += 1
    return equilibrium + (temperature - equilibrium) * math.exp(-max(0, minutes) * 60.0 / tau), comfort


def _event_minutes(event: Mapping[str, Any]) -> int:
    """事件持续分钟：payload 里的 effects/minutes，没写按 B17 的默认影响；没有持续时间返回 0。"""
    payload = event.get("payload") or {}
    specs = payload.get("effects", DEFAULT_EFFECTS.get(str(event.get("event_type", "")), []))
    return int(max((s.get("minutes", payload.get("minutes")) or 0 for s in specs), default=0))


def _default_gain(spec: ZoneSpec) -> float:
    """没有世界视图时的粗估：按设计外温差 3 K 估围护得热（内在得热未计）。"""
    return spec.ua_kw_per_k * 3.0


def _instant(value: Any) -> datetime:
    """state["at"] 可以是 datetime 或 ISO 字符串；都没有时按演示日 00:00 UTC 起算。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return datetime(2026, 7, 1, tzinfo=timezone.utc)
