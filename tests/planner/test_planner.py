"""B09 共享规划器：单模型单目标、贡献顺序无关、B08 账本口径、求解状态、规则编译与独立校验。"""

import sys
from datetime import timedelta
from pathlib import Path

import pyomo.environ as pyo
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_packs import FakeHvac, FakePv, FakeStorage, FakeTariff, HardKnapsack, Snooper  # noqa: E402

from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger  # noqa: E402
from plantkg.decision.planner import SharedPlanner, SolveOutcome, validate_solution  # noqa: E402
from plantkg.testing.fakes import fixture  # noqa: E402

DAY = yaml.safe_load((ROOT / "fixtures" / "planner" / "day_96.yaml").read_text(encoding="utf-8"))
BASE_REQ = fixture("planning_request")
CTX = BASE_REQ.context
H0 = BASE_REQ.horizon.start  # 2026-06-30T16:00Z = 南京 07-01 00:00
STEP = timedelta(minutes=15)


def prices():
    tbl = DAY["price_by_hour"]
    out = []
    for k in range(96):
        hour = k // 4
        out.append(next((v["cny_per_kwh"] for v in (tbl["valley"], tbl["peak"]) if hour in v["hours"]), tbl["flat_cny_per_kwh"]))
    return out


def forecast(fid, quantity, values):
    return c.Forecast(forecast_id=fid, quantity=quantity, unit="kW", issued_at=CTX.as_of,
                      target=c.Interval(start=H0, end=H0 + STEP * len(values)), step_minutes=15, values=tuple(values),
                      model_version="fake-1", input_slice=c.InputSlice(source_ids=("fixture",), visible_until=CTX.as_of), coverage=1.0)


FORECASTS = c.ForecastBundle(forecasts=(forecast("fc-pv", "pv_available", DAY["pv_available_kw"]),
                                        forecast("fc-load", "base_load", DAY["base_load_kw"])))
SNAP = fixture("state_snapshot")


def soc_rule(rule_id="rule-soc-floor", lower=0.2, **kw):
    data = dict(rule_id=rule_id, kind="fake_soc_floor", subject_ids=("entity://demo-electronics/battery/BAT1",), quantity="soc",
                unit="1", bounds={"lower": lower}, effective_from=H0 - timedelta(days=1), available_at=H0 - timedelta(days=1),
                config_ref="config://fixtures/planner/day_96.yaml#storage", nature="configured", review_status="trusted_config")
    data.update(kw)
    return c.Rule(**data)


def planner(packs=None, rules=(), **kw):
    packs = packs or default_packs()
    by_id = {r.rule_id: r for r in rules}
    return SharedPlanner({p.name: p for p in packs}, lambda _id: SNAP, lambda _ids: FORECASTS, lambda ids: [by_id[i] for i in ids], **kw)


def default_packs(**storage_kw):
    return [FakePv(), FakeStorage(DAY["storage"], **storage_kw), FakeTariff(prices())]


def request(packs=("pv", "storage", "tariff"), rule_ids=(), **budget):
    sb = {"time_limit_s": 20, "mip_rel_gap": 1e-4, "threads": 1, "random_seed": 0, **budget}
    return BASE_REQ.model_copy(update={"packs": tuple(packs), "rule_ids": tuple(rule_ids), "forecast_ids": ("fc-pv", "fc-load"),
                                       "solver_budget": c.SolverBudget(**sb)})


def series(plan, quantity):
    out = [0.0] * 96
    for a in plan.actions:
        if a.quantity == quantity:
            out[int((a.interval.start - H0) / STEP)] = a.value
    return out


# ---------------------------------------------------------------- 单模型、单目标、顺序无关


def test_one_model_one_objective_and_public_balance():
    pl = planner()
    result = pl.solve(request())
    assert result.solver_status == "optimal", result.validation_errors
    m = pl.last_builder.model
    assert len(list(m.component_objects(pyo.Objective, active=True))) == 1
    assert len(m.power_balance) == 96
    names = {v.name for v in m.component_objects(pyo.Var)}
    assert {"grid_import", "grid_export", "pv__use", "storage__charge"} <= names
    assert result.objective_terms == {"tariff.energy_cost": pytest.approx(result.objective_value)}


def test_contribution_order_does_not_change_problem():
    a = planner().solve(request(("pv", "storage", "tariff")))
    b = planner().solve(request(("tariff", "storage", "pv")))
    assert a.objective_value == pytest.approx(b.objective_value, abs=1e-6)
    for q in ("charge_power", "discharge_power", "pv_curtail"):
        assert sum(series(a.plan, q)) == pytest.approx(sum(series(b.plan, q)), abs=1e-3), q


def test_storage_shifts_energy_to_cheap_hours():
    with_bat = planner().solve(request())
    without = planner([FakePv(), FakeTariff(prices())]).solve(request(("pv", "tariff")))
    assert with_bat.objective_value < without.objective_value - 1.0
    charge, discharge = series(with_bat.plan, "charge_power"), series(with_bat.plan, "discharge_power")
    assert all(not (x > 1e-6 and y > 1e-6) for x, y in zip(charge, discharge))  # 互斥
    assert sum(discharge[k] for k in range(96) if k // 4 in DAY["price_by_hour"]["peak"]["hours"]) > 0


def test_new_pack_only_needs_its_own_module_and_assembly():
    base = planner().solve(request())
    more = planner(default_packs() + [FakeHvac()]).solve(request(("pv", "storage", "tariff", "hvac")))
    assert more.solver_status == "optimal"
    extra_energy = 40.0 * 24
    assert more.objective_value > base.objective_value + 0.3 * extra_energy * 0.9


def test_deterministic_plan_id_and_actions():
    a, b = planner().solve(request()), planner().solve(request())
    assert a.plan.plan_id == b.plan.plan_id
    assert [(x.action_id, round(x.value, 6)) for x in a.plan.actions] == [(x.action_id, round(x.value, 6)) for x in b.plan.actions]


# ---------------------------------------------------------------- B08 账本口径


def test_plan_passes_b08_ledger_with_objective():
    pl = planner()
    result = pl.solve(request())
    m, plan = pl.last_builder.model, result.plan
    charge, discharge, curtail = series(plan, "charge_power"), series(plan, "discharge_power"), series(plan, "pv_curtail")
    st = DAY["storage"]
    slots = [SlotInput(interval=c.Interval(start=H0 + k * STEP, end=H0 + (k + 1) * STEP), pv_available_kw=DAY["pv_available_kw"][k],
                       load_kw=DAY["base_load_kw"][k], charge_kw=charge[k], discharge_kw=discharge[k], pv_curtailed_kw=curtail[k],
                       price_cny_per_kwh=prices()[k], reported_grid_import_kw=pyo.value(m.grid_import[k]),
                       reported_energy_end_kwh=pyo.value(m.storage__energy[k])) for k in range(96)]
    storage = c.StorageParams(capacity_kwh=st["capacity_kwh"], eta_c=st["eta_c"], eta_d=st["eta_d"], soc_min=st["soc_min"], soc_max=st["soc_max"])
    meta = LedgerMeta("led-plan", "plan", CTX.run_id, plan.plan_id, plan.revision, CTX.version_manifest_id, ("fc-pv", "fc-load"))
    ledger = compute_ledger(slots, meta, storage, SNAP.pack_states["storage"]["energy_kwh"], objective_value_cny=result.objective_value)
    assert ledger.ledger.balanced, ledger.problems[:5]


# ---------------------------------------------------------------- 求解状态


def test_infeasible_returns_hints_and_no_plan():
    result = planner(default_packs(end_min_kwh=5000.0)).solve(request())  # 期末 5000 kWh > 容量
    assert result.solver_status == "infeasible" and result.plan is None
    assert any("storage" in h for h in result.conflict_hints)
    assert "不是 IIS" in result.conflict_hints[0]


def test_timeout_without_incumbent_has_no_plan():
    result = planner([HardKnapsack()]).solve(request(("hard",), time_limit_s=0.001, mip_rel_gap=1e-9))
    assert result.solver_status == "timeout" and result.plan is None and not result.validation_errors


def test_timeout_with_incumbent_is_feasible_with_gap():
    result = planner([HardKnapsack()]).solve(request(("hard",), time_limit_s=0.5, mip_rel_gap=1e-9))
    assert result.solver_status == "feasible" and result.plan is not None
    assert result.mip_gap is not None and result.mip_gap > 0
    assert result.objective_bound <= result.objective_value


class _Backend:
    name = "fake"

    def __init__(self, outcome):
        self.outcome = outcome

    def version(self):
        return "0"

    def solve(self, model, budget):
        return self.outcome


@pytest.mark.parametrize("termination, status", [("unbounded", "error"), ("infeasible_or_unbounded", "error"), ("error", "error"),
                                                 ("time_limit", "timeout")])
def test_status_mapping_without_solution(termination, status):
    result = planner(backend=_Backend(SolveOutcome(termination, False, None, None, 0.1, "x"))).solve(request())
    assert result.solver_status == status and result.plan is None


def test_solver_claiming_optimal_with_bad_values_is_error():
    """求解器说 optimal 但变量值违反约束（例如全 0 不满足功率平衡）：独立校验拦下，返回 error。"""
    class Zero(_Backend):
        def solve(self, model, budget):
            for v in model.component_data_objects(pyo.Var):
                v.set_value(0.0, skip_validation=True)
            return SolveOutcome("optimal", True, 0.0, 0.0, 0.1)

    result = planner(backend=Zero(None)).solve(request())
    assert result.solver_status == "error" and result.validation_errors and result.plan is None


def test_nonfinite_action_value_returns_error_not_contract_exception():
    """动作变量为 NaN：独立校验先拦成 error，不构造 Action（否则 pydantic 校验异常会穿出）。"""
    class NaNAction(_Backend):
        def solve(self, model, budget):
            for v in model.component_data_objects(pyo.Var):
                v.set_value(0.0, skip_validation=True)
            model.storage__charge[10].set_value(float("nan"), skip_validation=True)
            return SolveOutcome("optimal", True, 0.0, 0.0, 0.1)

    result = planner(backend=NaNAction(None)).solve(request())
    assert result.solver_status == "error" and result.plan is None
    assert any("非有限" in error for error in result.validation_errors)


def test_validate_solution_catches_fractional_binary_and_bounds():
    pl = planner()
    pl.solve(request())
    m = pl.last_builder.model
    m.storage__is_charging[10].set_value(0.5, skip_validation=True)
    m.storage__charge[11].set_value(400.0, skip_validation=True)
    errors = validate_solution(pl.last_builder)
    assert any("整数性" in e for e in errors) and any("超出" in e for e in errors)


# ---------------------------------------------------------------- 规则编译登记


def test_rule_compiled_into_pack_constraint_with_ref():
    pl = planner(rules=[soc_rule(lower=0.4)])
    result = pl.solve(request(rule_ids=("rule-soc-floor",)))
    assert result.solver_status == "optimal"
    m = pl.last_builder.model
    assert min(pyo.value(m.storage__energy[t]) for t in m.T) >= 400 - 1e-6
    assert c.Ref(kind="rule", id="rule-soc-floor") in pl.last_builder.state.constraints["storage__soc_floor_rule_soc_floor"].refs


def test_invisible_unreviewed_and_unknown_rules_become_pending():
    late = soc_rule("rule-late", available_at=CTX.as_of + timedelta(hours=1))
    draft = soc_rule("rule-draft", review_status="pending")
    unknown = soc_rule("rule-unknown", kind="no_such_kind")
    out_of_window = soc_rule("rule-old", effective_from=H0 - timedelta(days=9), effective_to=H0 - timedelta(days=5))
    result = planner(rules=[late, draft, unknown, out_of_window]).solve(request(rule_ids=("rule-late", "rule-draft", "rule-unknown", "rule-old")))
    reasons = {i.rule_id: str(i.reason) for i in result.pending_issues}
    assert reasons == {"rule-late": "not_visible", "rule-draft": "not_reviewed", "rule-unknown": "unsupported_kind"}
    assert result.solver_status == "optimal"


def test_declared_rule_constraint_missing_is_error():
    result = planner(default_packs(skip_rule_constraints=True), rules=[soc_rule()]).solve(request(rule_ids=("rule-soc-floor",)))
    assert result.solver_status == "error" and any("没有建进模型" in e for e in result.validation_errors)


# ---------------------------------------------------------------- 端口约束


def test_pack_cannot_touch_other_pack_variables():
    with pytest.raises(c.PlantkgError, match="不属于本包"):
        planner(default_packs() + [Snooper()]).solve(request(("pv", "storage", "tariff", "snoop")))


def test_balance_rejects_non_power_units():
    class BadUnit(FakeHvac):
        name = "bad"

        def contribute(self, mb, state, rules, forecasts):
            e = mb.add_var("stored", "continuous", (0, 10), c.Unit.kWh)
            mb.register_balance("demand", e, "stored_as_load")

    with pytest.raises(c.PlantkgError, match="而非 kW"):
        planner([BadUnit(name="bad")]).solve(request(("bad",)))


def test_constraint_requires_refs():
    class NoRef(FakeHvac):
        name = "noref"

        def contribute(self, mb, state, rules, forecasts):
            x = mb.add_var("x", "continuous", (0, 1), c.Unit.kW)
            mb.add_constraint("free", lambda t: x[t] <= 1, [])

    with pytest.raises(c.PlantkgError, match="Ref"):
        planner([NoRef(name="noref")]).solve(request(("noref",)))


def test_forecast_gap_is_not_planned_as_zero():
    holey = list(DAY["base_load_kw"])
    holey[5] = None
    bundle = c.ForecastBundle(forecasts=(forecast("fc-load", "base_load", holey),))
    pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [])
    with pytest.raises(c.PlantkgError, match="缺测"):
        pl.solve(request(("tariff",)))


def test_surplus_pv_without_storage_is_curtailed_not_exported():
    pl = planner([FakePv(), FakeTariff(prices())])
    result = pl.solve(request(("pv", "tariff")))
    m = pl.last_builder.model
    assert sum(series(result.plan, "pv_curtail")) > 0
    assert all(pyo.value(m.grid_export[t]) <= 1e-9 for t in m.T)  # 外送上限默认 0（07 Q16）


# ---------------------------------------------------------------- 对抗审查（Sonnet 子代理，2026-10-30）回归


def plan_ledger(result, prices_=None, base=None, export_limit=False):
    """只凭 Plan 动作与输入数据复算 B08 计划账本（下游 B17 能拿到的就是这些）。"""
    plan, st = result.plan, DAY["storage"]
    prices_ = prices_ or prices()
    base = base or DAY["base_load_kw"]
    q = {k: series(plan, k) for k in ("charge_power", "discharge_power", "pv_curtail", "grid_export")}
    slots = [SlotInput(interval=c.Interval(start=H0 + k * STEP, end=H0 + (k + 1) * STEP), pv_available_kw=DAY["pv_available_kw"][k],
                       load_kw=base[k], charge_kw=q["charge_power"][k], discharge_kw=q["discharge_power"][k],
                       grid_export_kw=q["grid_export"][k], pv_curtailed_kw=q["pv_curtail"][k], price_cny_per_kwh=prices_[k])
             for k in range(96)]
    storage = c.StorageParams(capacity_kwh=st["capacity_kwh"], eta_c=st["eta_c"], eta_d=st["eta_d"], soc_min=st["soc_min"], soc_max=st["soc_max"])
    meta = LedgerMeta("led-plan", "plan", CTX.run_id, plan.plan_id, plan.revision, CTX.version_manifest_id, ("fc-pv", "fc-load"))
    return compute_ledger(slots, meta, storage, SNAP.pack_states["storage"]["energy_kwh"], objective_value_cny=result.objective_value)


def test_thread_count_change_in_same_process_still_solves():
    a = planner().solve(request(threads=1))
    b = planner().solve(request(threads=2))
    assert a.solver_status == b.solver_status == "optimal"
    assert b.solver.params["threads_effective"] == a.solver.params["threads_effective"]


@pytest.mark.parametrize("bad", [
    lambda f: f.model_copy(update={"target": c.Interval(start=H0 - timedelta(minutes=5), end=H0 + STEP * 96 - timedelta(minutes=5))}),
    lambda f: f.model_copy(update={"unit": "kWh"}),
])
def test_forecast_misaligned_or_wrong_unit_rejected(bad):
    load = bad(FORECASTS.forecasts[1])
    bundle = c.ForecastBundle(forecasts=(FORECASTS.forecasts[0], load))
    pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [])
    with pytest.raises(c.PlantkgError):
        pl.solve(request())


def test_missing_base_load_rejected_and_unrequested_forecast_ignored():
    pv_only = c.ForecastBundle(forecasts=(FORECASTS.forecasts[0],))
    with pytest.raises(c.PlantkgError, match="base_load"):
        SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: pv_only, lambda ids: []).solve(request())
    extra = forecast("fc-load-other", "base_load", [v + 50 for v in DAY["base_load_kw"]])
    bundle = c.ForecastBundle(forecasts=FORECASTS.forecasts + (extra,))
    pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [])
    assert pl.solve(request()).objective_value == pytest.approx(planner().solve(request()).objective_value, abs=1e-6)


def test_rule_constraint_that_skips_every_slot_is_error():
    class SkipAll(FakeStorage):
        def contribute(self, mb, state, rules, forecasts):
            out = FakeStorage(self.params, skip_rule_constraints=True).contribute(mb, state, [], forecasts)
            for rule in rules:
                mb.add_constraint("soc_floor_" + rule.rule_id.replace("-", "_"), lambda t: pyo.Constraint.Skip, [c.Ref(kind="rule", id=rule.rule_id)])
            return out

    result = planner([FakePv(), SkipAll(DAY["storage"]), FakeTariff(prices())], rules=[soc_rule(lower=0.85)]).solve(
        request(rule_ids=("rule-soc-floor",)))
    assert result.solver_status == "error" and any("全部跳过" in e for e in result.validation_errors)


@pytest.mark.parametrize("hack", [
    lambda mb: mb.model.add_component("hack", pyo.Constraint(expr=mb.grid_import[0] <= 1)),
    lambda mb: mb.grid_export.setub(500),
    lambda mb: mb.grid_import[3].fix(0),
])
def test_bypassing_builder_is_caught(hack):
    class Bypass(FakeHvac):
        def contribute(self, mb, state, rules, forecasts):
            hack(mb)
            return super().contribute(mb, state, rules, forecasts)

    with pytest.raises(c.PlantkgError, match="绕过|公共购/售电"):
        planner(default_packs() + [Bypass(name="bypass")]).solve(request(("pv", "storage", "tariff", "bypass")))


def test_pack_exception_names_the_pack():
    class Broken(FakeHvac):
        def contribute(self, mb, state, rules, forecasts):
            mb.add_constraint("const", lambda t: 7.0 <= 5, [c.Ref(kind="config", id="config://x#y")])

    with pytest.raises(c.PlantkgError, match="包 broken"):
        planner([Broken(name="broken")]).solve(request(("broken",)))


def test_export_appears_in_plan_and_ledger_balances_from_plan_only():
    low = [30.0] * 96
    bundle = c.ForecastBundle(forecasts=(FORECASTS.forecasts[0], forecast("fc-load", "base_load", low)))
    pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [], export_limit_kw=200.0)
    result = pl.solve(request())
    assert sum(series(result.plan, "grid_export")) > 0
    led = plan_ledger(result, base=low)
    assert led.ledger.balanced, led.problems[:3]


def test_zero_price_slots_do_not_curtail_while_importing():
    zero = [0.0 if k >= 48 else 0.6 for k in range(96)]
    result = planner([FakePv(), FakeStorage(DAY["storage"]), FakeTariff(zero)]).solve(request())
    led = plan_ledger(result, prices_=zero)
    assert led.ledger.balanced, led.problems[:3]
    assert "planner.import_tiebreak" not in result.objective_terms


def test_plan_id_changes_with_rule_content_and_export_limit():
    a = planner(rules=[soc_rule(lower=0.2)]).solve(request(rule_ids=("rule-soc-floor",)))
    b = planner(rules=[soc_rule(lower=0.45, rule_version=2)]).solve(request(rule_ids=("rule-soc-floor",)))
    e = planner(export_limit_kw=300.0).solve(request())
    assert len({a.plan.plan_id, b.plan.plan_id, e.plan.plan_id, planner().solve(request()).plan.plan_id}) == 4


def test_unreviewed_rule_outside_horizon_is_just_skipped():
    old = soc_rule("rule-old-draft", review_status="pending", effective_from=H0 - timedelta(days=9), effective_to=H0 - timedelta(days=5))
    result = planner(rules=[old]).solve(request(rule_ids=("rule-old-draft",)))
    assert result.pending_issues == ()


def test_infeasible_diagnosis_respects_budget_and_reports_elapsed():
    import time as _time
    packs = default_packs(end_min_kwh=5000.0) + [HardKnapsack(n=400, name="hard1"), HardKnapsack(n=400, seed=5, name="hard2")]
    start = _time.perf_counter()
    result = planner(packs).solve(request(("pv", "storage", "tariff", "hard1", "hard2"), time_limit_s=1.0))
    wall = _time.perf_counter() - start
    assert result.solver_status == "infeasible"
    assert wall < 1.0 + 0.5 + 1.0  # 求解 ≤ 1 s + 诊断 ≤ 0.5 s + 建模开销
    assert result.elapsed_s >= wall * 0.5


# ---------------------------------------------------------------- PR #7 复审（goodluck55k）回归


def test_two_background_requests_in_parallel_are_correct_and_do_not_hang():
    import threading
    expected = planner().solve(request()).objective_value
    results, errors = [], []

    def go():
        try:
            results.append(planner().solve(request()))
        except Exception as exc:  # noqa: BLE001 - 测试要看到任何异常
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "并发求解挂起"
    assert not errors and len(results) == 2
    assert all(r.solver_status == "optimal" and r.objective_value == pytest.approx(expected, abs=1e-6) for r in results)


class _Starts(FakeHvac):
    """工序样式的包：只为候选开始时段建 x[s]，Σx = 1，开始后 4 段各 20 kW。"""

    def __init__(self, starts=None, scalar=False, name="job"):
        super().__init__(name=name)
        self.starts, self.scalar = starts, scalar

    def contribute(self, mb, state, rules, forecasts):
        ref = [c.Ref(kind="config", id="config://fixtures/planner/day_96.yaml#job")]
        job = "entity://demo-electronics/operation/JOB1"
        if self.scalar:
            x = mb.add_var("run", "binary", (0, 1), c.Unit.fraction, scalar=True, action=(job, "operation_run"))
            mb.add_constraint("once", x == 1, ref)
            mb.register_balance("demand", [20.0 * x] * len(mb.time_set), "job_load")
            return super().contribute(mb, state, rules, forecasts)
        x = mb.add_var("start", "binary", (0, 1), c.Unit.fraction, index=self.starts, action=(job, "operation_start"))
        mb.add_constraint("once", sum(x.values()) == 1, ref)
        load = [sum(20.0 * x[s] for s in x if s <= t < s + 4) for t in range(len(mb.time_set))]
        mb.register_balance("demand", load, "job_load")
        return super().contribute(mb, state, rules, forecasts)


def test_empty_candidate_index_is_rejected_not_scalar():
    with pytest.raises(c.PlantkgError, match="候选下标为空"):
        planner([FakeTariff(prices()), _Starts([])]).solve(request(("tariff", "job")))


@pytest.mark.parametrize("starts, scalar", [([40], False), ([8, 40, 60], False), (None, True)])
def test_single_sparse_and_scalar_variables(starts, scalar):
    result = planner([FakeTariff(prices()), _Starts(starts, scalar)]).solve(request(("tariff", "job")))
    assert result.solver_status == "optimal"
    acts = [a for a in result.plan.actions if a.quantity.startswith("operation")]
    assert len(acts) == 1
    if scalar:
        assert acts[0].interval == request().horizon
    else:
        assert int((acts[0].interval.start - H0) / STEP) in starts
        assert acts[0].interval.end - acts[0].interval.start == STEP
        if len(starts) > 1:
            assert int((acts[0].interval.start - H0) / STEP) == 8  # 谷价时段最便宜


def test_secondary_objective_never_trades_away_primary_cost():
    """复审的复现：x=0 时费用 0；x=1 时少购电但多 0.05 元。原费用优先，必须选 x=0，且下界/gap 自洽。"""
    class Choice(FakeHvac):
        def contribute(self, mb, state, rules, forecasts):
            x = mb.add_var("x", "binary", (0, 1), c.Unit.fraction, scalar=True)
            mb.register_balance("supply", [1000.0 * x] * len(mb.time_set), "self_gen")
            mb.register_cost(0.05 * x, "self_gen_cost")
            return super().contribute(mb, state, rules, forecasts)

    flat = c.ForecastBundle(forecasts=(forecast("fc-load", "base_load", [1000.0] * 96),))
    pl = SharedPlanner({"choice": Choice(name="choice")}, lambda _id: SNAP, lambda _ids: flat, lambda ids: [])
    result = pl.solve(request(("choice",)).model_copy(update={"forecast_ids": ("fc-load",)}))
    assert result.solver_status == "optimal"
    assert pyo.value(pl.last_builder.model.choice__x) == 0
    assert result.objective_value == pytest.approx(0.0, abs=1e-9)
    assert result.objective_bound <= result.objective_value + 1e-9


def test_feasible_timeout_metrics_are_consistent():
    result = planner([HardKnapsack()]).solve(request(("hard",), time_limit_s=0.5, mip_rel_gap=1e-9))
    assert result.solver_status == "feasible"
    obj, bound = result.objective_value, result.objective_bound
    assert bound <= obj
    assert result.mip_gap == pytest.approx((obj - bound) / abs(obj), rel=1e-9)
    # #195：墙钟是记录值，并行负载下会超过预算；这里验证预算被尊重（没有跑到无界）与指标自洽，
    # 不断言紧墙钟阈值（0.8 s 在 xdist 并行时偶发 1.0 s+）。
    assert 0.0 <= result.elapsed_s <= 5.0


def test_missing_bound_with_incumbent_is_not_faked():
    class NoBound(_Backend):
        def solve(self, model, budget):
            for v in model.component_data_objects(pyo.Var):
                v.set_value(0.0, skip_validation=True)
            return SolveOutcome("time_limit", True, 0.0, None, 0.1)

    result = planner(backend=NoBound(None)).solve(request())
    assert result.solver_status == "error" and result.plan is None
    assert any("下界" in e for e in result.validation_errors)


def test_requested_rule_not_returned_is_not_silently_dropped():
    pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: FORECASTS, lambda ids: [])
    with pytest.raises(c.PlantkgError, match="未取回"):
        pl.solve(request(rule_ids=("rule-soc-floor",)))


def test_unrequested_or_duplicate_rule_records_rejected():
    rule = soc_rule()
    for returned in ([rule, rule], [rule, soc_rule("rule-extra")]):
        pl = SharedPlanner({p.name: p for p in default_packs()}, lambda _id: SNAP, lambda _ids: FORECASTS, lambda ids, r=returned: r)
        with pytest.raises(c.PlantkgError, match="未请求或重复"):
            pl.solve(request(rule_ids=("rule-soc-floor",)))


# ---------------------------------------------------------------- PR #7 第二轮复审回归


class _Cost(FakeHvac):
    def __init__(self, kind, name="cost"):
        super().__init__(name=name)
        self.kind = kind

    def contribute(self, mb, state, rules, forecasts):
        if self.kind == "constant":
            mb.register_cost(5.0, "fixed_fee")
        elif self.kind == "variable":
            h = 0.25
            mb.register_cost(lambda t: 0.5 * mb.grid_import[t] * h, "energy")
        return super().contribute(mb, state, rules, forecasts)


@pytest.mark.parametrize("kind, expected", [("none", 0.0), ("constant", 5.0), ("variable", None)])
def test_no_cost_constant_cost_and_variable_cost_all_return_plans(kind, expected):
    result = planner([_Cost(kind)]).solve(request(("cost",)))
    assert result.solver_status == "optimal" and result.plan is not None
    if expected is not None:
        assert result.objective_value == pytest.approx(expected)
    else:
        energy = sum(v + 40.0 for v in DAY["base_load_kw"]) * 0.25
        assert result.objective_value == pytest.approx(0.5 * energy, rel=1e-6)
    assert result.objective_bound <= result.objective_value + 1e-9


ZERO = [0.0] * 96


def _assert_plan_passes_ledger_or_fails_clearly(result, prices_):
    if result.plan is not None:
        led = plan_ledger(result, prices_=prices_)
        assert led.ledger.balanced, led.problems[:3]
    else:
        assert result.solver_status in ("error", "timeout")


@pytest.mark.parametrize("packs, names", [(lambda: [FakePv(), FakeTariff(ZERO)], ("pv", "tariff")),
                                          (lambda: [FakePv(), FakeStorage(DAY["storage"]), FakeTariff(ZERO)], ("pv", "storage", "tariff"))])
def test_zero_price_short_budget_returns_ledger_consistent_plan(packs, names):
    """复审原复现：零电价、预算 0.02～0.04 s。第一阶段用完预算后第二阶段仍会精修，必须给出过 B08 的计划，不能靠 error 过关。"""
    for limit in (0.02, 0.03, 0.04):
        result = planner(packs()).solve(request(names, time_limit_s=limit))
        assert result.solver_status in ("optimal", "feasible") and result.solver.params["stage2"] == "ok", (limit, result.validation_errors)
        led = plan_ledger(result, prices_=ZERO)
        assert led.ledger.balanced, (limit, led.problems[:3])


def test_zero_price_when_stage_two_fails_falls_back_but_never_returns_bad_plan():
    from plantkg.decision.planner import HighsBackend

    class StageTwoFails(HighsBackend):
        calls = 0

        def solve(self, model, budget):
            StageTwoFails.calls += 1
            if StageTwoFails.calls >= 2:
                return SolveOutcome("time_limit", False, None, None, 0.0, "forced")
            return super().solve(model, budget)

    result = planner([FakePv(), FakeTariff(ZERO)], backend=StageTwoFails()).solve(request(("pv", "tariff")))
    _assert_plan_passes_ledger_or_fails_clearly(result, ZERO)
    assert result.solver.params["stage2"] == "no_solution" or result.plan is None
    if result.plan is None:
        assert any("B08" in e for e in result.validation_errors)


def test_ledger_rule_check_rejects_curtail_while_importing():
    from plantkg.decision.planner.planner import _ledger_rules

    pl = planner([FakePv(), FakeTariff(prices())])
    pl.solve(request(("pv", "tariff")))
    b = pl.last_builder
    b.model.grid_import[30].set_value(80.0)
    curtail = c.Action(action_id="pv__curtail[30]", object="entity://demo-electronics/pv/PV1", quantity="pv_curtail", unit="kW",
                       value=50.0, interval=b.time_set[30], refs=(c.Ref(kind="config", id="config://x#y"),))
    assert any("时段 30" in e for e in _ledger_rules([curtail], b, 0.25))
    assert _ledger_rules([], b, 0.25) == []


def test_curtail_variable_without_value_does_not_crash_ledger_check():
    """夜间 conservation 被 Skip 时 curtail[t] 无取值：返回明确 error，不抛未处理异常。"""
    class NightSkipPv(FakePv):
        def contribute(self, mb, state, rules, forecasts):
            avail = mb.pv_available
            use = mb.add_var("use", "continuous", (0, None), c.Unit.kW)
            cut = mb.add_var("curtail", "continuous", (0, None), c.Unit.kW, action=("entity://demo-electronics/pv/PV1", "pv_curtail"))
            mb.add_constraint("conservation", lambda t: use[t] + cut[t] == avail[t] if avail[t] > 0 else pyo.Constraint.Skip,
                              [c.Ref(kind="config", id="config://x#pv")])
            mb.register_balance("supply", lambda t: use[t] if avail[t] > 0 else 0.0, "pv_used")
            return receipt_of(mb, self.name)

    result = planner([NightSkipPv(), FakeTariff(prices())]).solve(request(("pv", "tariff")))
    assert result.solver_status == "error" and any("未给取值" in e for e in result.validation_errors)


def receipt_of(mb, name):
    from fake_packs import receipt
    return receipt(name, mb)


def test_mip_with_constant_cost_and_pack_fixed_integer_kept_fixed():
    class Fixed(FakeHvac):
        def contribute(self, mb, state, rules, forecasts):
            flag = mb.add_var("forced", "binary", (0, 1), c.Unit.fraction, scalar=True)
            flag.fix(1)
            free = mb.add_var("free", "binary", (0, 1), c.Unit.fraction)
            mb.add_constraint("use_free", lambda t: free[t] <= flag, [c.Ref(kind="config", id="config://x#f")])
            mb.register_balance("demand", lambda t: 10.0 * free[t], "free_load")
            mb.register_cost(3.0, "fixed_fee")
            return super().contribute(mb, state, rules, forecasts)

    pl = planner([Fixed(name="fixed")])
    result = pl.solve(request(("fixed",)))
    assert result.solver_status == "optimal" and result.objective_value == pytest.approx(3.0)
    assert pl.last_builder.model.fixed__forced.fixed
    assert not any(v.fixed for v in pl.last_builder.model.fixed__free.values())
