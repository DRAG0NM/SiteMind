"""B13 production 包：窗口平移、离散开始与曲线卷积、不可行、缺测、总分表替换与数据来源可见。"""

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "planner"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_packs import FakePv, FakeStorage, FakeTariff  # noqa: E402
from test_planner import DAY, FORECASTS, SNAP, prices, request  # noqa: E402

from plantkg.adapters.baseload import selected_device_baseload  # noqa: E402
from plantkg.bootstrap import PACKS_DIR  # noqa: E402
from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.registry import Registry, RegistryError  # noqa: E402
from plantkg.decision.planner import SharedPlanner  # noqa: E402
from plantkg.packs.production import (  # noqa: E402
    ProductionPack, load_operations, load_profile_for_start, operation_shape, shifted_total,
)

FIX = ROOT / "fixtures" / "packs" / "production"
CONFIG_ID = "fixtures/packs/production/operations.yaml"
SPEC = load_operations(FIX / "operations.yaml", CONFIG_ID)[0]
H0 = datetime(2026, 6, 30, 16, tzinfo=timezone.utc)  # 当地 07-01 00:00
STEP = timedelta(minutes=15)
ORIG = 39  # 当地 09:45
DAY_BUNDLES = {k: c.SourceBundle.model_validate(v) for k, v in json.loads((FIX / "hipe_day_20260701.json").read_text(encoding="utf-8")).items()}
HORIZON = c.Interval(start=H0, end=H0 + 96 * STEP)
LEDGER_WINDOW = c.Interval(start=H0 + STEP, end=H0 + 96 * STEP)  # HIPE 当日首个时段（当地 00:00）实测缺测


def window_rule(lower=8, upper=18, rule_id="rule-job1-window", subject=SPEC.entity_id, **kw):
    hhmm = lambda h: f"{int(h):02d}:{round(h % 1 * 60):02d}"
    data = dict(rule_id=rule_id, kind="operation_window", subject_ids=(subject,), condition={"local_window": [hhmm(lower), hhmm(upper)]},
                quantity="operation_start", unit="h", bounds={"lower": lower, "upper": upper}, effective_from=H0 - timedelta(days=1),
                available_at=H0 - timedelta(days=1), config_ref=f"config://{CONFIG_ID}#operations.JOB1.window", nature="configured",
                review_status="trusted_config")
    data.update(kw)
    return c.Rule(**data)


def solve(rules=(), with_storage=True, forecasts=FORECASTS, with_pv=True):
    packs = [FakeTariff(prices()), ProductionPack([SPEC])] + [FakePv()] * with_pv + [FakeStorage(DAY["storage"])] * with_storage
    by_id = {r.rule_id: r for r in rules}
    planner = SharedPlanner({p.name: p for p in packs}, lambda _id: SNAP, lambda _ids: forecasts, lambda ids: [by_id[i] for i in ids])
    names = tuple(p.name for p in packs)
    return planner.solve(request(packs=names, rule_ids=tuple(by_id)))


def starts(plan):
    return [int((a.interval.start - H0) / STEP) for a in plan.actions if a.quantity == "operation_start"]


def power(plan):
    out = [0.0] * 96
    for a in plan.actions:
        if a.quantity == "operation_power":
            out[int((a.interval.start - H0) / STEP)] = a.value
    return out


def test_shape_comes_from_real_hipe_run_and_rated_power():
    assert SPEC.slots == 12 and max(SPEC.shape.fractions) == 1.0
    assert SPEC.shape.meter == "VacuumPump1" and SPEC.shape.content_hash.startswith("sha256:")
    assert max(SPEC.profile_kw) == 150.0


def test_no_window_rule_keeps_original_start():
    res = solve()
    assert res.solver_status == "optimal"
    assert starts(res.plan) == [ORIG]
    assert power(res.plan) == pytest.approx(load_profile_for_start(SPEC, ORIG, 96), abs=1e-6)


def test_wide_window_shifts_and_lowers_cost_with_same_energy():
    # 不接光伏：原运行 09:45～12:45 落在峰段，窗口内移到平段能省钱（接了光伏时午间富余会弃光，平移不一定有收益）
    fixed, shifted = solve(with_storage=False, with_pv=False), solve([window_rule()], with_storage=False, with_pv=False)
    assert shifted.solver_status == "optimal"
    (s,) = starts(shifted.plan)
    local = (H0 + s * STEP).astimezone(timezone(timedelta(hours=8)))
    assert local.hour >= 8 and (local + SPEC.slots * STEP).hour <= 18
    assert s != ORIG and shifted.objective_value < fixed.objective_value - 1.0
    assert sum(power(shifted.plan)) == pytest.approx(sum(power(fixed.plan)), rel=1e-9)  # 只换时段，不改电量


def test_actions_start_and_end_match_profile():
    res = solve([window_rule()])
    (s,) = starts(res.plan)
    p = power(res.plan)
    assert p == pytest.approx(load_profile_for_start(SPEC, s, 96), abs=1e-6)
    on = [t for t, v in enumerate(p) if v > 0]
    assert on[0] == s and on[-1] == s + SPEC.slots - 1


def test_window_equal_to_original_run_gives_no_gain():
    # 窗口恰好等于原运行 09:45～12:45：没有平移空间，结果与不平移相同（无收益，不是不可行）
    res, base = solve([window_rule(lower=9.75, upper=12.75)], with_pv=False), solve(with_pv=False)
    assert res.solver_status == "optimal" and starts(res.plan) == [ORIG]
    assert res.objective_value == pytest.approx(base.objective_value, abs=1e-6)


def test_window_too_short_is_infeasible_with_rule_hint():
    res = solve([window_rule(lower=8, upper=10)])
    assert res.solver_status == "infeasible" and res.plan is None
    assert any("rule-job1-window" in h and "production" in h for h in res.conflict_hints)


def test_window_binds_only_while_rule_is_effective():
    # 窗口 08～10 放不下 3 小时工序；规则在当地 08:30 失效后不再约束，开始时段可自由选
    rule = window_rule(lower=8, upper=10, effective_to=H0 + timedelta(hours=8, minutes=30))
    res = solve([rule], with_pv=False, with_storage=False)
    assert res.solver_status == "optimal" and starts(res.plan)[0] >= 34


def test_rule_for_operation_not_pending_is_reported():
    snap = SNAP.model_copy(update={"pack_states": {**SNAP.pack_states, "production": {"pending_jobs": []}}})
    planner = SharedPlanner({"production": ProductionPack([SPEC]), "tariff": FakeTariff(prices())}, lambda _id: snap,
                            lambda _ids: FORECASTS, lambda ids: [window_rule()])
    with pytest.raises(c.PlantkgError, match="不在本次待排工序"):
        planner.solve(request(packs=("tariff", "production"), rule_ids=("rule-job1-window",)))


@pytest.mark.parametrize("kw,reason", [
    (dict(subject="entity://demo-electronics/operation/JOB9"), "subject_unknown"),
    (dict(lower=18, upper=8), None),  # Bounds 自身就拒绝 lower > upper
    (dict(unit="min"), "unit_unknown"),
    (dict(upper=30), "config_invalid"),
])
def test_bad_rules_become_pending(kw, reason):
    if reason is None:
        with pytest.raises(ValueError):
            window_rule(**kw)
        return
    res = solve([window_rule(**kw)])
    assert res.solver_status == "optimal" and starts(res.plan) == [ORIG]  # 规则不生效，工序留在原时段
    assert [i.reason for i in res.pending_issues] == [reason]


def test_provenance_note_is_visible_on_every_operation_action():
    res = solve([window_rule()])
    for a in res.plan.actions:
        if a.object == SPEC.entity_id:
            assert c.Ref(kind="config", id=f"config://{CONFIG_ID}#provenance_note") in a.refs
        if a.quantity == "operation_start":  # 窗口约束带规则及其配置来源，与编译声明一致
            assert {c.Ref(kind="rule", id="rule-job1-window"), c.Ref(kind="config", id=f"config://{CONFIG_ID}#operations.JOB1.window")} <= set(a.refs)


def test_unconfigured_pending_job_is_not_silently_dropped():
    snap = SNAP.model_copy(update={"pack_states": {**SNAP.pack_states, "production": {"pending_jobs": ["JOB1", "JOB2"]}}})
    planner = SharedPlanner({"production": ProductionPack([SPEC]), "tariff": FakeTariff(prices())}, lambda _id: snap,
                            lambda _ids: FORECASTS, lambda ids: [])
    with pytest.raises(c.PlantkgError, match="JOB2"):
        planner.solve(request(packs=("tariff", "production")))


def test_partial_missing_in_shape_run_is_rejected():
    vp = DAY_BUNDLES["VacuumPump1"]
    run = SPEC.shape.source_interval
    assert operation_shape(vp, run, "VacuumPump1").fractions == pytest.approx(SPEC.shape.fractions, abs=1e-6)
    holed = vp.model_copy(update={"records": tuple(r.model_copy(update={"value": None, "quality": c.Quality(flag="missing")})
                                                   if r.interval.start == run.start + 3 * STEP else r for r in vp.records)})
    with pytest.raises(c.PlantkgError, match="缺测"):
        operation_shape(holed, run, "VacuumPump1")


def test_shifted_total_conserves_energy_and_stays_nonnegative():
    report, coverage = shifted_total(DAY_BUNDLES["MainTerminal"], DAY_BUNDLES["VacuumPump1"], SPEC, LEDGER_WINDOW, H0 + 50 * STEP)
    assert report.energy_after_kwh == pytest.approx(report.energy_before_kwh, rel=1e-9)
    assert min(report.total_kw) >= 0 and min(report.residual_kw) >= 0
    assert coverage["factor"] == 25.0 and coverage["max_device_share"] < 1


def test_shifted_total_rejects_device_over_total_and_missing():
    main, vp = DAY_BUNDLES["MainTerminal"], DAY_BUNDLES["VacuumPump1"]
    big = vp.model_copy(update={"records": tuple(r.model_copy(update={"value": r.value and r.value * 1e4}) for r in vp.records)})
    with pytest.raises(c.PlantkgError, match="重复计量"):
        shifted_total(main, big, SPEC, LEDGER_WINDOW, H0 + 50 * STEP)
    with pytest.raises(c.PlantkgError, match="缺测"):  # 真实缺测：整天窗口含当地 00:00 缺测时段
        shifted_total(main, vp, SPEC, HORIZON, H0 + 50 * STEP)


def test_real_day_with_q25_base_load_plans_and_shifts():
    """07 Q25 口径：基础负荷 = 总进线 − 被选作形状的设备表（真实 HIPE 当日 ×25），工序负荷只由本包贡献。"""
    base = selected_device_baseload(DAY_BUNDLES["MainTerminal"], [DAY_BUNDLES["VacuumPump1"]])
    values = [r.value for r in sorted(base.records, key=lambda r: r.interval.start)]
    assert values[0] is None
    values[0] = values[1]  # 首段实测缺测：测试用预测按相邻时段保持（预测本身归 B14），不当 0
    load = c.Forecast(forecast_id="fc-load", quantity="base_load", unit="kW", issued_at=SNAP.as_of, target=HORIZON, step_minutes=15,
                      values=tuple(values), model_version="hindsight-hold-gap-test",
                      input_slice=c.InputSlice(source_ids=(base.bundle_id,), visible_until=SNAP.as_of), coverage=1.0)
    bundle = c.ForecastBundle(forecasts=(FORECASTS.forecasts[0], load))
    res = solve([window_rule()], forecasts=bundle)
    assert res.solver_status == "optimal" and len(starts(res.plan)) == 1


# ---------------------------------------------------------------- 装载与窗口键


SHAPE_TEXT = (FIX / "vacuum_pump1_run_20260701.json").read_text(encoding="utf-8")
OPS_TEXT = (FIX / "operations.yaml").read_text(encoding="utf-8").replace("shape: vacuum_pump1_run_20260701.json", "shape: run.json")


def operations_yaml(tmp_path, text):
    path = tmp_path / "operations.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_window_key_uses_operation_id_not_entity_tail():
    """operation_id=PRESS1、entity 尾名 JOB1：窗口键统一按 operation_id，声明与建模对得上（#87）。"""
    spec = replace(SPEC, operation_id="PRESS1")
    rule = window_rule(subject=spec.entity_id)
    snap = SNAP.model_copy(update={"pack_states": {**SNAP.pack_states, "production": {"pending_jobs": ["PRESS1"]}}})
    planner = SharedPlanner({"tariff": FakeTariff(prices()), "production": ProductionPack([spec])}, lambda _id: snap,
                            lambda _ids: FORECASTS, lambda ids: [rule])
    res = planner.solve(request(packs=("tariff", "production"), rule_ids=(rule.rule_id,)))
    assert res.solver_status == "optimal" and res.validation_errors == ()
    assert "production__window_press1" in planner.last_builder.state.constraints
    assert "production__window_job1" not in planner.last_builder.state.constraints


def test_load_operations_rejects_entity_tail_mismatching_operation_id(tmp_path):
    (tmp_path / "run.json").write_text(SHAPE_TEXT, encoding="utf-8")
    path = operations_yaml(tmp_path, OPS_TEXT.replace("operation_id: JOB1", "operation_id: PRESS1"))
    with pytest.raises(c.PlantkgError, match="不一致"):
        load_operations(path, "operations.yaml")


@pytest.mark.parametrize("old,new,pattern", [
    ("rated_kw: 150.0", "rated_kw: true", "rated_kw"),
    ("shape: run.json", "shape: missing.json", "读取失败"),
    ("2026-07-01T01:45:00Z", "2026-07-01T01:45:00", "必须带时区"),
])
def test_load_operations_rejects_bad_values(tmp_path, old, new, pattern):
    (tmp_path / "run.json").write_text(SHAPE_TEXT, encoding="utf-8")
    path = operations_yaml(tmp_path, OPS_TEXT.replace(old, new))
    with pytest.raises(c.PlantkgError, match=pattern):
        load_operations(path, "operations.yaml")


def test_load_operations_rejects_unknown_key_and_missing_section(tmp_path):
    (tmp_path / "run.json").write_text(SHAPE_TEXT, encoding="utf-8")
    path = operations_yaml(tmp_path, OPS_TEXT.replace("    shape: run.json", "    shape: run.json\n    rated_kw2: 1"))
    with pytest.raises(c.PlantkgError, match="未知键"):
        load_operations(path, "operations.yaml")
    with pytest.raises(c.PlantkgError, match="没有 operations"):
        load_operations(operations_yaml(tmp_path, "provenance_note: x\n"), "operations.yaml")
    with pytest.raises(c.PlantkgError, match="读取失败"):
        load_operations(tmp_path / "missing.yaml", "missing.yaml")


@pytest.mark.parametrize("fractions", [[], [0.5, -0.1], [0.5, float("nan")], [0.0, 0.0]])
def test_load_operations_rejects_bad_shape(tmp_path, fractions):
    raw = json.loads(SHAPE_TEXT)
    raw["fractions"] = fractions
    (tmp_path / "run.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(c.PlantkgError, match="fractions"):
        load_operations(operations_yaml(tmp_path, OPS_TEXT), "operations.yaml")


def test_manifest_provides_enable_registry_duplicate_detection(tmp_path):
    """pack.yaml 从文档变成可执行清单：registry 的重复检测要覆盖 production 的规则与状态（#89）。"""
    (manifest,) = Registry().load([PACKS_DIR / "production"], ["production"])
    assert manifest.provides.rule_kinds == ("operation_window",) and manifest.provides.state == ("pending_jobs",)
    clash = tmp_path / "clash"
    clash.mkdir()
    (clash / "pack.yaml").write_text("name: clash\nschema_version: 1\nstatus: placeholder\nprovides:\n  rule_kinds: [operation_window]\n",
                                     encoding="utf-8")
    with pytest.raises(RegistryError, match="operation_window"):
        Registry().load([PACKS_DIR / "production", clash], ["production", "clash"])
