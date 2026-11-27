"""B17 试算相对语义与时间边界（#173 / #135）。

既有违规下候选不加重即可行；空候选不判 infeasible；试算 horizon 截到剧本结束；跨天临时指令记 rejected
且不中止运行。全部经场景默认装配（`build_world` + `ScenarioRunner`）取证。
"""
from datetime import timedelta
from pathlib import Path

from plantkg.core.contracts import Interval, SimAction
from plantkg.world.scenario import TrialRunner, load_scenario

from .conftest import local, scenario

ROOT = Path(__file__).resolve().parents[3]


def _heat_snapshot(make):
    runner = make(load_scenario(ROOT / "configs/scenarios/extreme_heat_wave.yaml"), name="heat.db")
    runner.run(until=local("14:00"))
    return runner


def test_empty_candidate_reports_baseline_violations_instead_of_infeasible(make):
    """#135：热浪午后同一快照上，空候选不再被判 infeasible，既有越界记在 baseline_violations。"""
    runner = _heat_snapshot(make)
    result = TrialRunner(runner.world).trial(runner.snapshot(), [], Interval(start=local("14:00"), end=local("14:05")))
    assert result.feasible is True and result.violations == ()
    assert any(v.kind == "hard_zone" for v in result.baseline_violations)


def test_candidate_only_reports_violations_added_on_top_of_baseline(make):
    """储能放电是缓解动作：既有硬带越界不转移给候选，violations 只剩新增（空）。"""
    runner = _heat_snapshot(make)
    action = SimAction(entity_id=runner.world.index.battery_id, command="discharge", params={"power_kw": 150.0},
                       interval=Interval(start=local("14:00"), end=local("14:05")))
    result = TrialRunner(runner.world).trial(runner.snapshot(), [action], Interval(start=local("14:00"), end=local("14:05")))
    assert result.feasible is True and result.violations == ()
    assert result.baseline_violations  # 既有越界仍在基线里如实保留


def test_same_node_same_minute_worsening_is_infeasible(make):
    """同一节点同一分钟从 72 kW 恶化到 572 kW 必须算新增违规，不能只看（类型, 实体, 分钟）（评审 P1-1）。"""
    from plantkg.world.scenario.ledger import Grid

    runner = _heat_snapshot(make)
    # 既有容量越界：P-HVAC-P 平时约 72 kW；通风动作（ThermalSim 记到 EXF-P1）会让同一节点同分钟涨约 500 kW
    runner.world.grid = Grid(runner.world.index, {"P-HVAC-P": 50.0})
    horizon = Interval(start=local("14:00"), end=local("14:05"))
    snap = runner.snapshot()
    baseline = TrialRunner(runner.world).trial(snap, [], horizon)
    assert baseline.feasible and any(v.kind == "capacity" and v.entity_id == "P-HVAC-P" for v in baseline.baseline_violations)
    worse = SimAction(entity_id=runner.world.index.eid("AC-1"), command="ventilate", params={"kw": 500.0}, interval=horizon)
    result = TrialRunner(runner.world).trial(snap, [worse], horizon)
    assert result.feasible is False and any(v.kind == "capacity" and v.entity_id == "P-HVAC-P" for v in result.violations)
    assert runner.estimate_cost([worse], horizon.end) is None


def test_horizon_beyond_scenario_end_is_truncated(make):
    """试算区间超过剧本结束只跑到结束，不因冻结输入缺分钟抛 KeyError。"""
    runner = make(scenario(), name="horizon.db")
    runner.run(until=local("08:00"))
    horizon = Interval(start=local("08:05"), end=local("08:05") + timedelta(days=1))
    result = TrialRunner(runner.world).trial(runner.snapshot(), [], horizon)
    assert result.minute_ledger and result.minute_ledger[-1].interval.start == local("23:59")


def test_cross_day_instruction_is_rejected_and_run_completes(make):
    """临时规则结束越过剧本结束：记 rejected、不登记规则、不下发动作，运行跑到结束。"""
    sc = scenario(operator_actions=[{
        "at": "00:05", "kind": "instruction",
        "payload": {"text": "行政楼整晚保持 24 ℃", "zone": "admin", "setpoint_c": 24, "start": "00:10", "end": "+1 12:00"}}])
    runner = make(sc, name="crossday.db")
    trace = runner.run(until=local("00:15"))
    assert runner.state.at == local("00:15")
    assert trace.temp_rules == {}
    rejected = [n for n in trace.notes if n["kind"] == "rejected" and "超过剧本结束" in (n.get("reason") or "")]
    assert len(rejected) == 1 and rejected[0]["zone"] == "admin" and rejected[0]["rule_text"] == "行政楼整晚保持 24 ℃"
    assert all(not (a["action"]["params"] or {}).get("temp_rule") for a in trace.actions)
