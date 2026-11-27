"""B19 能源专家：日前运行（B09 → B15 → B17 → B08）与实时削峰冒烟测试。"""
from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, Rule, StateSnapshot
from plantkg.experts.energy.day import run_day
from plantkg.experts.energy.forecast import build_forecasts
from plantkg.experts.energy.planner import plan_day
from plantkg.experts.energy.realtime import SlotTarget
from plantkg.world.scenario.script import load_scenario

from .conftest import ROOT, local


def _window_minutes(day, start, end):
    return day.minutes(start, end)


def _peak_kw(minutes) -> float:
    return max((m.entry.grid_import_kwh * 60 for m in minutes), default=0.0)


def _peak_without_storage(minutes) -> float:
    return max(((m.entry.grid_import_kwh + m.entry.discharge_kwh - m.entry.charge_kwh) * 60 for m in minutes), default=0.0)


def _replan(day, packs=None, *, end_target_kwh=None):
    """按 run_day 的日前规划输入直接走 plan_day 公开入口，计划账本仍由 B08 复算。"""
    request = day.plan.request
    energy = day.plan.ledger.entries[0].energy_start_kwh
    snapshot = StateSnapshot(run_id=request.context.run_id, snapshot_id=f"test@{request.context.as_of.isoformat()}",
                             event_seq=0, as_of=request.context.as_of, version_manifest_id=request.context.version_manifest_id,
                             pack_states={"storage": {"energy_kwh": energy}})
    return plan_day(packs or day.packs, day.forecasts, snapshot, request.context, request.horizon,
                    energy_start_kwh=energy, end_target_kwh=end_target_kwh or day.plan.end_target_kwh, operations=())


def test_plan_ledger_balances_with_degradation_cost(day):
    """模型目标含储能衰减费时，购电费用与另列的衰减项合计才等于模型目标，账本仍 balanced。"""
    packs = dataclasses.replace(day.packs, storage=dataclasses.replace(day.packs.storage, degradation_cny_per_kwh=0.05))
    replan = _replan(day, packs)
    assert replan.result.solver_status == "optimal", replan.result.validation_errors
    assert replan.ledger.balanced, replan.ledger.violations[:3]
    assert replan.ledger.total_cost_cny is not None
    assert replan.ledger.objective_terms and "storage.degradation" in replan.ledger.objective_terms
    assert replan.ledger.objective_value_cny == pytest.approx(replan.result.objective_value)


def test_plan_ledger_deducts_pv_limit_from_available(day):
    """pv_output_cap 的限发不算可发：计划账本的 pv_available 取 预测 − pv_limit。"""
    cap = Rule(rule_id="rule-day-pv-cap", kind="pv_output_cap", subject_ids=(day.packs.pv.entity_id,), quantity="pv_output",
               unit="kW", bounds={"upper": 0.0}, effective_from=day.day_start - timedelta(days=1),
               available_at=day.day_start - timedelta(days=1), config_ref=day.packs.pv.config_ref,
               nature="configured", review_status="trusted_config")
    packs = dataclasses.replace(day.packs, rules=day.packs.rules + (cap,))
    replan = _replan(day, packs)
    assert replan.result.solver_status == "optimal", replan.result.validation_errors
    assert replan.ledger.balanced, replan.ledger.violations[:3]
    limits = {action.interval.start: action.value for action in replan.plan.actions if action.quantity == "pv_limit"}
    forecast = next(f for f in day.forecasts.forecasts if f.quantity == "pv_available")
    shift = int((replan.plan.valid_interval.start - forecast.target.start) / timedelta(minutes=15))
    raw = forecast.values[shift:shift + len(replan.ledger.entries)]
    for entry, value in zip(replan.ledger.entries, raw, strict=True):
        assert entry.pv_available_kwh == pytest.approx((value - limits.get(entry.interval.start, 0.0)) * 0.25, abs=1e-9)


def test_pending_overlapping_tariff_rule_does_not_block_pricing(day):
    """待复核的可见电价规则与已批准规则重叠：B09 把它当待处理，定价只用已编译规则，计划仍 optimal。"""
    base = next(rule for rule in day.packs.rules if rule.kind == "tariff_tou")
    pending = base.model_copy(update={"rule_id": "rule-tariff-pending"})
    pending = pending.model_copy(update={"review_status": "pending", "reviewer": None})
    packs = dataclasses.replace(day.packs, rules=day.packs.rules + (pending,))
    replan = _replan(day, packs)
    assert replan.result.solver_status == "optimal", replan.result.validation_errors
    assert replan.ledger.balanced, replan.ledger.violations[:3]
    assert any(issue.rule_id == "rule-tariff-pending" for issue in replan.result.pending_issues)


def test_approved_overlapping_tariff_rule_is_still_rejected(day):
    """已批准且可见的两条重叠电价仍必须报错，过滤器不能把真冲突静默丢掉。"""
    base = next(rule for rule in day.packs.rules if rule.kind == "tariff_tou")
    duplicate = base.model_copy(update={"rule_id": "rule-tariff-duplicate"})
    packs = dataclasses.replace(day.packs, rules=day.packs.rules + (duplicate,))
    with pytest.raises(PlantkgError) as exc:
        _replan(day, packs)
    assert exc.value.code == ErrorCode.DOMAIN_INVALID


def test_concurrent_plan_day_end_floor_is_per_request(day):
    """两个并发 plan_day 的期末下界各取各的；不靠共享的 last_builder。"""
    def floors(target):
        return _replan(day, end_target_kwh=target).end_floor_kwh

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(floors, (400.0, 500.0))) == pytest.approx([400.0, 500.0])


def test_day_ahead_plan_gate_and_plan_ledger(day):
    """一天跑通：B09 最优、B08 计划账本平衡、门控可执行、计划账本可按运行查回。"""
    assert day.plan.plan.solver_status == "optimal"
    ledger = day.plan.ledger
    assert ledger.balanced, ledger.violations[:3]
    assert len(ledger.entries) == 96
    assert ledger.entries[-1].energy_end_kwh >= 400.0 - 1e-6  # 期末目标 = 当天起始 400 kWh（下界，可高于）
    assert day.plan.end_floor_kwh == pytest.approx(400.0)
    assert day.decision.outcome in ("review_required", "auto_confirmed")
    assert day.executable.status in ("confirmed", "auto_confirmed")

    stored = day.assembly.gate.repository.metadata(day.plan.plan.plan_id)
    assert stored["planning_request"]["horizon"] == day.plan.request.horizon.model_dump(mode="json")
    saved = day.assembly.ledgers.load(day.plan_run.run_id)
    assert len(saved) == 1 and saved[0].plan_id == day.plan.plan.plan_id and saved[0].balanced


def test_tariff_counterfactual_uses_same_predictions_and_b08_ledger(day):
    import copy
    import dataclasses
    from plantkg.experts.energy.planner import compare_tariff_interpretations
    horizon = day.plan.request.horizon
    cutoff = horizon.start - timedelta(hours=6)
    ctx = day.plan.request.context.model_copy(update={"as_of": cutoff})
    snapshot = StateSnapshot(run_id=ctx.run_id, snapshot_id="counterfactual-snapshot", event_seq=0, as_of=cutoff,
                             version_manifest_id=ctx.version_manifest_id,
                             pack_states={"storage": {"energy_kwh": day.scenario.initial.soc * day.packs.storage.capacity_kwh}})
    forecasts = build_forecasts(day.inputs, horizon, ctx)
    result = compare_tariff_interpretations(day.packs, day.packs, forecasts, snapshot, ctx, horizon,
                                           energy_start_kwh=day.scenario.initial.soc * day.packs.storage.capacity_kwh,
                                           end_target_kwh=day.plan.end_target_kwh)
    assert result["status_a"] == result["status_b"] == "optimal"
    assert result["cost_a_under_b"] == pytest.approx(result["cost_b_under_b"])
    tariff = next(rule for rule in day.packs.rules if rule.kind == "tariff_tou")
    condition = copy.deepcopy(tariff.condition)
    for period in condition["periods"]:
        if period["name"] == "peak":
            period["ratio"] += 0.2
    alternative = tariff.model_copy(update={"condition": condition})
    packs_b = dataclasses.replace(day.packs, rules=tuple(alternative if rule.rule_id == tariff.rule_id else rule
                                                        for rule in day.packs.rules))
    result_b = compare_tariff_interpretations(day.packs, packs_b, forecasts, snapshot, ctx, horizon,
                                             energy_start_kwh=day.scenario.initial.soc * day.packs.storage.capacity_kwh,
                                             end_target_kwh=day.plan.end_target_kwh)
    assert result_b["cost_a_under_b"] >= result_b["cost_b_under_b"] - 0.01
    from plantkg.core.ports import VersionStore
    from plantkg.core.registry import Registry
    from plantkg.experts.energy.proposal import install as install_energy
    from plantkg.packs.tariff import price_schedule
    registry = Registry()
    install_energy(registry)
    registry.register(VersionStore, day.db.versions)
    published = alternative.model_copy(update={"value": 1.2})
    registry.register_hook("knowledge.b30.rules", lambda manifest, at, as_of: (published,))
    day.registry = registry
    day._start()
    replanning_at = local("00:15")
    day.fast_forward(replanning_at)
    day._replan(replanning_at)
    assert not day.replan_failures
    slot = Interval(start=replanning_at, end=replanning_at + timedelta(minutes=15))
    expected = price_schedule([published], day.packs.tariff.base_prices, [slot])[0].price_cny_per_kwh
    assert day.current_plan.prices[0].price_cny_per_kwh == pytest.approx(expected)
    with pytest.raises(PlantkgError):
        compare_tariff_interpretations(day.packs, day.packs, forecasts, snapshot,
                                      ctx.model_copy(update={"as_of": cutoff - timedelta(minutes=1)}), horizon,
                                      energy_start_kwh=day.scenario.initial.soc * day.packs.storage.capacity_kwh,
                                      end_target_kwh=day.plan.end_target_kwh)


def test_lighting_fault_switches_circuit_via_scenario(scene, make_assembly):
    """10:30 照明回路故障：能源专家提案经 B17 直派器 apply 成 switch_circuit 动作。"""
    day = run_day(scenario=scene, assembly=make_assembly("lighting.db"), date="2026-07-01")
    day.execute(local("10:36"), start_at=local("10:25"))
    actions = [rec["action"] for rec in day.trace.actions
               if rec["action"]["command"] == "switch_circuit" and rec["source"].get("expert") == "energy"]
    assert actions and actions[0]["entity_id"].endswith("/LT-A03")
    assert any(e.event_type == "lighting_fault" for e in day.trace.events)


def test_spike_is_shaved_by_realtime_and_peak_drops(day):
    """08:05 突增：08:06 起分钟层放电，之后窗口峰值低于不充不放的峰值。"""
    day.execute(local("08:45"), start_at=local("07:30"))
    trace = day.trace
    assert all(m.balanced for m in trace.ledger)
    assert not trace.slot_problems
    assert day.verify_soc(local("07:30"), local("08:45"))["ok"]

    before = {m.entry.interval.start: m for m in _window_minutes(day, local("08:00"), local("08:06"))}
    at_0805 = before[local("08:05")]
    assert at_0805.entry.discharge_kwh * 60 < 50  # 响应预算内不动：08:05 仍按计划
    after = _window_minutes(day, local("08:06"), local("08:45"))
    assert all(m.entry.discharge_kwh * 60 > 300 for m in after[:5])  # 08:06 起放电削峰

    shaved_peak = _peak_kw(after)
    raw_peak = _peak_without_storage(after)
    assert raw_peak - shaved_peak > 300  # 突增期间的峰值被压下去
    assert _peak_kw(_window_minutes(day, local("08:00"), local("08:06"))) > shaved_peak  # 08:05 那一分钟仍如实记录

    cost = day.window_cost(local("07:30"), local("08:30"))
    assert cost["discharge_kwh"] > 0 and cost["saved_cny"] > 0


def test_spike_keeps_hvac_route_and_minute_layer_owns_storage(day):
    """08:05 突增：暖通仍收到事件并动作，储能只由分钟层控制，事件不再被记为 no_route。"""
    day.execute(local("08:08"), start_at=local("07:30"))
    routes = day.runner.dispatcher.routes
    assert "hvac" in routes["load_spike"] and "hvac" in routes["heat_wave"]
    assert routes["lighting_fault"] == ["energy"]

    ticket = next(t for t in day.trace.tickets.values() if "act1-spike" in t["event_ids"])
    assert "hvac" in ticket["experts"]
    hvac = [a["action"] for a in day.trace.actions if a["source"].get("expert") == "hvac"]
    assert any(a["command"] == "set_mode" for a in hvac)

    battery = day.world.index.battery_id
    storage = [a for a in day.trace.actions if a["action"]["entity_id"] == battery
               and a["action"]["command"] in ("charge", "discharge")]
    assert storage and all(a["source"].get("layer") == "minute" for a in storage)
    assert not any(n.get("result") == "no_route" and n.get("event_id") == "act1-spike" for n in day.trace.notes)


def test_heat_wave_still_reaches_hvac(make_assembly):
    """热浪事件保留暖通路由：09:30 热浪经直派器落到行政楼预冷动作。"""
    scene = load_scenario(ROOT / "configs/scenarios/extreme_heat_wave.yaml")
    day = run_day(scenario=scene, assembly=make_assembly("heat.db"), date="2026-07-01")
    day.execute(local("09:35"), start_at=local("09:29"))
    hvac = [a["action"] for a in day.trace.actions if a["source"].get("expert") == "hvac"]
    assert any(a["command"] == "set_mode" and a["params"].get("mode") == "precool" for a in hvac)


def test_pv_drop_realtime_uses_settled_pv_with_storage_headroom(scene, make_assembly):
    """云层事件后控制器读已结算的实际光伏：有储能余量时按少发的 101 kW 补放电，不用基础输入。"""
    pv_scene = load_scenario(ROOT / "configs/scenarios/extreme_pv_drop_peak.yaml")
    day = run_day(scenario=pv_scene, assembly=make_assembly("pv.db"), date="2026-07-02")
    day.execute(local("14:11", 2), start_at=local("14:00", 2))
    settled = day.minutes(local("14:10", 2), local("14:11", 2))[0].entry
    minute_state = day._minute_state(local("14:11", 2))
    assert minute_state["pv_kw"] == pytest.approx(settled.pv_available_kwh * 60, abs=1e-6)
    assert minute_state["pv_kw"] < 30  # 实际只剩两成
    forecast_pv = day._slot_data[local("14:00", 2)][1]
    target = SlotTarget(start=local("14:00", 2), end=local("14:15", 2), load_kw=minute_state["load_kw"], pv_kw=forecast_pv)
    action = day.controller.step(minute_state, target)
    assert action.command == "discharge"
    assert action.params["power_kw"] == pytest.approx(forecast_pv - minute_state["pv_kw"], abs=1.0)
    stale = day.controller.step(dict(minute_state, pv_kw=day.inputs.minute(local("14:10", 2)).pv_kw), target)
    assert stale.params["power_kw"] < 2.0  # 用基础输入只剩跟踪误差，补不到骤降的功率


def test_before_0806_minutes_are_unchanged_without_the_spike(scene, make_assembly):
    """08:06 前的分钟记录不因响应而变：去掉突增事件重跑，08:00–08:05 的充放逐分钟一致。"""
    quiet = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    quiet = quiet.model_copy(update={"events": tuple(e for e in quiet.events if e.id != "act1-spike")})
    a = run_day(scenario=load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml"),
                assembly=make_assembly("with.db"), date="2026-07-01")
    b = run_day(scenario=quiet, assembly=make_assembly("quiet.db"), date="2026-07-01")
    a.execute(local("08:06"), start_at=local("07:30"))
    b.execute(local("08:06"), start_at=local("07:30"))
    for minute in (local("08:00"), local("08:01"), local("08:02"), local("08:03"), local("08:04"), local("08:05")):
        left = next(m for m in a.minutes(minute, minute + timedelta(minutes=1)))
        right = next(m for m in b.minutes(minute, minute + timedelta(minutes=1)))
        assert left.entry.charge_kwh == pytest.approx(right.entry.charge_kwh, abs=1e-9)
        assert left.entry.discharge_kwh == pytest.approx(right.entry.discharge_kwh, abs=1e-9)
    assert a.minutes(local("08:05"), local("08:06"))[0].entry.grid_import_kwh > \
        b.minutes(local("08:05"), local("08:06"))[0].entry.grid_import_kwh  # 突增本身记在 08:05


def test_rolling_replan_uses_actual_soc_and_keeps_end_target(scene, make_assembly):
    """08:10 分叉、08:15/08:30 重解：初始 SOC 取分钟账本实际值；每次整日期末下界都不漂移。"""
    assembly = make_assembly("rolling.db")
    day = run_day(scenario=scene, assembly=assembly, date="2026-07-01")
    day.execute(local("08:45"), start_at=local("07:30"), fork_at=local("08:10"))
    replans = [r for r in day.replans if r.at >= local("08:15")]
    assert [r.at for r in replans][:2] == [local("08:15"), local("08:30")]
    for replan in replans:
        minute = next(m for m in day.minutes(replan.at, replan.at + timedelta(minutes=1)))
        assert replan.energy_kwh == pytest.approx(minute.entry.energy_start_kwh, abs=1e-6)
        assert replan.end_floor_kwh == pytest.approx(day.end_target_kwh)
        assert replan.solver_status == "optimal" and replan.elapsed_s < 5.0
    # 08:10 知识版本分叉：子运行快照储能与分钟账本 08:10 起始电量一致
    child = day.trace.runs[-1]
    assert child.forked_at == local("08:10")
    snapshot = assembly.db.store.snapshot(child.run_id, local("08:10"))
    minute = next(m for m in day.minutes(local("08:10"), local("08:11")))
    assert snapshot["pack_states"]["storage"]["energy_kwh"] == pytest.approx(minute.entry.energy_start_kwh, abs=1e-6)


def test_end_target_does_not_drift_with_low_actual_soc(scene, make_assembly):
    """初始/期末目标 500 kWh、08:15 实际只有 400 kWh：多次重解与 16:00 知识分叉后仍以 500 kWh 为下界。"""
    assembly = make_assembly("drift.db")
    tuned = scene.model_copy(update={"initial": scene.initial.model_copy(update={"soc": 0.25, "energy_end_target_kwh": 500.0})})
    day = run_day(scenario=tuned, assembly=assembly, date="2026-07-01")
    assert day.end_target_kwh == pytest.approx(500.0) and day.plan.end_floor_kwh == pytest.approx(500.0)  # 20% → 25%

    # 08:15 实际降到 400 kWh：直接按同一上下文重解，期末下界仍是 500
    for at, energy in ((local("08:15"), 400.0), (local("08:30"), 380.0), (local("16:00"), 300.0)):
        horizon = Interval(start=at, end=day.day_end)
        ctx = day.ctx.model_copy(update={"as_of": at})
        snapshot = StateSnapshot(run_id=day.plan_run.run_id, snapshot_id=f"test@{at.isoformat()}", event_seq=0, as_of=at,
                                 version_manifest_id=day.plan_run.version_manifest_id,
                                 pack_states={"storage": {"energy_kwh": energy}})
        replan = plan_day(day.packs, build_forecasts(day.inputs, horizon, ctx), snapshot, ctx, horizon,
                          energy_start_kwh=energy, end_target_kwh=500.0, operations=())  # 只验期末下界，不排工序
        assert replan.end_floor_kwh == pytest.approx(500.0), at
        assert replan.ledger.balanced

    # 16:00 知识版本分叉：子运行沿用父运行的期末目标，分叉后的重解仍以 500 为下界
    day.execute(local("16:10"), start_at=local("15:50"), fork_at=local("16:00"))
    child = day.trace.runs[-1]
    assert child.forked_at == local("16:00")
    assert day.db.runs.get_run(child.run_id).energy_end_target_kwh == pytest.approx(500.0)
    assert all(r.end_floor_kwh == pytest.approx(500.0) for r in day.replans)


def test_independent_soc_check_and_end_target_floor(day):
    """22:30 跑到日末：独立 SOC 核对为真，期末储能不低于目标。"""
    day.execute(start_at=local("22:30"))
    assert day.verify_soc()["ok"]
    end = day.end_report()
    assert end["reached_day_end"] and end["final_energy_kwh"] >= day.end_target_kwh - 1e-6 and end["end_gap_kwh"] == 0.0


def test_run_day_refuses_production_and_unaligned_fork(scene, make_assembly, day):
    with pytest.raises(PlantkgError):
        run_day(scenario=scene, assembly=make_assembly("prod.db"), date="2026-07-01", profile="production")
    with pytest.raises(PlantkgError):
        day.execute(local("08:20"), start_at=local("08:00"), fork_at=local("08:10") + timedelta(seconds=30))


def test_run_day_executes_event_level_lighting_shed(day):
    """整日运行里 08:05 突增的事件级照明削峰真的执行：LT-P 被削，电池充放仍只由分钟层下发。"""
    day.execute(local("08:08"), start_at=local("07:30"))
    shed = [a for a in day.trace.actions if a["action"]["command"] == "shed"]
    assert shed and all(a["action"]["entity_id"].endswith("/LT-P") and a["source"]["expert"] == "energy" for a in shed)
    assert day.runner.state.power_kw["LT-P"] == pytest.approx(42.0 * 0.8)  # 派生事件重复下发也只削一次 20%
    battery = day.world.index.battery_id
    assert all(a["source"].get("layer") == "minute" for a in day.trace.actions
               if a["action"]["entity_id"] == battery and a["action"]["command"] in ("charge", "discharge"))


def test_run_day_pv_drop_has_admission_and_ticket(make_assembly):
    """云层骤降：能源专家保留 pv_drop 路由，14:05 事件不再是 no_route，开出事件单并留有动作。"""
    pv_scene = load_scenario(ROOT / "configs/scenarios/extreme_pv_drop_peak.yaml")
    day = run_day(scenario=pv_scene, assembly=make_assembly("pvd.db"), date="2026-07-02")
    day.execute(local("14:20", 2), start_at=local("14:00", 2))
    assert day.runner.dispatcher.routes["pv_drop"] == ["energy"]
    assert any("energy" in t["experts"] for t in day.trace.tickets.values())
    assert not any(n.get("result") == "no_route" for n in day.trace.notes)


def test_replan_includes_event_extra_load(day):
    """08:05 的 550 kW 突增进 08:15 重解：该重解计划自己的 08:15 槽负荷比日前计划高出突增，不只是实际放电变大。"""
    day.execute(local("08:16"), start_at=local("07:30"))
    assert day.replans[-1].at == local("08:15")

    def load_kwh(plan):
        return next(e.load_kwh for e in plan.ledger.entries if e.interval.start == local("08:15"))
    assert load_kwh(day.current_plan) - load_kwh(day.plan) > 80  # 550 kW × 0.25 h = 137.5 kWh，扣除工序位置差异


def test_job_enters_plan_and_is_fixed_after_start(day):
    """B13 工序进日前计划（窗口内有开始动作），开始后每次重解固定不动，并按已执行进度只排剩余形状。"""
    op = day.packs.operations[0]
    starts = [a.interval.start for a in day.plan.plan.actions if a.quantity == "operation_start" and a.value > 0.5]
    assert len(starts) == 1 and local("07:00") <= starts[0] <= local("11:00")
    day.execute(local("09:00"), start_at=local("07:30"))
    start = day._job_start[op.operation_id]
    assert start == starts[0] or start >= local("07:30")
    assert day.current_plan.request.packs[-1] == "production"
    ops, fixed = day._remaining_operations(local("09:00"))
    assert fixed == {op.operation_id} and ops[0].original_start == local("09:00")
    assert len(ops[0].shape.fractions) == op.slots - int((local("09:00") - start) / timedelta(minutes=15))
    # 重解不改已开始工序的开始时刻；世界里每格都注入了工序负荷
    jobs = [n for n in day.trace.notes if n.get("kind") == "energy_job"]
    assert {n["started_at"] for n in jobs} == {start.isoformat()}
    assert [n["slot"] for n in jobs] == list(range(len(jobs)))
    assert day._slot_power[local("08:30")]["operation_kw"] == pytest.approx(
        op.profile_kw[int((local("08:30") - start) / timedelta(minutes=15))], rel=1e-3)
