"""B20 电梯专家：人梯/货梯策略、SimPy 群控与 B17 场景适配的冒烟测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plantkg.core.contracts import Interval, SimAction
from plantkg.core.ledger import LedgerMeta
from plantkg.world.scenario.inputs import SiteInputProvider
from plantkg.world.scenario.runner import ScenarioWorld
from plantkg.world.scenario.script import load_scenario
from plantkg.world.scenario.__main__ import tariff_prices
from plantkg.world.site.model import SiteModel
from plantkg.world.sim.base import MinuteInputs, WorldState
from plantkg.world.sim.elevator import (
    ElevatorConfig,
    FreightStrategy,
    FreightTask,
    PassengerStrategy,
    ScenarioElevatorSim,
    build_demand,
    run_freight_sim,
    run_passenger_sim,
)

ROOT = Path(__file__).resolve().parents[3]
SITE = ROOT / "configs/sites/factory/site.yaml"
UTC = timezone.utc
CAR_FE2 = "entity://demo-electronics/freight_elevator/FE-2"


@pytest.fixture(scope="module")
def site() -> SiteModel:
    return SiteModel.load(SITE)


@pytest.fixture(scope="module")
def config(site: SiteModel) -> ElevatorConfig:
    return ElevatorConfig.from_site(site)


def test_zone_strategy_beats_fixed_in_morning_peak(config):
    demand = build_demand(config, "peak_up", minutes=30, staff=800)
    fixed = run_passenger_sim(config, demand, PassengerStrategy("fixed", "peak_up", return_floor=1))
    zone = run_passenger_sim(config, demand, PassengerStrategy("zone", "peak_up", return_floor=1))
    assert zone.avg_wait_s < fixed.avg_wait_s
    assert zone.served >= fixed.served
    assert zone.energy_kwh > 0 and zone.trips > 0


def test_freight_priority_protects_urgent_tasks_and_batches_at_peak(config):
    tasks = tuple(
        [FreightTask(f"N{i}", (i * 10.0) % 7200, 1, 2 + i % 3, False) for i in range(600)]
        + [FreightTask(f"U{i}", (i * 100.0) % 7200, 1, 2 + i % 3, True) for i in range(60)]
    )
    windows = {"passenger_peak_windows": ((0.0, 1800.0),), "peak_price_windows": ((3600.0, 5400.0),), "horizon_s": 4 * 3600}
    fifo = run_freight_sim(config, tasks, FreightStrategy("fifo"), **windows)
    priority = run_freight_sim(config, tasks, FreightStrategy("priority", avoid_passenger_peak=True, batch_at_peak=True), **windows)
    assert priority.urgent_avg_wait_min < fifo.urgent_avg_wait_min
    assert priority.energy_kwh < fifo.energy_kwh
    assert priority.deferred > 0 and priority.batches > 0
    assert priority.done == fifo.done == len(tasks)


def test_scenario_adapter_writes_elevator_power_and_safety_events(site):
    sim = ScenarioElevatorSim(site)
    at = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)  # 当地 10:00
    state = WorldState(at=at, devices={}, rng=None)
    sim.init(state)
    state.inputs = MinuteInputs(at=at, equipment_kw={}, pv_kw=0.0, price_cny_per_kwh=0.6, outdoor_c=30.0,
                                occupancy={"trips:passenger_elevator": 20.0, "tasks:material_handling": 2.0})
    state.power_kw = {}
    sim.step(state, [], timedelta(minutes=1))
    assert any(code.startswith("PE-") and kw > 0 for code, kw in state.power_kw.items())
    assert state.sims["elevator"]["wait_person_min"] > 0

    state.effects.append({"kind": "trapped", "entity_id": CAR_FE2, "since": at.isoformat(), "until": None})
    state.power_kw = {}
    sim.step(state, [], timedelta(minutes=1))
    safety = state.sims["elevator"]["safety_events"]
    assert safety and safety[-1]["kind"] == "trapped" and safety[-1]["severity"] == 5

    rescue = SimAction(entity_id=CAR_FE2, command="rescue", params={}, interval=Interval(start=at, end=at + timedelta(minutes=1)))
    state.power_kw = {}
    sim.step(state, [rescue], timedelta(minutes=1))
    assert state.devices[CAR_FE2]["trapped"] is False
    assert safety[-1]["kind"] == "rescued"


def test_demo_scenario_loads_and_morning_peak_matches():
    scene = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    prices, note = tariff_prices(scene)
    inputs = SiteInputProvider(scene, prices, price_note=note)
    assert inputs.site.plant_id == "demo-electronics"
    assert scene.events[0].type == "elevator_congestion"


def test_scenario_adapter_energy_enters_the_minute_ledger():
    scene = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    prices, note = tariff_prices(scene)
    inputs = SiteInputProvider(scene, prices, price_note=note)
    world = ScenarioWorld.build(scene, inputs, [ScenarioElevatorSim(inputs.site)])
    state, energy = world.initial_state()
    meta = LedgerMeta(ledger_id="elevator-test", kind="execution", run_id="run-elevator", plan_id="scenario:run-elevator",
                      plan_revision=0, version_manifest_id="vm-test", source_refs=(inputs.provider_id,))
    record, _ = world.advance(state, energy, meta)
    assert record.balanced
    assert record.entry.load_breakdown["elevator"] > 0


def test_takeover_restores_freight_capacity_and_zone_mode_only_boosts_when_set(site):
    at = datetime(2026, 7, 1, 1, 0, tzinfo=UTC)  # 当地 09:00，非早高峰

    def run(actions, trapped):
        sim = ScenarioElevatorSim(site)
        state = WorldState(at=at, devices={}, rng=None)
        sim.init(state)
        state.inputs = MinuteInputs(at=at, equipment_kw={}, pv_kw=0.0, price_cny_per_kwh=0.6, outdoor_c=30.0,
                                    occupancy={"trips:passenger_elevator": 40.0, "tasks:material_handling": 1.0})
        if trapped:
            state.effects.append({"kind": "trapped", "entity_id": CAR_FE2, "since": at.isoformat(), "until": None})
            state.devices[CAR_FE2]["fault"] = True
        state.power_kw = {}
        for _ in range(30):
            sim.step(state, actions, timedelta(minutes=1))
            state.at += timedelta(minutes=1)
        return state

    interval = Interval(start=at, end=at + timedelta(hours=2))
    freight = ScenarioElevatorSim(site).config.group_id("freight")
    low = ScenarioElevatorSim(site).config.group_id("admin_low")
    take = SimAction(entity_id=freight, command="set_mode", params={"mode": "takeover"}, interval=interval)
    assert run([take], True).devices[freight]["queue"] < run([], True).devices[freight]["queue"]
    zone = SimAction(entity_id=low, command="set_mode", params={"mode": "zone"}, interval=interval)
    assert run([zone], False).devices[low]["queue"] < run([], False).devices[low]["queue"]


def test_unserved_passengers_count_as_waiting_to_horizon(config):
    demand = build_demand(config, "peak_up", minutes=30, staff=800)
    fixed = run_passenger_sim(config, demand, PassengerStrategy("fixed", "peak_up", return_floor=1))
    zone = run_passenger_sim(config, demand, PassengerStrategy("zone", "peak_up", return_floor=1))
    assert fixed.unserved == fixed.arrivals - fixed.served > 0
    assert zone.unserved < fixed.unserved and zone.avg_wait_s < fixed.avg_wait_s
    assert fixed.energy_kwh == pytest.approx(fixed.trip_kwh + fixed.standby_kwh, abs=0.01)


def test_default_assembly_uses_real_elevator_expert_and_scenario_sim(tmp_path):
    from plantkg.world.scenario import ScenarioRunner
    from plantkg.world.scenario.__main__ import build_world, open_db

    world = build_world(load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml"))
    assert [type(s).__name__ for s in world.simulators].count("ScenarioElevatorSim") == 1
    assert not any(type(s).__name__ == "ElevatorSim" for s in world.simulators)
    db = open_db(tmp_path / "s.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db).run(until=datetime(2026, 7, 1, 7, 0, tzinfo=UTC))  # 当地 15:00，含 14:20 困人
    finally:
        db.close()
    trapped = [p for p in trace.proposals if p.expert == "elevator" and p.safety_tags]
    commands = [(x.command, x.params) for p in trapped for x in p.actions]
    assert any(c == "set_active" for c, _ in commands) and any(params.get("takeover_of") for _, params in commands)
    assert trace.ledger and max(m.entry.load_breakdown["elevator"] for m in trace.ledger) > 0


def _run_minutes(site, at, minutes, occupancy_for, actions=()):
    """跑 N 个分钟子步，返回 (状态, 各设备功率积分 kWh)。"""
    sim = ScenarioElevatorSim(site)
    state = WorldState(at=at, devices={}, rng=None)
    sim.init(state)
    kwh = 0.0
    for minute in range(minutes):
        state.inputs = MinuteInputs(at=state.at, equipment_kw={}, pv_kw=0.0, price_cny_per_kwh=0.6, outdoor_c=30.0,
                                    occupancy=occupancy_for(minute))
        state.power_kw = {}
        sim.step(state, actions, timedelta(minutes=1))
        kwh += sum(state.power_kw.values()) / 60
        state.at += timedelta(minutes=1)
    return state, kwh


def test_power_integral_equals_running_plus_standby_energy(site):
    at = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)  # 当地 10:00，人梯、货梯全开
    config = ScenarioElevatorSim(site).config
    busy = {"trips:passenger_elevator": 20.0, "tasks:material_handling": 2.0}
    state, kwh = _run_minutes(site, at, 1, lambda _m: busy)
    standby = len(config.cars()) * config.standby_kw / 60  # 无停梯：每台待机 1 分钟
    running = state.sims["elevator"]["energy_kwh"]
    assert running > 0.5
    assert kwh == pytest.approx(running + standby, abs=1e-6)


def _batch_actions(site, at):
    from plantkg.experts.elevator import ElevatorExpert
    expert = ElevatorExpert(ScenarioElevatorSim(site).config)
    proposal = expert.propose({"event_type": "freight_priority", "entity_id": expert.config.group_id("freight"), "at": at},
                              {"price_period": "peak"})
    assert proposal.cost.cny < 0
    return [SimAction(entity_id=a.entity_id, command=a.command, params=a.params,
                      interval=Interval(start=at, end=at + timedelta(hours=1))) for a in proposal.actions], proposal


def test_batch_mode_cuts_trip_energy_for_same_completed_tasks(site):
    at = datetime(2026, 7, 1, 3, 0, tzinfo=UTC)  # 当地 11:00
    flow = lambda m: {"tasks:material_handling": 2.0 if m < 20 else 0.0}  # noqa: E731
    actions, proposal = _batch_actions(site, at)
    plain, _ = _run_minutes(site, at, 60, flow)
    batch, _ = _run_minutes(site, at, 60, flow, actions)
    freight = ScenarioElevatorSim(site).config.group_id("freight")
    assert plain.devices[freight]["queue"] == batch.devices[freight]["queue"] == 0
    # 完成量相同（40 件）时，合批的行程能耗更低，方向与提案 cost.cny < 0 一致
    assert batch.sims["elevator"]["energy_kwh"] < plain.sims["elevator"]["energy_kwh"]
    assert plain.sims["elevator"]["energy_kwh"] == pytest.approx(24.0)
    assert batch.sims["elevator"]["energy_kwh"] == pytest.approx(16.0)
