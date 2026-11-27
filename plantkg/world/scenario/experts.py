"""确定性专家（B17 最简替身，登记为 AgentResultSource 的 deterministic 实现）。

供直派器（G-A 关口、独立组）在 B19～B22 专家与 B27 值守之前跑通“事件 → 专家 → apply”。规则按常理编写：
能源（储能放电削峰、照明回路切换）、电梯（分区运行、困人救援）、暖通（推迟预冷、预冷、井道通风）、
设备监测（空压泄漏、未见过的组合异常）。知识标签（随知识发布增长）决定能否判出泄漏与组合异常。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import AgentResultKind, ErrorCode, ExpertProposal, Interval, PlantkgError, ProposalCost, ProposedAction, RunBinding

DEFAULT_ROUTES: dict[str, list[str]] = {
    "elevator_congestion": ["elevator"],
    "load_spike": ["energy", "hvac"],
    "sim_limit": ["energy", "hvac"],
    "pv_drop": ["energy"],
    "heat_wave": ["hvac", "energy"],
    "lighting_fault": ["energy"],
    "compressor_pressure_drop": ["equipment"],
    "equipment_fault": ["equipment"],
    "elevator_trapped": ["elevator", "hvac"],
    "power_quality_dip": ["equipment", "energy"],
    "unknown_combo": ["equipment"],
    "capacity_shed_request": ["hvac", "energy"],
}
SAFETY_EVENTS = {"elevator_trapped"}


def _act(entity_id: str, command: str, start: datetime, minutes: int, readiness: str = "ready", depends_on=(), **params) -> ProposedAction:
    return ProposedAction(entity_id=entity_id, command=command, params=params, interval=Interval(start=start, end=start + timedelta(minutes=minutes)),
                          readiness=readiness, depends_on=tuple(depends_on))


def _proposal(expert: str, binding: RunBinding, actions=(), cny=0.0, wait=0.0, comfort=0.0, confidence=0.8, diagnosis=None, safety=()) -> ExpertProposal:
    return ExpertProposal(expert=expert, ticket_id=binding.ticket_id, actions=tuple(actions), cost=ProposalCost(cny=cny, wait_min=wait, comfort_min=comfort),
                          confidence=confidence, diagnosis=diagnosis, safety_tags=tuple(safety))


def energy(p: Mapping[str, Any], b: RunBinding) -> ExpertProposal:
    ev, t, ids = p["event"], b.trigger_at, p["ids"]
    kind = ev["event_type"]
    if kind == "lighting_fault":
        return _proposal("energy", b, [_act(ev["entity_id"], "switch_circuit", t, 1)], cny=0.0, confidence=0.9,
                         diagnosis="照明回路跳闸，切到备用回路")
    if kind in ("load_spike", "sim_limit", "pv_drop", "heat_wave", "capacity_shed_request"):
        soc, soc_min = p["state"]["soc"], p["state"]["soc_min"]
        node = ev["payload"].get("node")
        if node and node not in p["state"].get("battery_nodes", [node]):
            return _proposal("energy", b, confidence=0.7, diagnosis=f"储能接在 {'、'.join(p['state']['battery_nodes'][1:3])} 下，放电缓解不了 {node}")
        if soc <= soc_min + 0.01:
            return _proposal("energy", b, confidence=0.6, diagnosis=f"储能 SOC {soc:.2f} 已到下限，无法放电削峰")
        minutes = 60 if kind in ("pv_drop", "heat_wave") else 30
        kw = float(ev["payload"].get("discharge_kw", 400.0))
        return _proposal("energy", b, [_act(ids["battery"], "discharge", t, minutes, power_kw=kw)], cny=-kw * minutes / 60 * 0.3,
                         confidence=0.8, diagnosis=f"储能放电 {kw:.0f} kW 削峰 {minutes} 分钟（放到下限为止）")
    if kind == "power_quality_dip":
        return _proposal("energy", b, confidence=0.6, diagnosis="判为电网侧电压暂降，空压机欠压保护动作，无需检修")
    return _proposal("energy", b, confidence=0.5, diagnosis="不属能源领域")


def elevator(p: Mapping[str, Any], b: RunBinding) -> ExpertProposal:
    ev, t, ids = p["event"], b.trigger_at, p["ids"]
    kind = ev["event_type"]
    if kind == "elevator_congestion":
        group = ev["entity_id"] or ids["admin_low"]
        return _proposal("elevator", b, [_act(group, "set_mode", t, 45, mode="zone")], cny=0.5, confidence=0.85,
                         diagnosis="早高峰上行拥堵，切分区运行")
    if kind == "elevator_trapped":
        car = ev["entity_id"]
        ready = "ready" if p.get("confirmed") else "needs_human"
        return _proposal("elevator", b, [_act(car, "rescue", t, 1, ready), _act(ids["freight"], "set_mode", t, 120, ready, mode="takeover")],
                         confidence=0.95, diagnosis="轿厢停在两层之间且有人，救援须人工确认；其他货梯接管生产任务",
                         safety=("people_in_car",))
    return _proposal("elevator", b, confidence=0.5, diagnosis="不属电梯领域")


def hvac(p: Mapping[str, Any], b: RunBinding) -> ExpertProposal:
    ev, t, ids = p["event"], b.trigger_at, p["ids"]
    kind = ev["event_type"]
    if kind in ("load_spike", "sim_limit"):
        if p["state"].get("admin_temp", 0.0) >= 26.0:
            return _proposal("hvac", b, confidence=0.7, diagnosis=f"行政楼已 {p['state']['admin_temp']:.1f} ℃，不能再推迟供冷")
        return _proposal("hvac", b, [_act(ids["zone:admin"], "set_mode", t, 15, mode="off")], cny=-5.0, comfort=8.0, confidence=0.75,
                         diagnosis="行政楼推迟预冷 15 分钟让出容量")
    if kind == "heat_wave":
        return _proposal("hvac", b, [_act(ids["zone:admin"], "set_mode", t, 60, mode="precool")], cny=12.0, confidence=0.7,
                         diagnosis="高温来临前行政楼预冷 1 小时，错开峰段")
    if kind == "capacity_shed_request":  # 只看功率不看区域标记：洁净车间也想停，由直派器的硬约束拦下
        return _proposal("hvac", b, [_act(ids["zone:cleanroom"], "set_mode", t, 30, mode="off"),
                                     _act(ids["zone:admin"], "set_mode", t, 30, mode="off")], cny=-20.0, comfort=30.0, confidence=0.7,
                         diagnosis="削峰：暂停洁净车间与行政楼供冷 30 分钟")
    if kind == "elevator_trapped":
        ready = "ready" if p.get("confirmed") else "needs_human"
        return _proposal("hvac", b, [_act(ev["entity_id"], "ventilate", t, 60, ready, kw=3.0)], confidence=0.9, diagnosis="给井道通风")
    return _proposal("hvac", b, confidence=0.5, diagnosis="不属暖通领域")


def equipment(p: Mapping[str, Any], b: RunBinding) -> ExpertProposal:
    ev, t, ids, know = p["event"], b.trigger_at, p["ids"], set(p["knowledge"])
    kind = ev["event_type"]
    if kind == "compressor_pressure_drop":
        if "compressor_leak_rule" in know:
            return ExpertProposal(
                expert="equipment", ticket_id=b.ticket_id, cost=ProposalCost(cny=-3.0), confidence=0.9,
                actions=(_act(ids["air_system"], "isolate_leak", t, 1), _act(ev["entity_id"], "repair", t + timedelta(hours=2), 1, "needs_human")),
                diagnosis="压力下降速率超过阈值，按《空压机运行维护手册》判为泄漏；隔离泄漏段并开维修工单",
                evidence_refs=("knowledge:compressor_leak_rule",))
        return _proposal("equipment", b, [_act(ids["air_system"], "isolate_leak", t, 1, "needs_diagnosis", ("leak_check",))],
                         confidence=0.45, diagnosis="压力异常，缺泄漏判别规则，待诊断")
    if kind == "unknown_combo":
        if "combo_rule" in know:
            return _proposal("equipment", b, [_act(ids["LT-P"], "repair", t, 1), _act(ids["AC-1"], "repair", t, 1)], cny=0.0,
                             confidence=0.85, diagnosis="照明与空压同时异常：同一生产总配电柜接触不良，按纠错知识复位")
        return _proposal("equipment", b, confidence=0.1, diagnosis="未见过的组合异常，无法判断")
    if kind == "equipment_fault":
        return _proposal("equipment", b, [_act(ev["entity_id"], "repair", t, 1, "needs_human")], confidence=0.8, diagnosis="设备故障，开维修工单")
    if kind == "power_quality_dip":
        return _proposal("equipment", b, [_act(ev["entity_id"], "repair", t, 1, "needs_human")], confidence=0.6,
                         diagnosis="判为空压机电机绕组故障，建议停机检修")
    return _proposal("equipment", b, confidence=0.5, diagnosis="不属设备监测领域")


EXPERTS = {"energy": energy, "elevator": elevator, "hvac": hvac, "equipment": equipment}


class DeterministicExperts:
    """AgentResultSource（deterministic）：payload["expert"] 选专家，返回 {"proposal": ExpertProposal JSON}。"""

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if kind != AgentResultKind.expert.value and kind != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"确定性专家只实现 expert，不实现 {kind}")
        fn = EXPERTS.get(payload["expert"])
        if fn is None:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"没有专家 {payload['expert']}")
        return {"proposal": fn(payload, binding).model_dump(mode="json")}
