"""B17 复审补修（PR #24 评审）：快照不被正式运行改写、专家失败结果不打断仿真、配电柜容量进检查。"""
import dataclasses
from datetime import timedelta

import pytest

from plantkg.core.contracts import Interval, SimAction
from plantkg.core.ports import AgentResultSource
from plantkg.world.scenario import TrialRunner
from plantkg.world.scenario.ledger import Grid
from plantkg.world.scenario.runner import default_registry

from .conftest import local, scenario

ADMIN = "entity://demo-electronics/zone/admin"


def test_snapshot_not_mutated_by_later_formal_run(make):
    runner = make(scenario())
    runner.run(until=local("08:00"))
    snap = runner.snapshot()
    horizon = Interval(start=local("08:00"), end=local("08:15"))
    before = TrialRunner(runner.world).trial(snap, [], horizon)
    runner.apply(SimAction(entity_id=ADMIN, command="set_mode", params={"mode": "off"},
                           interval=Interval(start=local("08:01"), end=local("08:15"))))
    runner.run(until=local("08:03"))
    after = TrialRunner(runner.world).trial(snap, [], horizon)
    assert after.soft_cost.cny == before.soft_cost.cny


class _BlockedExpert:
    """live 结果源：返回受阻；可选带一份合法提案，验证业务状态优先于提案内容。"""
    source_kind = "nexent"

    def __init__(self, with_proposal: bool):
        self.calls, self.with_proposal = 0, with_proposal
        self.inner = default_registry().resolve_mode(AgentResultSource, "deterministic", key="elevator") if with_proposal else None

    def get(self, kind, binding, payload):
        self.calls += 1
        result = {"status": "blocked", "proposal_ids": []}
        if self.with_proposal:
            result["proposal"] = self.inner.get(kind, binding, payload)["proposal"]
        return result


@pytest.mark.parametrize("with_proposal", [False, True], ids=["no_proposal", "with_proposal"])
def test_blocked_result_falls_back_and_simulation_continues(make, with_proposal):
    events = [{"at": "08:05", "type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "summary": "拥堵"}]
    wall = [local("08:00") + timedelta(days=200)]
    registry = default_registry()
    blocked = _BlockedExpert(with_proposal)
    registry.register_mode(AgentResultSource, "live", blocked, key="elevator")
    runner = make(scenario(events=events), mode="live", registry=registry, wall=lambda: wall[0], name="live.db")
    live = runner.run(until=local("09:00"))
    det = make(scenario(events=events), mode="evaluation", name="det.db").run(until=local("09:00"))
    assert runner.state.at == local("09:00")  # 仿真没有停在 08:05
    assert blocked.calls == 2  # 重试一次
    run = live.agent_runs[0]
    assert run.status == "failed" and run.proposal_ids
    assert [a["action"] for a in live.actions] == [a["action"] for a in det.actions]  # 确定性兜底照常执行


def test_failed_result_keeps_returned_at_and_total_latency(make):
    """受阻结果也要记墙钟：耗时从首次请求算到末次返回，重试的两次都计入。"""
    wall = [local("08:00") + timedelta(days=200)]

    class SlowBlocked(_BlockedExpert):
        def get(self, kind, binding, payload):
            wall[0] += timedelta(seconds=30)
            return super().get(kind, binding, payload)

    events = [{"at": "08:05", "type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "summary": "拥堵"}]
    registry = default_registry()
    registry.register_mode(AgentResultSource, "live", SlowBlocked(False), key="elevator")
    run = make(scenario(events=events), mode="live", registry=registry, wall=lambda: wall[0]).run(until=local("08:10")).agent_runs[0]
    assert run.status == "failed" and run.timing.retries == 1
    assert run.timing.returned_at is not None and run.timing.latency_ms == 60000


def test_panel_capacity_declared_on_site_is_checked_and_scenario_can_override(make):
    runner = make(scenario())
    site = runner.world.index.site
    site.nodes["P-AIR"] = dataclasses.replace(site.nodes["P-AIR"], capacity_kva=10)
    grid = Grid(runner.world.index, {})
    assert grid.limits["P-AIR"] == 10 * site.power_factor
    assert Grid(runner.world.index, {"P-AIR": 500}).limits["P-AIR"] == 500  # 剧本按 kW 覆盖
    runner.world.grid = grid
    runner.run(until=local("08:00"))
    result = TrialRunner(runner.world).trial(runner.snapshot(), [], Interval(start=local("08:00"), end=local("08:05")))
    # 空候选不再判不可行（#173 相对语义）：既有容量越界记入基线，候选只对新增违规负责
    assert result.feasible is True and result.violations == ()
    assert any("P-AIR" in v.detail for v in result.baseline_violations if v.kind == "capacity")


def test_instruction_in_hard_zone_is_rejected_not_registered(make):
    cleanroom = "entity://demo-electronics/zone/cleanroom"
    sc = scenario(operator_actions=[
        {"at": "13:00", "kind": "instruction", "payload": {"text": "洁净车间保持 26 ℃", "zone": "cleanroom", "setpoint_c": 26, "start": "14:00", "end": "14:30"}},
        {"at": "13:05", "kind": "instruction", "payload": {"text": "行政楼保持 24 ℃", "zone": "admin", "setpoint_c": 24, "start": "14:00", "end": "14:30"}}])
    runner = make(sc)
    trace = runner.run(until=local("14:45"))
    assert [r.text for r in runner.temp_rules.active(local("14:00"))] == ["行政楼保持 24 ℃"]
    assert [r.text for r in trace.temp_rules.values()] == ["行政楼保持 24 ℃"] and all(r.estimated_cost_cny is not None for r in trace.temp_rules.values())
    rejected = [n for n in trace.notes if n["kind"] == "rejected" and n.get("rule_text")]
    assert len(rejected) == 1 and rejected[0]["zone"] == "cleanroom" and "硬约束" in rejected[0]["reason"]
    assert trace.state_at(local("14:00"))[cleanroom]["setpoint"] == 22.0
    assert trace.state_at(local("14:00"))[ADMIN]["setpoint"] == 24.0
