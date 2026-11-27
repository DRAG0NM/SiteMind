"""B20 电梯专家提案：困人安全标记、协同接管、分区与货梯策略。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from plantkg.experts.elevator import ElevatorExpert
from plantkg.world.site.model import SiteModel
from plantkg.world.sim.elevator import ElevatorConfig

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, 6, 20)  # 当地 14:20


@pytest.fixture(scope="module")
def expert() -> ElevatorExpert:
    site = SiteModel.load(ROOT / "configs/sites/factory/site.yaml")
    return ElevatorExpert(ElevatorConfig.from_site(site))


def test_trapped_event_has_safety_tags_and_human_gate(expert):
    event = {"event_type": "elevator_trapped", "entity_id": "FE-2", "at": AT, "id": "trap", "summary": "2 号货梯困人"}
    proposal = expert.propose(event, {"confirmed": False, "metrics": {}})
    assert proposal.expert == "elevator"
    assert "people_in_car" in proposal.safety_tags and "safety" in proposal.safety_tags
    assert proposal.confidence >= 0.9
    rescue = next(action for action in proposal.actions if action.command == "rescue")
    assert rescue.readiness == "needs_human"
    takeover = next(action for action in proposal.actions if action.command == "set_mode")
    assert takeover.params["mode"] == "takeover"
    assert takeover.params["takeover_of"] == "entity://demo-electronics/freight_elevator/FE-2"
    assert "knowledge:elevator_trapped_rescue" in proposal.evidence_refs


def test_trapped_event_is_ready_after_human_confirmation(expert):
    event = {"event_type": "elevator_trapped", "entity_id": "FE-2", "at": AT, "id": "trap"}
    proposal = expert.propose(event, {"confirmed": True, "metrics": {}})
    assert all(action.readiness == "ready" for action in proposal.actions)


def test_congestion_proposes_zone_mode(expert):
    event = {"event_type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "at": AT,
             "payload": {"effects": [{"kind": "surge", "factor": 3.0, "minutes": 40}]}}
    proposal = expert.propose(event, {"metrics": {"avg_wait_s": 120.0, "queue": 30.0}})
    mode = next(action for action in proposal.actions if action.command == "set_mode")
    assert mode.params["mode"] == "zone"
    assert "knowledge:elevator_peak_dispatch" in proposal.evidence_refs


def test_off_peak_stops_two_cars(expert):
    event = {"event_type": "elevator_energy", "entity_id": "elevator_group:admin_low", "at": AT}
    proposal = expert.propose(event, {})
    assert proposal.cost.cny < 0
    active = [action for action in proposal.actions if action.command == "set_active"]
    assert active and all(action.params["cars"] == 2 for action in active)


def test_freight_urgent_uses_priority(expert):
    event = {"event_type": "freight_priority", "entity_id": "elevator_group:freight", "at": AT,
             "payload": {"urgent": True}}
    proposal = expert.propose(event, {"price_period": "peak"})
    mode = next(action for action in proposal.actions if action.command == "set_mode")
    assert mode.params["mode"] == "priority"
    assert proposal.cost.cny <= 0  # 优先级不合批，趟数同先来先到，能耗差为 0


def test_unknown_event_returns_no_action(expert):
    proposal = expert.propose({"event_type": "hvac_fault", "entity_id": "AHU-A01", "at": AT}, {})
    assert proposal.actions == () and proposal.confidence <= 0.6


def _off_peak_run(expert, at, state):
    from datetime import timedelta
    from plantkg.core.contracts import Interval, SimAction
    from plantkg.world.sim.base import MinuteInputs, WorldState
    from plantkg.world.sim.elevator import ScenarioElevatorSim

    site = SiteModel.load(ROOT / "configs/sites/factory/site.yaml")
    proposal = expert.propose({"event_type": "elevator_energy", "entity_id": "elevator_group:admin_low", "at": at}, state)
    actions = [SimAction(entity_id=a.entity_id, command=a.command, params=a.params, interval=Interval(start=a.interval.start, end=a.interval.end))
               for a in proposal.actions]

    def kwh(sim_actions):
        sim = ScenarioElevatorSim(site)
        world = WorldState(at=at, devices={}, rng=None)
        sim.init(world)
        total = 0.0
        for _ in range(240):
            world.inputs = MinuteInputs(at=world.at, equipment_kw={}, pv_kw=0.0, price_cny_per_kwh=0.6, outdoor_c=30.0, occupancy={})
            world.power_kw = {}
            sim.step(world, sim_actions, timedelta(minutes=1))
            total += sum(world.power_kw.values()) / 60
            world.at += timedelta(minutes=1)
        return total

    return proposal, kwh([]), kwh(actions)


def test_off_peak_at_night_does_not_add_cars(expert):
    from datetime import timezone
    at = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)  # 当地 02:00，剧本默认每区已只开 1 台
    proposal, base, after = _off_peak_run(expert, at, {})
    assert not proposal.actions and proposal.cost.cny == 0
    assert after == base


def test_off_peak_by_day_saves_money_in_the_executed_window(expert):
    from datetime import timezone
    at = datetime(2026, 7, 1, 6, 20, tzinfo=timezone.utc)  # 当地 14:20，每区 3 台全开
    proposal, base, after = _off_peak_run(expert, at, {})
    assert proposal.cost.cny < 0 and after < base
    assert [a.params["cars"] for a in proposal.actions if a.command == "set_active"] == [2, 2]
    # 仿真与提案同方向；金额按 0.7 元/kWh 折算后与提案同量级（提案用 60 分钟样本放大到 4 小时）
    assert (after - base) * 0.7 < 0
