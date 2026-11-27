"""B08 独立电量账本：手算核对、反例、平移守恒、复核与随机一致性。"""

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.ledger import (  # noqa: E402
    LedgerMeta,
    ShiftedOperation,
    SlotInput,
    compute_ledger,
    ledger_summary,
    replace_shifted_load,
    validate_balance,
)

FIXTURE = yaml.safe_load((ROOT / "fixtures" / "ledger" / "hand_calc_three_slots.yaml").read_text(encoding="utf-8"))
STORAGE = c.StorageParams(**FIXTURE["storage"])
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


def meta(kind="plan"):
    return LedgerMeta("led-1", kind, "run-1", "plan-1", 1, "vm-1", ("fc-1",))


def slots_from(rows, start=T0, step=STEP):
    return [SlotInput(interval=c.Interval(start=start + i * step, end=start + (i + 1) * step), **row) for i, row in enumerate(rows)]


def hand_calc(**kw):
    return compute_ledger(slots_from(FIXTURE["slots"]), meta(), STORAGE, FIXTURE["energy_start_kwh"], **kw)


# ---------------------------------------------------------------- 手算


def test_hand_calculated_three_slots():
    result = hand_calc()
    led = result.ledger
    assert led.balanced and not result.problems
    for entry, want in zip(led.entries, FIXTURE["expected"]["per_slot"], strict=True):
        for key, value in want.items():
            assert getattr(entry, key) == pytest.approx(value, abs=1e-9), key
    exp = FIXTURE["expected"]
    assert led.total_cost_cny == pytest.approx(exp["total_cost_cny"])
    assert led.peak_import_kw == pytest.approx(exp["peak_import_kw"])
    assert result.energy_kwh["grid_import"] == pytest.approx(exp["grid_import_kwh"])
    assert led.entries[-1].soc_end == pytest.approx(exp["soc_end"])
    assert validate_balance(led) == [] and led.recheck() == []


def test_plan_ledger_matches_objective_within_tolerance():
    assert hand_calc(objective_value_cny=67.5 + 0.005).ledger.balanced
    off = hand_calc(objective_value_cny=70.0)
    assert not off.ledger.balanced and any("模型目标" in p for p in off.problems)


def test_plan_ledger_balances_purchase_plus_nonpurchase_terms():
    """模型目标含非购电成本项时，账本按“购电费用 + 另列成本项”核对，不再把合法 optimal 判成不平。"""
    result = hand_calc(objective_value_cny=72.5, objective_terms={"storage.degradation": 5.0})
    led = result.ledger
    assert led.balanced, result.problems
    assert led.total_cost_cny == pytest.approx(67.5)
    assert led.objective_value_cny == pytest.approx(72.5)
    assert led.objective_terms == {"storage.degradation": 5.0}
    assert led.objective_gap_cny == pytest.approx(0.0, abs=1e-9)
    assert validate_balance(led) == []
    mismatched = hand_calc(objective_value_cny=67.5, objective_terms={"storage.degradation": 5.0})
    assert not mismatched.ledger.balanced and any("模型目标" in p for p in mismatched.problems)


def test_tolerance_band_negative_power_is_clipped_before_energy():
    result = run([{"pv_available_kw": -1e-9, "load_kw": -1e-9, "price_cny_per_kwh": 0.5}])
    assert result.ledger.balanced, result.problems
    assert result.ledger.entries[0].load_kwh == 0.0 and result.ledger.entries[0].pv_available_kwh == 0.0


def test_negative_power_beyond_tolerance_is_rejected():
    with pytest.raises(c.PlantkgError) as exc:
        run([{"pv_available_kw": 0, "load_kw": -1e-3, "price_cny_per_kwh": 0.5}])
    assert exc.value.code == c.ErrorCode.DOMAIN_INVALID


def test_execution_ledger_does_not_compare_objective():
    result = compute_ledger(slots_from(FIXTURE["slots"]), meta("execution"), STORAGE, 500, objective_value_cny=1.0)
    assert result.ledger.balanced  # 执行与计划之差是预测偏差，不算错


# ---------------------------------------------------------------- 反例：必须判为不平


def run(rows, storage=STORAGE, e0=500.0):
    return compute_ledger(slots_from(rows), meta(), storage, e0)


def test_simultaneous_charge_and_discharge_fails():
    result = run([{"pv_available_kw": 0, "load_kw": 100, "charge_kw": 100, "discharge_kw": 50, "price_cny_per_kwh": 0.5}])
    assert not result.ledger.balanced and any("同时充放" in p for p in result.problems)


def test_soc_out_of_bounds_fails():
    rows = [{"pv_available_kw": 0, "load_kw": 0, "charge_kw": 250, "price_cny_per_kwh": 0.3}] * 4
    result = run(rows, e0=850)  # 850 + 4 × 0.95 × 62.5 = 1087.5 > 900
    assert not result.ledger.balanced and any("SOC" in p for p in result.problems)


def test_reported_import_inconsistent_fails():
    rows = [{**FIXTURE["slots"][0], "reported_grid_import_kw": 120}]  # 重算应为 100
    result = run(rows)
    assert not result.ledger.balanced and any("残差" in p for p in result.problems)


def test_reported_import_consistent_passes():
    assert run([{**FIXTURE["slots"][0], "reported_grid_import_kw": 100}]).ledger.balanced


def test_supply_surplus_fails():
    result = run([{"pv_available_kw": 0, "load_kw": 50, "discharge_kw": 250, "price_cny_per_kwh": 0.5}])
    assert not result.ledger.balanced and any("供给超过需求" in p for p in result.problems)


def test_missing_price_is_not_zero():
    rows = [FIXTURE["slots"][0], {**FIXTURE["slots"][1], "price_cny_per_kwh": None}]
    result = run(rows)
    assert result.ledger.total_cost_cny is None and not result.ledger.balanced
    assert result.ledger.entries[1].cost_cny is None


def test_curtailment_beyond_available_fails():
    result = run([{"pv_available_kw": 100, "load_kw": 100, "pv_curtailed_kw": 150, "price_cny_per_kwh": 0.5}])
    assert not result.ledger.balanced


def test_charge_without_storage_fails():
    result = compute_ledger(slots_from([FIXTURE["slots"][0]]), meta(), None, 0.0)
    assert not result.ledger.balanced and any("没有储能" in p for p in result.problems)


@pytest.mark.parametrize("bad", [{"load_kw": float("nan")}, {"load_kw": -5.0}, {"price_cny_per_kwh": float("inf")}])
def test_invalid_inputs_rejected(bad):
    with pytest.raises(c.PlantkgError):
        run([{**FIXTURE["slots"][0], **bad}])


def test_non_contiguous_or_mixed_steps_rejected():
    a = SlotInput(interval=c.Interval(start=T0, end=T0 + STEP), pv_available_kw=0, load_kw=1, price_cny_per_kwh=1)
    gap = SlotInput(interval=c.Interval(start=T0 + 2 * STEP, end=T0 + 3 * STEP), pv_available_kw=0, load_kw=1, price_cny_per_kwh=1)
    longer = SlotInput(interval=c.Interval(start=T0 + STEP, end=T0 + 3 * STEP), pv_available_kw=0, load_kw=1, price_cny_per_kwh=1)
    for slots in ([a, gap], [a, longer]):
        with pytest.raises(c.PlantkgError):
            compute_ledger(slots, meta(), None)


def test_hourly_slots_use_hours_not_slot_count():
    hourly = slots_from([{"pv_available_kw": 0, "load_kw": 100, "price_cny_per_kwh": 1.0}], step=timedelta(hours=1))
    led = compute_ledger(hourly, meta(), None).ledger
    assert led.entries[0].grid_import_kwh == pytest.approx(100) and led.total_cost_cny == pytest.approx(100)


# ---------------------------------------------------------------- 复核已有账本


def test_validate_balance_catches_tampered_cost_and_peak():
    led = hand_calc().ledger
    entries = list(led.entries)
    entries[0] = entries[0].model_copy(update={"cost_cny": 1.0})
    tampered = led.model_copy(update={"entries": tuple(entries), "peak_import_kw": 10.0})
    problems = validate_balance(tampered)
    assert any("费用" in p for p in problems) and any("峰值" in p for p in problems)


def test_contract_rejects_balanced_flag_on_bad_ledger():
    bad = run([{"pv_available_kw": 0, "load_kw": 100, "charge_kw": 100, "discharge_kw": 50, "price_cny_per_kwh": 0.5}]).ledger
    with pytest.raises(ValueError):
        c.Ledger.model_validate({**bad.model_dump(), "balanced": True, "violations": ()})


# ---------------------------------------------------------------- 工序平移


def test_shift_conserves_total_and_operation_energy():
    total = [100.0, 180.0, 180.0, 100.0, 100.0, 100.0]
    op = ShiftedOperation("JOB1", (80.0, 80.0), original_start=1, new_start=4)
    report = replace_shifted_load(total, [op], slot_hours=0.25)
    assert report.residual_kw == (100.0, 100.0, 100.0, 100.0, 100.0, 100.0)
    assert report.total_kw == (100.0, 100.0, 100.0, 100.0, 180.0, 180.0)
    assert report.energy_before_kwh == pytest.approx(report.energy_after_kwh)
    assert report.operation_energy_kwh["JOB1"] == pytest.approx(40.0)


def test_shift_to_same_slot_is_identity():
    total = [100.0, 180.0, 100.0]
    report = replace_shifted_load(total, [ShiftedOperation("J", (80.0,), 1, 1)], 0.25)
    assert report.total_kw == tuple(total)


def test_double_counting_rejected():
    """分表曲线大于总表（分表不属于总表或已被扣过）→ 负残余 → 拒绝。"""
    with pytest.raises(c.PlantkgError, match="重复计量"):
        replace_shifted_load([100.0, 100.0, 100.0], [ShiftedOperation("J", (150.0,), 1, 2)], 0.25)
    two = [ShiftedOperation("A", (60.0,), 1, 0), ShiftedOperation("B", (60.0,), 1, 2)]  # 同一时段被两道工序重复扣
    with pytest.raises(c.PlantkgError, match="重复计量"):
        replace_shifted_load([100.0, 100.0, 100.0], two, 0.25)


@pytest.mark.parametrize("op", [ShiftedOperation("J", (10.0, 10.0), 0, 2), ShiftedOperation("J", (10.0,), -1, 0)])
def test_shift_out_of_horizon_rejected(op):
    with pytest.raises(c.PlantkgError, match="放不进"):
        replace_shifted_load([50.0, 50.0, 50.0], [op], 0.25)


def test_same_operation_twice_rejected():
    op = ShiftedOperation("J", (10.0,), 0, 1)
    with pytest.raises(c.PlantkgError, match="两次"):
        replace_shifted_load([50.0, 50.0], [op, op], 0.25)


def test_shifted_load_feeds_ledger_with_same_energy():
    total = [100.0, 180.0, 180.0, 100.0]
    report = replace_shifted_load(total, [ShiftedOperation("J", (80.0, 80.0), 1, 2)], 0.25)
    rows = [{"pv_available_kw": 0, "load_kw": v, "price_cny_per_kwh": 0.5} for v in report.total_kw]
    led = run(rows, storage=None, e0=0).ledger
    assert sum(e.load_kwh for e in led.entries) == pytest.approx(sum(total) * 0.25)


# ---------------------------------------------------------------- B0/B1/B2 同口径、随机一致性


def test_policies_use_same_accounting():
    """B0 不调度、B1 只调储能、B2 储能加平移：同一函数核算，各自都能被独立复核通过。"""
    base = [{"pv_available_kw": 300, "load_kw": 250, "price_cny_per_kwh": 0.3}, {"pv_available_kw": 0, "load_kw": 400, "price_cny_per_kwh": 1.2}]
    b0 = run(base)
    b1 = run([{**base[0], "charge_kw": 50}, {**base[1], "discharge_kw": 100}])
    for result in (b0, b1):
        assert result.ledger.balanced and validate_balance(result.ledger) == []
    assert b1.ledger.total_cost_cny < b0.ledger.total_cost_cny


def test_random_consistent_dispatch_always_balances():
    rng = random.Random(20261030)
    for _ in range(200):
        rows, energy = [], 500.0
        for _ in range(8):
            pv, load = rng.uniform(0, 500), rng.uniform(50, 600)
            mode = rng.choice(("idle", "charge", "discharge"))
            charge = rng.uniform(0, min(250, (900 - energy) / 0.95 / 0.25)) if mode == "charge" else 0.0
            discharge = rng.uniform(0, min(250, load, (energy - 100) * 0.95 / 0.25)) if mode == "discharge" else 0.0
            energy += 0.95 * charge * 0.25 - discharge * 0.25 / 0.95
            rows.append({"pv_available_kw": pv, "load_kw": load, "charge_kw": charge, "discharge_kw": discharge,
                         "price_cny_per_kwh": rng.choice((0.3, 0.6, 1.2))})
        result = run(rows)
        assert result.ledger.balanced, result.problems
        assert validate_balance(result.ledger) == [] and result.ledger.recheck() == []


# ---------------------------------------------------------------- 对抗审查（deepseek-flash，2026-10-30）回归


@pytest.mark.parametrize("e0", [-100.0, float("nan"), 1200.0])
def test_invalid_initial_energy_rejected_not_crash(e0):
    with pytest.raises(c.PlantkgError):
        run([{"pv_available_kw": 0, "load_kw": 900, "charge_kw": 900, "price_cny_per_kwh": 0.5}], e0=e0)


def test_initial_soc_below_min_is_unbalanced_not_crash():
    result = run([{"pv_available_kw": 0, "load_kw": 100, "price_cny_per_kwh": 0.5}], e0=50)  # SOC 0.05 < 0.1
    assert not result.ledger.balanced


def test_curtail_while_importing_is_unbalanced():
    result = run([{"pv_available_kw": 600, "load_kw": 300, "discharge_kw": 100, "pv_curtailed_kw": 600, "price_cny_per_kwh": 0.5}])
    assert not result.ledger.balanced and any("同时购电" in p for p in result.problems)


def test_curtail_with_discharge_from_execution_cut_is_allowed():
    # 执行削减：光伏 300 弃 100 后与放电 100 一起正好供 300 负荷，不购电
    result = run([{"pv_available_kw": 300, "load_kw": 300, "discharge_kw": 100, "pv_curtailed_kw": 100, "price_cny_per_kwh": 0.5}])
    assert result.ledger.balanced, result.problems


def test_shift_tiny_negative_residuals_do_not_trip_conservation():
    total = [100.0] * 8
    ops = [ShiftedOperation(f"J{i}", (100.003,), i, i) for i in range(8)]  # 每段 −0.00075 kWh，在逐段容差内
    report = replace_shifted_load(total, ops, 0.25)
    assert len(report.total_kw) == 8


@pytest.mark.parametrize("hours", [0.0, -0.25, float("nan")])
def test_shift_requires_positive_slot_hours(hours):
    with pytest.raises(c.PlantkgError):
        replace_shifted_load([100.0], [ShiftedOperation("J", (150.0,), 0, 0)], hours)


def test_inverted_soc_bounds_rejected():
    bad = c.StorageParams(capacity_kwh=1000, eta_c=0.95, eta_d=0.95, soc_min=0.9, soc_max=0.1)
    with pytest.raises(c.PlantkgError, match="SOC 下限"):
        run([FIXTURE["slots"][0]], storage=bad)


# ---------------------------------------------------------------- 第二轮审查（衔接与规模）回归


def test_rounding_curtailment_is_not_false_alarm():
    # 计划取整：弃光 300.001、购电 0.001 kW（0.00025 kWh），在电量容差内，不应判不平
    result = run([{"pv_available_kw": 600, "load_kw": 300, "pv_curtailed_kw": 300.001, "price_cny_per_kwh": 0.5}])
    assert result.ledger.balanced, result.problems


def test_validate_balance_rechecks_objective():
    led = hand_calc(objective_value_cny=67.5).ledger
    assert validate_balance(led, objective_value_cny=67.5) == []
    assert any("模型目标" in p for p in validate_balance(led, objective_value_cny=500.0))
    forged = led.model_copy(update={"objective_gap_cny": 0.0})
    assert any("objective_gap_cny" in p for p in validate_balance(forged, objective_value_cny=60.0))


def test_reported_energy_end_is_checked():
    ok = {**FIXTURE["slots"][0], "reported_energy_end_kwh": 547.5}
    bad = {**FIXTURE["slots"][0], "reported_energy_end_kwh": 560.0}
    assert run([ok]).ledger.balanced
    assert any("期末电量" in p for p in run([bad]).problems)


@pytest.mark.parametrize("bad_meta", [LedgerMeta("l", "Plan", "r", "p", 1, "vm", ("x",)), LedgerMeta("l", "plan", "r", "p", 1, "vm", ())])
def test_meta_validated_up_front(bad_meta):
    with pytest.raises(c.PlantkgError):
        compute_ledger(slots_from(FIXTURE["slots"]), bad_meta, STORAGE, 500)


def test_week_scale_numerically_stable():
    """一周 672 段、负荷约 1 MW、储能 1 MWh：充放往返后 SOC、费用与守恒不因浮点累积误判。"""
    rows = []
    for k in range(672):
        phase = k % 96
        charge = 200.0 if 8 <= phase < 20 else 0.0  # 12 段 × 200 × 0.25 × 0.95 = 570 kWh
        discharge = 180.5 if 40 <= phase < 52 else 0.0  # 12 段 × 180.5 × 0.25 / 0.95 = 570 kWh，每天回到原点
        rows.append({"pv_available_kw": 400.0 if 36 <= phase < 68 else 0.0, "load_kw": 1000.0 + (phase % 7),
                     "charge_kw": charge, "discharge_kw": discharge, "price_cny_per_kwh": (0.3, 0.6, 1.2)[phase % 3]})
    result = run(rows, e0=300)
    assert result.ledger.balanced, result.problems[:3]
    assert validate_balance(result.ledger) == []
    assert result.ledger.entries[-1].energy_end_kwh == pytest.approx(300.0, abs=1e-6)  # 7 次往返后无漂移


def test_summary_is_small_and_complete():
    summary = ledger_summary(hand_calc().ledger)
    assert summary["balanced"] and summary["total_cost_cny"] == pytest.approx(67.5)
    assert c.ToolResult(ok=True, business_status="done", data=summary).to_agent_text()


# ---------------------------------------------------------------- PR #3 复审（goodluck55k）回归


def test_validate_balance_keeps_missing_price_violation():
    led = run([{"pv_available_kw": 0, "load_kw": 100, "price_cny_per_kwh": None}], storage=None, e0=0.0).ledger
    assert not led.balanced
    assert any("缺电价" in p for p in validate_balance(led))


def test_validate_balance_keeps_curtail_while_importing():
    led = run([{"pv_available_kw": 600, "load_kw": 300, "pv_curtailed_kw": 600, "price_cny_per_kwh": 0.5}], storage=None, e0=0.0).ledger
    assert not led.balanced
    assert any("同时购电" in p for p in validate_balance(led))


def test_validate_balance_never_empty_for_unbalanced_ledger():
    led = hand_calc().ledger.model_copy(update={"balanced": False, "violations": ()})
    assert validate_balance(led)


@pytest.mark.parametrize("e0, row", [
    (50.0, {"pv_available_kw": 0, "load_kw": 100, "charge_kw": 300, "price_cny_per_kwh": 0.5}),  # SOC 0.05 → 充回 0.121
    (950.0, {"pv_available_kw": 0, "load_kw": 300, "discharge_kw": 300, "price_cny_per_kwh": 0.5}),  # SOC 0.95 → 放回 0.871
])
def test_initial_soc_out_of_bounds_not_masked_by_first_slot(e0, row):
    led = run([row], e0=e0).ledger
    assert not led.balanced and any("初始 SOC" in p for p in led.violations)
    assert any("初始 SOC" in p for p in validate_balance(led))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_reported_energy_end_must_be_finite(bad):
    with pytest.raises(c.PlantkgError):
        run([{"pv_available_kw": 0, "load_kw": 100, "price_cny_per_kwh": 0.5, "reported_energy_end_kwh": bad}])


def test_reported_energy_end_without_storage_must_be_zero():
    row = {"pv_available_kw": 0, "load_kw": 100, "price_cny_per_kwh": 0.5}
    assert run([{**row, "reported_energy_end_kwh": 0.0}], storage=None, e0=0.0).ledger.balanced
    result = run([{**row, "reported_energy_end_kwh": 20.0}], storage=None, e0=0.0)
    assert not result.ledger.balanced and any("期末电量" in p for p in result.problems)
