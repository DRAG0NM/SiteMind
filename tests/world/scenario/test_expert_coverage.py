"""B22 真实设备专家覆盖契约（#184 / #134）。

真实 `EquipmentExpert` 经 `SCENARIO_HOOK` 整体替换 B17 的 `equipment` 占位后，必须对 `DEFAULT_ROUTES` 里
所有派给它的事件类型、以及 8 个极端剧本里的事件给出“动作或显式诊断”，不能落回
“证据不足或事件不属设备监测领域，不处置”的兜底。测试都经默认装配（`default_registry` → ScenarioRunner → 直派器）跑剧本。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plantkg.core.ports import AgentResultSource
from plantkg.experts.equipment.source import EquipmentResultSource
from plantkg.world.scenario import load_scenario
from plantkg.world.scenario.experts import DEFAULT_ROUTES
from plantkg.world.scenario.runner import default_registry

from .conftest import local, scenario

ROOT = Path(__file__).resolve().parents[3]
EXTREME = sorted((ROOT / "configs/scenarios").glob("extreme_*.yaml"))
# 各专家“这事不归我管/证据不足”的兜底说法：出现它就算没有给出显式诊断。
FALLBACK_MARKERS = ("证据不足", "不属设备监测领域", "不属能源领域", "不属暖通领域", "不属电梯领域")
EVENT_ENTITY = {
    "elevator_congestion": "elevator_group:admin_low",
    "load_spike": "AC-1",
    "sim_limit": "AC-1",
    "pv_drop": "zone:park",
    "heat_wave": "zone:admin",
    "lighting_fault": "LT-A03",
    "compressor_pressure_drop": "AC-2",
    "equipment_fault": "AC-1",
    "elevator_trapped": "FE-2",
    "power_quality_dip": "AC-3",
    "unknown_combo": "LT-P",
    "capacity_shed_request": "zone:cleanroom",
}


def _registry():
    """默认装配，并确认 equipment 是真实 B22 结果源（不是 B17 占位）。"""
    registry = default_registry()
    source = registry.resolve_mode(AgentResultSource, "deterministic", key="equipment")
    assert isinstance(source, EquipmentResultSource), type(source).__name__
    return registry


def _explicit(proposal) -> bool:
    """有动作，或诊断说清了本领域判断（不是“证据不足/不属本领域”的兜底）。"""
    return bool(proposal.actions) or (bool(proposal.diagnosis) and not any(m in proposal.diagnosis for m in FALLBACK_MARKERS))


def _ticket_of(trace, event_id: str) -> str:
    return next(ticket_id for ticket_id, rec in trace.tickets.items() if event_id in rec["event_ids"])


@pytest.mark.parametrize("event_type", sorted(DEFAULT_ROUTES), ids=lambda name: name)
def test_every_default_route_event_gets_action_or_explicit_diagnosis(make, event_type):
    """DEFAULT_ROUTES 的每个事件类型：派到的每位真实专家都要给动作或显式诊断。"""
    events = [{"at": "09:00", "type": event_type, "entity_id": EVENT_ENTITY[event_type], "severity": 3, "summary": f"{event_type} 契约事件"}]
    trace = make(scenario(events=events), registry=_registry()).run(until=local("09:30"))
    proposals = {proposal.expert: proposal for proposal in trace.proposals}
    assert len(proposals) == len(DEFAULT_ROUTES[event_type]), [p.expert for p in trace.proposals]
    for expert in DEFAULT_ROUTES[event_type]:
        assert expert in proposals, f"{event_type} 没派到 {expert}"
        assert _explicit(proposals[expert]), (event_type, expert, proposals[expert].confidence, proposals[expert].diagnosis)


@pytest.mark.parametrize("path", EXTREME, ids=lambda path: path.stem)
def test_extreme_scripts_get_action_or_explicit_diagnosis(make, path):
    """8 个极端剧本里的每条提案都要有动作或显式诊断。"""
    trace = make(load_scenario(path), registry=_registry()).run()
    assert trace.proposals, path.stem
    bad = [(p.expert, p.confidence, p.diagnosis) for p in trace.proposals if not _explicit(p)]
    assert not bad, (path.stem, bad)


def test_unseen_combo_next_day_restores_actions(make):
    """extreme_unseen_combo：纠错发布 combo_rule 后，次日同类组合异常恢复动作。"""
    trace = make(load_scenario(ROOT / "configs/scenarios/extreme_unseen_combo.yaml"), registry=_registry()).run()
    next_day = [e for e in trace.events if e.event_type == "unknown_combo" and e.at >= local("16:00", day=2)]
    assert len(next_day) == 1
    ticket_id = _ticket_of(trace, next_day[0].event_id)
    actions = [a for a in trace.actions if a["source"].get("ticket_id") == ticket_id]
    assert len(actions) >= 1 and {a["action"]["command"] for a in actions} == {"repair"}


def test_factory_demo_fourth_act_next_day_restores_actions(make):
    """factory_demo_day：第四幕纠错后，次日第四幕的同类组合异常恢复动作。"""
    trace = make(load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml"), registry=_registry()).run()
    ticket_id = _ticket_of(trace, "day2-combo")
    actions = [a for a in trace.actions if a["source"].get("ticket_id") == ticket_id]
    assert len(actions) >= 1 and {a["action"]["command"] for a in actions} == {"repair"}


def test_root_cause_conflict_presents_equipment_and_energy_disagreement(make):
    """extreme_root_cause_conflict：设备专家的诊断置信度达呈现阈值，与能源的分歧进 conflict note。"""
    trace = make(load_scenario(ROOT / "configs/scenarios/extreme_root_cause_conflict.yaml"), registry=_registry()).run()
    equipment = next(p for p in trace.proposals if p.expert == "equipment")
    assert not equipment.actions and equipment.diagnosis and equipment.confidence >= 0.5
    conflicts = [n for n in trace.notes if n["kind"] == "conflict"]
    assert conflicts and any(any(d.startswith("equipment：") for d in n["diagnoses"]) and
                             any(d.startswith("energy：") for d in n["diagnoses"]) for n in conflicts)
