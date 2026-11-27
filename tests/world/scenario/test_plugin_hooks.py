"""B17 场景装配钩子：同名仿真器原位替换、observe 阶段置后、真实专家优先于替身、领域包按列表缓存。"""
from __future__ import annotations

from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.world.scenario import runner
from plantkg.world.scenario.runner import SCENARIO_HOOK, default_registry, pack_registry
from plantkg.core.contracts import Interval, SimAction
from plantkg.world.scenario.sims import SIM_HOOK, default_simulators
from plantkg.world.sim.base import MINUTE

from .conftest import local, scenario

BAT = "entity://demo-electronics/battery/BAT1"


class _Sim:
    def __init__(self, name, stage=None):
        self.name = name
        if stage:
            self.stage = stage


def _scenario_registry(*factories):
    reg = Registry()
    for f in factories:
        reg.register_hook(SIM_HOOK, f)
    return reg


def test_same_name_replaces_in_place_and_observe_goes_last():
    observe = _Sim("sensors", stage="observe")
    elevator = _Sim("elevator")
    extra = _Sim("faults")
    reg = _scenario_registry(lambda i: observe, lambda i: elevator, lambda i: extra)
    sims = default_simulators(None, reg)
    names = [s.name for s in sims]
    assert names == ["profile", "hvac", "elevator", "air", "faults", "sensors"]
    assert sims[2] is elevator


def test_without_hooks_order_unchanged():
    assert [s.name for s in default_simulators(None, Registry())] == ["profile", "hvac", "elevator", "air"]


def test_plugin_expert_wins_over_placeholder():
    class _Real:
        def get(self, kind, binding, payload):
            return {}

    real = _Real()
    plugins = Registry()
    plugins.register_hook(SCENARIO_HOOK, lambda r: r.register_mode(AgentResultSource, "deterministic", real, key="elevator"))
    reg = default_registry(plugins)
    assert reg.resolve_mode(AgentResultSource, "deterministic", key="elevator") is real
    assert reg.resolve_mode(AgentResultSource, "deterministic", key="hvac") is not real


def test_pack_registry_cached_per_pack_list(monkeypatch):
    loads = []
    monkeypatch.setattr(runner.Registry, "load", lambda self, dirs, packs, settings=None: loads.append(tuple(packs)) or [])
    runner._load_packs.cache_clear()
    try:
        a = pack_registry(["pv"])
        assert pack_registry(["pv"]) is a
        assert pack_registry(["pv", "tariff"]) is not a
        assert loads == [("pv",), ("pv", "tariff")]
    finally:
        runner._load_packs.cache_clear()


class _BatteryProbe:
    """只读观测器：记录本分钟看到的储能状态。"""
    name, stage = "probe", "observe"

    def __init__(self):
        self.seen = {}

    def step(self, state, acts, dt):
        self.seen[state.at] = dict(state.devices[BAT])
        return state


def test_observe_sees_final_physical_state_of_the_same_minute(make):
    runner = make(scenario(initial={"soc": 0.5}))
    probe = _BatteryProbe()
    runner.world.simulators.append(probe)
    runner.run(until=local("08:00"))
    runner.apply(SimAction(entity_id=BAT, command="discharge", params={"power_kw": 400.0},
                           interval=Interval(start=local("08:00"), end=local("08:02"))))
    trace = runner.run(until=local("08:04"))
    cap = runner.world.storage.capacity_kwh
    for m in trace.ledger:
        at = m.entry.interval.start
        if local("08:00") <= at < local("08:04"):
            seen = probe.seen[at]
            assert seen["soc"] == round(m.entry.energy_end_kwh / cap, 6)  # 与同分钟结算后的 SOC 一致
            assert seen["power_kw"] == round(m.entry.charge_kwh * 60 - m.entry.discharge_kwh * 60, 3)
    assert probe.seen[local("08:00")]["power_kw"] == -400.0  # 放电开始的首分钟
    assert probe.seen[local("08:02")]["power_kw"] == 0.0  # 放电结束后
