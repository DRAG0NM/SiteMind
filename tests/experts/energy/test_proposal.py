"""B19 能源专家的提案、MCP 工具登记与计划账本 provider。"""
from __future__ import annotations

import pytest

from plantkg.core.contracts import Actor, Context, PlantkgError, Role, RunBinding
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.experts.energy.day import open_demo_inputs
from plantkg.experts.energy.proposal import EnergyExpert, energy_state_hook, install, register_agent_source
from plantkg.world.scenario.runner import ScenarioRunner, ScenarioWorld, default_registry

from .conftest import local

STATE = {"soc": 0.42, "soc_min": 0.1, "admin_temp": 25.0,
         "battery_nodes": ["entity://demo-electronics/battery/BAT1", "TX1", "LV1"],
         "battery_id": "entity://demo-electronics/battery/BAT1",
         "ids": {"battery": "entity://demo-electronics/battery/BAT1", "LT-P": "entity://demo-electronics/lighting/LT-P"},
         "at": local("08:05").isoformat()}


def test_lighting_fault_switches_circuit():
    proposal = EnergyExpert().propose({"event_type": "lighting_fault", "entity_id": "entity://demo-electronics/lighting/LT-A03"}, STATE)
    assert [a.command for a in proposal.actions] == ["switch_circuit"]
    assert proposal.actions[0].entity_id.endswith("/LT-A03")
    assert 0.5 <= proposal.confidence <= 1


def test_load_spike_discharges_and_sheds_lighting():
    proposal = EnergyExpert().propose({"event_type": "load_spike", "entity_id": "entity://demo-electronics/air_compressor/AC-2",
                                       "payload": {"discharge_kw": 450}}, STATE)
    commands = [a.command for a in proposal.actions]
    assert commands == ["discharge", "shed"]
    assert proposal.actions[0].params["power_kw"] == 450
    assert proposal.actions[1].params["ratio"] == 0.8  # B17 的 shed 参数是剩余功率比例：削减 20%
    assert proposal.cost.cny < 0  # 省钱的估算（负代价）


def test_lighting_shed_executes_as_20_percent_cut(scene, make_assembly):
    """事件级照明削峰经真实 B17 仿真执行后，LT-P 保留 80% 功率（不是降到 20%）。"""
    inputs = open_demo_inputs(scene, profile="dev")
    world = ScenarioWorld.build(scene, inputs, registry=default_registry())
    runner = ScenarioRunner(world, make_assembly("shed.db").db, registry=default_registry(), mode="evaluation")
    runner.run(until=local("08:06"))
    before = runner.state.power_kw["LT-P"]
    runner.run(until=local("08:07"))
    assert before > 0 and runner.state.power_kw["LT-P"] == pytest.approx(before * 0.8, rel=1e-6)
    shed = [a["action"] for a in runner.trace.actions if a["action"]["command"] == "shed"]
    assert shed and shed[0]["params"]["ratio"] == 0.8


def test_energy_state_hook_reads_real_runner(day):
    """真实 DayRun 的状态回调：ids 取 runner.ids（字典），提案业务钩子能据此返回动作。"""
    day.execute(local("08:06"))
    runner = day.runner
    st = next(t for t in runner.tickets.tickets.values() if t.event["event_type"] == "load_spike")
    real = RunBinding(run_id=runner.run_info.run_id, knowledge_version=runner.knowledge_version, ticket_id=st.ticket.ticket_id,
                      round=st.ticket.round, state_version=0, trigger_at=st.trigger_at)
    event, state = energy_state_hook(runner)(real)
    assert event["event_type"] == "load_spike"
    assert state["ids"]["battery"] == runner.world.index.battery_id

    registry = Registry()
    install(registry)
    registry.register_hook(f"energy_state:{runner.run_info.run_id}", energy_state_hook(runner))
    hook = registry.hooks_for("expert_propose:energy")[-1]
    ctx = day.ctx.model_copy(update={"run_id": runner.run_info.run_id})
    binding = real
    proposal = hook(ctx, binding)
    assert [a.command for a in proposal.actions] == ["discharge", "shed"]


def test_no_action_when_soc_at_floor_or_other_domain():
    low = dict(STATE, soc=0.105)
    assert EnergyExpert().propose({"event_type": "load_spike"}, low).actions == ()
    assert EnergyExpert().propose({"event_type": "elevator_congestion"}, STATE).actions == ()


def test_install_registers_proposal_hook_and_agent_source():
    registry = Registry()
    registry.register_hook("energy_state:run-x", lambda binding: ({"event_type": "lighting_fault",
                                                          "entity_id": "entity://demo-electronics/lighting/LT-A03"}, STATE))
    install(registry)
    hook = registry.hooks_for("expert_propose:energy")[-1]
    binding = RunBinding(run_id="run-x", knowledge_version=1, ticket_id="TK-001", round=1, state_version=0,
                         trigger_at=local("08:05"))
    ctx = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=Actor(actor_id="u", roles=(Role.operator,)),
                  channel="offline", trace_id="t", run_id="run-x", as_of=local("08:05"), version_manifest_id="vm")
    proposal = hook(ctx, binding)
    assert proposal.expert == "energy" and proposal.ticket_id == "TK-001"
    assert [a.command for a in proposal.actions] == ["switch_circuit"]

    register_agent_source(registry, EnergyExpert())
    source = registry.resolve_mode(AgentResultSource, "deterministic", key="energy")
    result = source.get("expert", binding, {"event": {"event_type": "lighting_fault",
                                                      "entity_id": "entity://demo-electronics/lighting/LT-A03"},
                                            "state": STATE, "ids": STATE["ids"]})
    assert result["proposal"]["expert"] == "energy"


def test_energy_propose_tool_is_registered_by_the_default_assembly():
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings

    services = build_services(load_settings(profile="dev", overrides={"storage.enabled": False}))
    try:
        binding = services.registry.tools.get("energy_propose")
        assert binding is not None and binding.spec.provider == "B19"
        assert services.registry.hooks_for("expert_propose:energy")  # B23 统一包装会取到这个业务函数
    finally:
        services.close()


def test_plan_ledger_provider_roundtrip(day):
    run_id = day.plan_run.run_id
    ctx = day.ctx.model_copy(update={"run_id": run_id})
    page = day.assembly.ledgers.query(ctx, run_id, None, 10)
    assert len(page.items) == 1 and page.items[0].plan_id == day.plan.plan.plan_id and page.items[0].balanced
    assert day.assembly.ledgers.load(run_id)[0].kind == "plan"
    with pytest.raises(PlantkgError):  # 会话必须绑定所查询的运行
        day.assembly.ledgers.query(ctx, "run-other", None, 10)


def test_shed_ratio_follows_payload_and_is_clamped():
    """capacity_shed_request 请求 15%：照明剩余 85%；超过配置最大削减比例（50%）的请求夹到 50%。"""
    def ratio(payload):
        event = {"event_type": "capacity_shed_request", "payload": payload}
        shed = [a for a in EnergyExpert().propose(event, STATE).actions if a.command == "shed"]
        return shed[0].params["ratio"]
    assert ratio({"shed_ratio": 0.15}) == pytest.approx(0.85)
    assert ratio({}) == pytest.approx(0.8)
    assert ratio({"shed_ratio": 0.9}) == pytest.approx(0.5)


def test_mcp_propose_reads_run_day_state_through_bootstrap_registry(scene, make_assembly):
    """经 bootstrap 装配的 energy_propose 业务钩子，读到 run_day 注册的运行状态并给出提案（不手工再 register_hook）。"""
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.experts.energy.day import run_day

    services = build_services(load_settings(profile="dev", overrides={"storage.enabled": False}))
    try:
        day = run_day(scenario=scene, assembly=make_assembly("mcp.db"), date="2026-07-01", registry=services.registry)
        day.execute(local("08:08"), start_at=local("07:30"))
        run_id = day.runner.run_info.run_id
        assert services.registry.hooks_for(f"energy_state:{run_id}")
        st = next(t for t in day.runner.tickets.tickets.values() if t.event["event_type"] == "load_spike")
        binding = RunBinding(run_id=run_id, knowledge_version=1, ticket_id=st.ticket.ticket_id, round=st.ticket.round,
                             state_version=0, trigger_at=st.trigger_at)
        ctx = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=Actor(actor_id="u", roles=(Role.operator,)),
                      channel="offline", trace_id="t", as_of=local("08:08"), version_manifest_id="vm")
        proposal = services.registry.hooks_for("expert_propose:energy")[-1](ctx, binding)
        assert proposal.actions and {a.command for a in proposal.actions} >= {"discharge"}
    finally:
        services.close()


def test_mcp_propose_takes_each_tickets_own_event_not_the_latest_alarm(scene, make_assembly):
    """同运行、同分钟、不同设备两张事件单：每张单的提案 ticket_id、动作设备都对应自己的事件，对不上的绑定不回退到最近告警。"""
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.experts.energy.day import run_day
    from plantkg.world.scenario.script import ScriptEvent

    services = build_services(load_settings(profile="dev", overrides={"storage.enabled": False}))
    try:
        day = run_day(scenario=scene, assembly=make_assembly("mcp2.db"), date="2026-07-01", registry=services.registry)
        day.execute(local("10:29"), start_at=local("10:25"))
        runner = day.runner
        at = local("10:30")
        runner.queue.append(("event", at, ScriptEvent(at=at.isoformat(), id="act2-light-p", type="lighting_fault", entity_id="LT-P",
                                                      severity=2, summary="生产照明回路故障")))
        runner.queue.sort(key=lambda q: q[1])
        day.execute(local("10:31"))
        run_id = runner.run_info.run_id
        tickets = [t for t in runner.tickets.tickets.values() if t.event["event_type"] == "lighting_fault"]
        assert {t.entity_id.rsplit("/", 1)[-1] for t in tickets} == {"LT-A03", "LT-P"}
        ctx = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=Actor(actor_id="u", roles=(Role.operator,)),
                      channel="offline", trace_id="t", as_of=local("10:31"), version_manifest_id="vm")
        hook = services.registry.hooks_for("expert_propose:energy")[-1]
        for t in tickets:
            binding = RunBinding(run_id=run_id, knowledge_version=1, ticket_id=t.ticket.ticket_id, round=t.ticket.round,
                                 state_version=0, trigger_at=t.trigger_at)
            proposal = hook(ctx, binding)
            assert proposal.ticket_id == t.ticket.ticket_id
            assert [a.entity_id for a in proposal.actions] == [t.entity_id]
        stale = RunBinding(run_id=run_id, knowledge_version=1, ticket_id=tickets[0].ticket.ticket_id,
                           round=tickets[0].ticket.round + 1, state_version=0, trigger_at=local("10:31"))
        assert hook(ctx, stale).actions == ()  # 轮次对不上：不回退到最近告警
    finally:
        services.close()
