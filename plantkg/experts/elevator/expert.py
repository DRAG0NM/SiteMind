"""电梯专家（B20）：事件 + 状态 → ExpertProposal。

提案里的动作按“模式 / 分区 / 停用启用轿厢”三类工具给出，不直接控制电梯；困人救援标
`needs_human`，由值班人员确认后再执行。代价与候梯数字来自仿真或传入状态，不由模型编造。
"""
from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.core.contracts import ExpertProposal, Interval, ProposalCost, ProposedAction

from plantkg.world.sim.elevator import (
    ElevatorConfig,
    FreightStrategy,
    FreightTask,
    PassengerStrategy,
    build_demand,
    run_freight_sim,
    run_passenger_sim,
)

UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
OFF_PEAK_MINUTES = 240  # 低谷节能动作的执行时段
SAFETY_TAGS_TRAPPED = ("people_in_car", "safety")
PRICE_CNY_PER_KWH = 0.7  # configured：状态里没有分时电价时的折算电价


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _instant(event: Mapping[str, Any]) -> datetime:
    value = event.get("at") or event.get("trigger_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime(2026, 7, 1, 0, 5, tzinfo=UTC)  # synthetic：没有绑定时刻时的演示时刻（08:05 当地）


def _act(entity_id: str, command: str, start: datetime, minutes: int, readiness: str = "ready", **params: Any) -> ProposedAction:
    return ProposedAction(entity_id=entity_id, command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)), readiness=readiness)


class ElevatorExpert:
    """电梯专家的确定性提案。状态里带 metrics（仿真结果）与 confirmed（困人是否已人工确认）。"""

    expert = "elevator"

    def __init__(self, config: ElevatorConfig) -> None:
        self.config = config
        self._peak: dict[str, float] | None = None

    # ---- 入口
    def propose(self, event: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> ExpertProposal:
        state = state or {}
        kind = str(event.get("event_type") or event.get("type") or "")
        if kind == "elevator_congestion":
            return self._congestion(event, state)
        if kind == "elevator_trapped":
            return self._trapped(event, state)
        if kind in ("freight_priority", "material_task", "elevator_freight"):
            return self._freight(event, state)
        if kind in ("elevator_energy", "elevator_off_peak"):
            return self._off_peak(event, state)
        return self._proposal(event, confidence=0.5, diagnosis="不属电梯领域")

    def synthetic_context(self, focus: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """B27 的事件单上下文还没接入时的替身：从关注词猜一个事件，数字由一次早高峰仿真给出。"""
        text = focus or ""
        if any(word in text for word in ("困人", "救援", "trapped")):
            event = {"event_type": "elevator_trapped", "entity_id": self.config.freight_cars[1 if len(self.config.freight_cars) > 1 else 0].code,
                     "severity": 5, "summary": "2 号货梯停在两层之间，轿厢内有人"}
        elif any(word in text for word in ("货梯", "物料", "freight", "搬运")):
            event = {"event_type": "freight_priority", "entity_id": self.config.group_id("freight"), "severity": 3,
                     "summary": "物料任务排队", "payload": {"priority": "urgent"}}
        elif any(word in text for word in ("低谷", "节能", "停梯", "off")):
            event = {"event_type": "elevator_energy", "entity_id": self.config.group_id("admin_low"), "severity": 2,
                     "summary": "低谷停部分梯节能"}
        else:
            event = {"event_type": "elevator_congestion", "entity_id": self.config.group_id("admin_low"), "severity": 3,
                     "summary": "早高峰上行拥堵", "payload": {"effects": [{"kind": "surge", "factor": 3.0, "minutes": 40}]}}
        return event, {"confirmed": False, "price_period": "peak", "metrics": self.peak_metrics()}

    def peak_metrics(self) -> dict[str, float]:
        """早高峰 30 分钟、800 人次的固定派梯与分区两次仿真（同一需求、同一种子），结果确定，算一次后复用。"""
        if self._peak is None:
            demand = build_demand(self.config, "peak_up", minutes=30, staff=800)
            fixed = run_passenger_sim(self.config, demand, PassengerStrategy("fixed", "peak_up", return_floor=1))
            zone = run_passenger_sim(self.config, demand, PassengerStrategy("zone", "peak_up", return_floor=1))
            self._peak = congestion_metrics(fixed, zone)
        return self._peak

    # ---- 人梯拥堵
    def _congestion(self, event: Mapping[str, Any], state: Mapping[str, Any]) -> ExpertProposal:
        metrics = _metrics(state)
        avg, queue, surge = _number(metrics.get("avg_wait_s")), _number(metrics.get("queue")), _surge(event)
        at = _instant(event)
        rule = state.get("published_elevator_rule")
        if rule:
            condition = rule.get("condition") or {}
            span = condition.get("time_range") or []
            local_time = at.astimezone(LOCAL_TZ).strftime("%H:%M")
            if len(span) != 2 or not span[0] <= local_time < span[1]:
                rule = None
        if rule:
            zones = (rule.get("condition") or {}).get("zones")
            if zones:
                configured = {"low": list(self.config.low_floors), "high": list(self.config.high_floors)}
                expected = {name: list(range(bounds[0], bounds[1] + 1)) for name, bounds in zones.items()}
                if expected != configured:
                    return self._proposal(event, confidence=0.5,
                        diagnosis="已发布楼层分区与电梯配置不一致，暂停自动分区并请人工核对",
                        evidence=(f"rule:{rule['rule_id']}",))
            wait_limit = float(rule["bounds"]["upper"])
            queue_limit = int(rule["condition"]["queue_threshold"])
            high_queue, high_wait = _number(metrics.get("high_queue")), _number(metrics.get("high_avg_wait_s"))
            primary_trigger = avg > wait_limit or queue > queue_limit
            high_trigger = high_queue > queue_limit or high_wait > wait_limit
            if not primary_trigger and not high_trigger:
                return self._proposal(event, confidence=0.65, diagnosis=f"平均候梯 {avg:.0f} s、排队 {queue:.0f} 人，未超过已发布规则，维持固定派梯",
                                      evidence=(f"rule:{rule['rule_id']}",))
        else:
            primary_trigger = not (avg < 60 and queue < 20 and surge < 2.0)
            high_trigger = _number(metrics.get("high_queue")) >= 20 or _number(metrics.get("high_avg_wait_s")) >= 60
            if not primary_trigger:
                return self._proposal(event, confidence=0.65, diagnosis=f"平均候梯 {avg:.0f} s、排队 {queue:.0f} 人，维持固定派梯")
        group = self._group(event, "admin_low")
        zone = "high" if group == self.config.group_id("admin_high") else "low"
        cars = len(self.config.cars(zone))
        actions = ([_act(group, "set_mode", at, 45, mode="zone"), _act(group, "set_active", at, 45, cars=cars)]
                   if primary_trigger else [])
        high_group = self.config.group_id("admin_high")
        if high_trigger and (high_group != group or not primary_trigger):
            actions.append(_act(high_group, "set_mode", at, 45, mode="zone"))
            actions.append(_act(high_group, "set_active", at, 45, cars=len(self.config.cars("high"))))
        if not primary_trigger:
            zone, cars = "high", len(self.config.cars("high"))
        label = "高区" if zone == "high" else "低区"
        return self._proposal(
            event, actions=actions, cny=self._cny(state, _number(metrics.get("zone_trip_kwh")) - _number(metrics.get("trip_kwh"))),
            confidence=0.85,
            diagnosis=f"早高峰上行 {avg:.0f} s 平均候梯、排队 {queue:.0f} 人；{label}切分区运行并全开 {cars} 台梯",
            evidence=(f"rule:{rule['rule_id']}",) if rule else ("knowledge:elevator_peak_dispatch",))

    # ---- 困人
    def _trapped(self, event: Mapping[str, Any], state: Mapping[str, Any]) -> ExpertProposal:
        at = _instant(event)
        car = self._eid(event.get("entity_id"))
        freight_group = self.config.group_id("freight")
        ready = "ready" if state.get("confirmed") else "needs_human"
        actions = [_act(car, "rescue", at, 1, ready)]
        diagnosis = "轿厢停在两层之间且有人：该梯停止运行并通知持证维保，救援须人工确认"
        if any(spec.entity_id == car for spec in self.config.cars("freight")):
            actions += [_act(freight_group, "set_mode", at, 120, ready, mode="takeover", takeover_of=car),
                        _act(freight_group, "set_active", at, 120, ready, cars=len(self.config.cars("freight")))]
            diagnosis += "；其余货梯接管该梯的生产任务，避免产线断料"
        return self._proposal(
            event, actions=actions, confidence=0.95, safety=SAFETY_TAGS_TRAPPED, diagnosis=diagnosis,
            evidence=("knowledge:elevator_trapped_rescue",))

    # ---- 货梯
    def _freight(self, event: Mapping[str, Any], state: Mapping[str, Any]) -> ExpertProposal:
        at = _instant(event)
        payload = dict(event.get("payload") or {})
        urgent = bool(payload.get("urgent")) or str(payload.get("priority", "")).lower() in ("urgent", "高", "紧急")
        group = self.config.group_id("freight")
        cars = len(self.config.cars("freight"))
        if urgent:
            return self._proposal(
                event, actions=[_act(group, "set_mode", at, 60, mode="priority"), _act(group, "set_active", at, 60, cars=cars)],
                cny=self._cny(state, self._freight_delta_kwh("priority")), confidence=0.85, diagnosis="紧急物料优先，货梯切优先级排队并全开；人梯高峰期间也让行",
                evidence=("knowledge:elevator_freight_priority",))
        price_period = str(state.get("price_period") or _metrics(state).get("price_period") or "flat")
        if price_period == "peak":
            return self._proposal(
                event, actions=[_act(group, "set_mode", at, 60, mode="batch"), _act(group, "set_active", at, 60, cars=cars)],
                cny=self._cny(state, self._freight_delta_kwh("priority", batch=True)), confidence=0.8, diagnosis="峰电时段把普通物料任务合并批次，少跑往返、避开人梯高峰",
                evidence=("knowledge:elevator_freight_priority",))
        return self._proposal(
            event, actions=[_act(group, "set_mode", at, 60, mode="priority")],
            cny=self._cny(state, self._freight_delta_kwh("priority")), confidence=0.75,
            diagnosis="货梯按生产任务优先级排队，普通任务让行人梯高峰",
            evidence=("knowledge:elevator_freight_priority",))

    # ---- 低谷节能
    def _off_peak(self, event: Mapping[str, Any], state: Mapping[str, Any]) -> ExpertProposal:
        """每区比当前少开 1 台。当前台数取状态里的 active_cars（组实体或 low/high → 台数），没有就按剧本默认：当地 06:00 前每区 1 台，其余全开。"""
        at = _instant(event)
        reported = state.get("active_cars") or {}
        early = at.astimezone(LOCAL_TZ).hour < 6
        actions, now_total, target_total = [], 0, 0
        for zone in ("low", "high"):
            group, total = self.config.group_id(zone), len(self.config.cars(zone))
            now = int(_number(reported.get(group, reported.get(zone)), max(1, total // 2) if early else total))
            target = min(now, max(1, total - 1))
            now_total, target_total = now_total + now, target_total + target
            if target < now:
                actions += [_act(group, "set_active", at, OFF_PEAK_MINUTES, cars=target),
                            _act(group, "set_mode", at, OFF_PEAK_MINUTES, mode="off_peak")]
        if not actions:
            return self._proposal(event, confidence=0.8, diagnosis=f"当前已只开 {now_total} 台人梯，没有可再停的梯，维持现状")
        demand = build_demand(self.config, "off_peak", minutes=60, staff=800, seed=17)
        full = run_passenger_sim(self.config, demand, PassengerStrategy("zone", "off_peak", active_cars=now_total))
        part = run_passenger_sim(self.config, demand, PassengerStrategy("zone", "off_peak", active_cars=target_total))
        cny = self._cny(state, (part.energy_kwh - full.energy_kwh) * OFF_PEAK_MINUTES / 60)  # 仿真 1 小时的差额放大到执行时段
        return self._proposal(
            event, actions=actions, cny=cny, confidence=0.8,
            diagnosis=f"低谷人梯从 {now_total} 台减到 {target_total} 台；仿真按 {OFF_PEAK_MINUTES // 60} 小时执行时段约省 {-cny:.1f} 元电费",
            evidence=("knowledge:elevator_peak_dispatch",))

    # ---- 辅助
    def _proposal(self, event: Mapping[str, Any], *, actions: list[ProposedAction] | tuple = (), cny: float = 0.0,
                  confidence: float, diagnosis: str, safety: tuple[str, ...] = (), evidence: tuple[str, ...] = ()) -> ExpertProposal:
        ticket = str(event.get("ticket_id") or f"ticket://elevator-{event.get('id') or 'demo'}")
        return ExpertProposal(expert=self.expert, ticket_id=ticket, actions=tuple(actions), cost=ProposalCost(cny=cny),
                              confidence=confidence, diagnosis=diagnosis, safety_tags=safety, evidence_refs=evidence)

    @staticmethod
    def _cny(state: Mapping[str, Any], delta_kwh: float) -> float:
        """仿真能耗差 × 电价；状态没给电价就用 configured 折算价。"""
        price = _number(state.get("price_cny_per_kwh") or _metrics(state).get("price_cny_per_kwh"), PRICE_CNY_PER_KWH)
        return round(delta_kwh * price, 2)

    def _freight_delta_kwh(self, name: str, batch: bool = False) -> float:
        """货梯策略相对先来先到的能耗差（kWh，synthetic 任务样本：1 小时 60 件，其中 6 件紧急）。"""
        rng = random.Random(11)
        tasks = tuple(FreightTask(f"T{i}", rng.uniform(0, 3600), 1, rng.choice((2, 3, 4)), i % 10 == 0) for i in range(60))
        common = {"peak_price_windows": ((0.0, 3600.0),), "horizon_s": 2 * 3600}
        base = run_freight_sim(self.config, tasks, FreightStrategy("fifo"), **common)
        new = run_freight_sim(self.config, tasks, FreightStrategy(name, batch_at_peak=batch), **common)
        return new.energy_kwh - base.energy_kwh

    def _group(self, event: Mapping[str, Any], default: str) -> str:
        entity = self._eid(event.get("entity_id"))
        for name in ("admin_low", "admin_high", "freight"):
            if entity == self.config.group_id(name):
                return entity
        if entity in {c.entity_id for c in self.config.cars()}:
            car = next(c for c in self.config.cars() if c.entity_id == entity)
            return self.config.group_id("freight" if car.zone == "freight" else "admin_low" if car.zone == "low" else "admin_high")
        return self.config.group_id(default)

    def _eid(self, value: Any) -> str:
        try:
            return self.config.eid(str(value)) if value is not None else self.config.group_id("admin_low")
        except ValueError:
            return str(value)


def congestion_metrics(current: Any, zone: Any) -> dict[str, float]:
    """拥堵判断用当前模式（固定派梯）的仿真结果；分区的行程能耗只用来估提案代价。"""
    return {"avg_wait_s": current.avg_wait_s, "max_wait_s": current.max_wait_s, "queue": current.avg_queue,
            "congestion_min": current.congestion_min, "energy_kwh": current.energy_kwh,
            "trip_kwh": current.trip_kwh, "zone_trip_kwh": zone.trip_kwh}


def _metrics(state: Mapping[str, Any]) -> Mapping[str, Any]:
    return state.get("metrics") or {}


def _surge(event: Mapping[str, Any]) -> float:
    payload = event.get("payload") or {}
    effects = payload.get("effects") or []
    factor = 1.0
    for effect in effects:
        if isinstance(effect, Mapping) and effect.get("kind") == "surge":
            factor = max(factor, _number(effect.get("factor"), 1.0))
    return factor


__all__ = ["ElevatorExpert", "SAFETY_TAGS_TRAPPED", "congestion_metrics"]
