"""B10 pv 包：夜间零光伏、全弃光、装机缩放边界、模型与账本一致、弃光改变购电费用、不用未来实测当预测。"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pyomo.environ as pyo
import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "planner"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_packs import FakeStorage, FakeTariff  # noqa: E402
from test_planner import CTX, DAY, H0, SNAP, STEP, forecast, prices, request  # noqa: E402

from plantkg.adapters import PvgisAdapter  # noqa: E402
from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger  # noqa: E402
from plantkg.decision.planner import SharedPlanner  # noqa: E402
from plantkg.packs.pv import PvConfig, PvPack, check_within_kwp, load_pv_config, pv_actual_kw, pv_breakdown  # noqa: E402

FIX = ROOT / "fixtures" / "packs" / "pv"
CONFIG = load_pv_config(FIX / "pv.yaml", "fixtures/packs/pv/pv.yaml")
PV_ID = CONFIG.entity_id
SLOTS = [c.Interval(start=H0 + k * STEP, end=H0 + (k + 1) * STEP) for k in range(96)]
H = 0.25


@pytest.fixture(scope="module")
def pvgis(tmp_path_factory):
    """真实 PVGIS 摘录（2017-06-30～07-02 UTC）经 B05 适配器缩放到 500 kWp，源 07-01、07-02 映射到厂区 06-29、06-30。"""
    rows = json.loads((ROOT / "fixtures" / "timeseries" / "pvgis_2017_excerpt.json").read_text(encoding="utf-8"))["rows"]
    path = tmp_path_factory.mktemp("pvgis") / "pvgis.json"
    path.write_text(json.dumps({"inputs": {"meteo_data": {"radiation_db": "PVGIS-ERA5"}, "pv_module": {"peak_power": 1}},
                                "meta": {"outputs": {"hourly": {"timestamp": "hourly averages", "variables": {"P": {"units": "W"}}}}},
                                "outputs": {"hourly": rows}}), encoding="utf-8")
    return PvgisAdapter().normalize(path, {"source_start": "2017-07-01", "plant_start": "2026-06-29", "days": 2, "pvgis_version": "5.3",
                                           "source_peak_kwp": 1, "target_peak_kwp": 500}, {})


def persistence(bundle):
    """测试替身（正式预测归 B14）：目标时段取 24 h 前的值，as_of 时还不可见就取 48 h 前的值；都只读已可见记录。"""
    by_start = {r.interval.start: r for r in bundle.records}
    out = []
    for slot in SLOTS:
        rec = by_start[slot.start - timedelta(days=1)]
        if rec.available_at > CTX.as_of:
            rec = by_start[slot.start - timedelta(days=2)]
        assert rec.available_at <= CTX.as_of
        out.append(rec.value)
    return out


def bundle_with(pv_values):
    return c.ForecastBundle(forecasts=(forecast("fc-pv", "pv_available", pv_values), forecast("fc-load", "base_load", DAY["base_load_kw"])))


def solve(pv_values, rules=(), storage=False, config=CONFIG):
    packs = [PvPack(config), FakeTariff(prices())] + [FakeStorage(DAY["storage"])] * storage
    by_id = {r.rule_id: r for r in rules}
    bundle = bundle_with(pv_values)
    pl = SharedPlanner({p.name: p for p in packs}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [by_id[i] for i in ids])
    res = pl.solve(request(packs=tuple(p.name for p in packs), rule_ids=tuple(by_id)))
    return res, pl


def series(plan, quantity):
    out = [0.0] * 96
    for a in plan.actions:
        if a.quantity == quantity:
            out[int((a.interval.start - H0) / STEP)] = a.value
    return out


def cap_rule(upper, rule_id="rule-pv-cap", **kw):
    data = dict(rule_id=rule_id, kind="pv_output_cap", subject_ids=(PV_ID,), quantity="pv_output", unit="kW", bounds={"upper": upper},
                effective_from=H0 - timedelta(days=1), available_at=H0 - timedelta(days=1), config_ref="config://fixtures/packs/pv/pv.yaml#pv",
                nature="configured", review_status="trusted_config")
    data.update(kw)
    return c.Rule(**data)


def ledger_of(res, pl, pv_values, storage=False, curtail=None, report_import=True):
    plan, m = res.plan, pl.last_builder.model
    curtail = curtail or series(plan, "pv_curtail")
    pv_values = [v - x for v, x in zip(pv_values, series(plan, "pv_limit"))]
    charge, discharge = series(plan, "charge_power"), series(plan, "discharge_power")
    slots = [SlotInput(interval=SLOTS[k], pv_available_kw=pv_values[k], load_kw=DAY["base_load_kw"][k], charge_kw=charge[k],
                       discharge_kw=discharge[k], pv_curtailed_kw=curtail[k], price_cny_per_kwh=prices()[k],
                       reported_grid_import_kw=pyo.value(m.grid_import[k]) if report_import else None) for k in range(96)]
    st = DAY["storage"]
    params = c.StorageParams(capacity_kwh=st["capacity_kwh"], eta_c=st["eta_c"], eta_d=st["eta_d"], soc_min=st["soc_min"],
                             soc_max=st["soc_max"]) if storage else None
    meta = LedgerMeta("led-pv", "plan", CTX.run_id, plan.plan_id, plan.revision, CTX.version_manifest_id, ("fc-pv", "fc-load"))
    e0 = SNAP.pack_states["storage"]["energy_kwh"] if storage else 0.0
    return compute_ledger(slots, meta, params, e0, objective_value_cny=res.objective_value if report_import else None)


# ---------------------------------------------------------------- 数据口径


def test_pvgis_stays_derived_and_is_not_rescaled(pvgis):
    days = [c.Interval(start=r.interval.start, end=r.interval.end) for r in pvgis.records]
    values = pv_actual_kw(pvgis, days, pvgis.records[-1].available_at, CONFIG.kwp)
    raw = [r.value for r in pvgis.records]
    assert values == raw  # 原样取用 B05 已缩放到 500 kWp 的 kW，不再乘装机
    assert {r.nature for r in pvgis.records} == {"derived"} and 0 < max(raw) <= CONFIG.kwp
    assert pvgis.records[0].transform_steps[0].params["target_peak_kwp"] == CONFIG.kwp


def test_actuals_refuse_records_not_yet_visible(pvgis):
    day = [c.Interval(start=r.interval.start, end=r.interval.end) for r in pvgis.records]
    with pytest.raises(c.PlantkgError, match="还不可见"):
        pv_actual_kw(pvgis, day, pvgis.records[50].available_at - timedelta(seconds=1), CONFIG.kwp)
    first_day = day[:96]  # 只看第一天时，第二天还不可见的记录不影响
    assert len(pv_actual_kw(pvgis, first_day, pvgis.records[95].available_at, CONFIG.kwp)) == 96


def test_actuals_reject_off_grid_and_non_finite(pvgis):
    day = [c.Interval(start=r.interval.start, end=r.interval.end) for r in pvgis.records[:96]]
    shifted = day[0].model_copy(update={"start": day[0].start + timedelta(minutes=7), "end": day[0].end + timedelta(minutes=7)})
    rec = pvgis.records[0].model_copy(update={"interval": shifted})
    off = pvgis.model_copy(update={"records": (rec, *pvgis.records[1:96])})
    with pytest.raises(c.PlantkgError, match="网格"):
        pv_actual_kw(off, day, pvgis.records[-1].available_at, CONFIG.kwp)
    for bad in (float("nan"), -1.0, float("inf")):
        with pytest.raises(c.PlantkgError, match="非负有限数"):
            check_within_kwp([0.0, bad], CONFIG.kwp, "测试")


def test_future_actuals_cannot_pose_as_day_ahead_forecast(pvgis):
    pv = persistence(pvgis)
    with pytest.raises(ValueError, match="日前预测必须在目标区间开始前发布"):  # 用目标日实测当预测：发布时刻只能晚于目标开始
        c.Forecast(forecast_id="fc-leak", quantity="pv_available", unit="kW", issued_at=H0 + timedelta(hours=12),
                   target=c.Interval(start=H0, end=H0 + 96 * STEP), step_minutes=15, values=tuple(pv), model_version="leak",
                   input_slice=c.InputSlice(source_ids=("pvgis",), visible_until=H0 + timedelta(hours=12)), coverage=1.0)
    late = forecast("fc-pv", "pv_available", pv).model_copy(update={"issued_at": CTX.as_of + timedelta(minutes=1)})
    bundle = c.ForecastBundle(forecasts=(late, forecast("fc-load", "base_load", DAY["base_load_kw"])))
    pl = SharedPlanner({"pv": PvPack(CONFIG)}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [])
    with pytest.raises(c.PlantkgError, match="尚未发布"):
        pl.solve(request(packs=("pv",)))


def test_kwp_boundary_accepts_equal_and_rejects_double_scaling(pvgis):
    pv = persistence(pvgis)
    at_cap = [CONFIG.kwp if v > 0 else 0.0 for v in pv]
    assert solve(at_cap)[0].solver_status == "optimal"
    with pytest.raises(c.PlantkgError, match="超过装机 500"):
        solve([v * 500 for v in pv])  # 已缩放到 500 kWp 的值再乘一次装机
    with pytest.raises(c.PlantkgError, match="超过装机 500"):
        solve(DAY["pv_available_kw"])  # 构造日峰值 650 kW 与 500 kWp 不符
    with pytest.raises(c.PlantkgError, match="正有限数"):
        PvConfig(entity_id=PV_ID, kwp=0, config_ref="config://x#pv")


def test_missing_pv_forecast_is_not_planned_as_zero():
    bundle = c.ForecastBundle(forecasts=(forecast("fc-load", "base_load", DAY["base_load_kw"]),))
    pl = SharedPlanner({"pv": PvPack(CONFIG)}, lambda _id: SNAP, lambda _ids: bundle, lambda ids: [])
    with pytest.raises(c.PlantkgError, match="没有 pv_available"):
        pl.solve(request(packs=("pv",)))


# ---------------------------------------------------------------- 模型


def test_night_zero_pv_has_no_actions_and_conserves(pvgis):
    pv = persistence(pvgis)
    res, pl = solve(pv)
    assert res.solver_status == "optimal", res.validation_errors
    use, cut = series(res.plan, "pv_use"), series(res.plan, "pv_curtail")
    night = [k for k, v in enumerate(pv) if v == 0]
    assert len(night) >= 30 and all(use[k] == 0 and cut[k] == 0 for k in night)
    assert all(abs(use[k] + cut[k] - pv[k]) <= 1e-6 for k in range(96))
    acts = [a for a in res.plan.actions if a.quantity in ("pv_use", "pv_curtail")]
    assert all(a.object == PV_ID and c.Ref(kind="config", id=CONFIG.config_ref) in a.refs for a in acts)


def test_full_curtailment_raises_import_by_pv_energy(pvgis):
    pv = persistence(pvgis)
    free, _ = solve(pv)
    capped, pl = solve(pv, rules=[cap_rule(0.0)])
    assert capped.solver_status == "optimal", capped.validation_errors
    assert series(capped.plan, "pv_use") == [0.0] * 96 and series(capped.plan, "pv_curtail") == [0.0] * 96
    assert series(capped.plan, "pv_limit") == pytest.approx(pv, abs=1e-6)  # 限发记为 pv_limit，不算弃光
    extra = sum(p * u * H for p, u in zip(prices(), series(free.plan, "pv_use")))
    assert capped.objective_value - free.objective_value == pytest.approx(extra, abs=1e-3)
    assert all(c.Ref(kind="rule", id="rule-pv-cap") in a.refs for a in capped.plan.actions if a.quantity == "pv_limit")
    out = ledger_of(capped, pl, pv)  # 账本可发 = 预测 − 限发，限发时购电不判“弃光同时购电”
    assert out.ledger.balanced, out.problems[:5]
    assert out.energy_kwh["pv_available"] == pytest.approx(0.0, abs=1e-6)


def test_cap_rule_only_applies_inside_its_effect_window(pvgis):
    pv = persistence(pvgis)
    noon = H0 + timedelta(hours=12)  # 当地 12:00 起限发 50 kW
    res, _ = solve(pv, rules=[cap_rule(50.0, effective_from=noon)])
    assert res.solver_status == "optimal", res.validation_errors
    use, limit = series(res.plan, "pv_use"), series(res.plan, "pv_limit")
    assert max(use[:48]) > 50.0 and max(use[48:]) <= 50.0 + 1e-6
    assert limit[:48] == [0.0] * 48 and all(limit[k] == pytest.approx(max(pv[k] - 50.0, 0.0), abs=1e-6) for k in range(48, 96))


def test_bad_cap_rules_become_pending_issues():
    kind = PvPack(CONFIG).rule_kinds[0]
    assert kind.compile(cap_rule(10.0, unit="kWh"), CTX).reason == "unit_unknown"
    assert kind.compile(cap_rule(10.0, subject_ids=("entity://demo-electronics/pv/PV9",)), CTX).reason == "subject_unknown"
    assert kind.compile(cap_rule(None, bounds={"lower": 1.0}), CTX).reason == "config_invalid"
    assert kind.compile(cap_rule(-1.0), CTX).reason == "config_invalid"
    assert kind.compile(cap_rule(10.0), CTX).constraint_names == ("output_cap_rule_pv_cap",)


# ---------------------------------------------------------------- 账本


def test_model_and_ledger_agree_with_storage(pvgis):
    pv = [v * 3 for v in persistence(pvgis)]  # 放大到接近装机，午间有富余，储能与弃光都会出现
    res, pl = solve(pv, storage=True)
    assert res.solver_status == "optimal", res.validation_errors
    out = ledger_of(res, pl, pv, storage=True)
    assert out.ledger.balanced, out.problems[:5]
    assert out.energy_kwh["pv_used"] == pytest.approx(sum(series(res.plan, "pv_use")) * H, abs=1e-3)
    assert out.energy_kwh["pv_curtailed"] == pytest.approx(sum(series(res.plan, "pv_curtail")) * H, abs=1e-3)
    parts = pv_breakdown(out.ledger)
    assert parts["pv_to_charge"] > 0
    total = sum(parts[k] for k in ("pv_direct", "pv_to_charge", "pv_to_export", "pv_unattributed", "pv_curtailed"))
    assert total == pytest.approx(parts["pv_available"], abs=1e-9) and min(parts.values()) >= 0


def test_several_caps_with_curtailment_keep_ledger_balanced(pvgis):
    pv = [v * 3 for v in persistence(pvgis)]
    noon = H0 + timedelta(hours=12)
    rules = [cap_rule(460.0, "rule-cap-a"), cap_rule(440.0, "rule-cap-b", effective_from=noon)]
    res, pl = solve(pv, rules=rules)
    assert res.solver_status == "optimal", res.validation_errors
    caps = [460.0 if k < 48 else 440.0 for k in range(96)]
    assert series(res.plan, "pv_limit") == pytest.approx([max(v - cap, 0.0) for v, cap in zip(pv, caps)], abs=1e-6)
    assert sum(series(res.plan, "pv_curtail")) > 0 and sum(series(res.plan, "pv_limit")) > 0  # 限发与经济性弃光同时出现
    out = ledger_of(res, pl, pv)
    assert out.ledger.balanced, out.problems[:5]


def test_changing_curtailment_same_day_changes_import_and_cost(pvgis):
    pv = persistence(pvgis)
    res, pl = solve(pv)
    base = ledger_of(res, pl, pv)
    noon = max(range(96), key=lambda k: pv[k])
    curtail = series(res.plan, "pv_curtail")
    curtail[noon] = pv[noon]  # 同一天把最亮时段改成全弃光，购电按差额重算
    changed = ledger_of(res, pl, pv, curtail=curtail, report_import=False)
    assert changed.energy_kwh["grid_import"] - base.energy_kwh["grid_import"] == pytest.approx(pv[noon] * H, abs=1e-6)
    assert changed.ledger.total_cost_cny - base.ledger.total_cost_cny == pytest.approx(pv[noon] * H * prices()[noon], abs=1e-6)


def test_breakdown_does_not_credit_grid_charging_to_pv():
    slot = c.Interval(start=H0, end=H0 + STEP)
    meta = LedgerMeta("led-x", "execution", CTX.run_id, "plan-x", 0, CTX.version_manifest_id, ("src",))
    params = c.StorageParams(capacity_kwh=1000, eta_c=1, eta_d=1, soc_min=0, soc_max=1)
    mixed = compute_ledger([SlotInput(interval=slot, pv_available_kw=100, load_kw=60, charge_kw=200, price_cny_per_kwh=1.0)], meta, params, 500)
    parts = pv_breakdown(mixed.ledger)  # 光伏 100 kW 供 60 kW 负荷后剩 40 kW，但同时段购电 160 kW 一起充电：分不清
    assert parts["pv_direct"] == pytest.approx(15) and parts["pv_to_charge"] == 0 and parts["pv_unattributed"] == pytest.approx(10)
    sunny = compute_ledger([SlotInput(interval=slot, pv_available_kw=100, load_kw=60, charge_kw=40, price_cny_per_kwh=1.0)], meta, params, 500)
    assert pv_breakdown(sunny.ledger)["pv_to_charge"] == pytest.approx(10)  # 没有购电和放电：充电全来自光伏


# ---------------------------------------------------------------- 配置装载


def _pv_yaml(tmp_path, text):
    path = tmp_path / "pv.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_pv_config_rejects_bool_unknown_key_and_missing_file(tmp_path):
    """kwp: true 过去被 isinstance 收成 1 kWp（#128）；缺文件/未知键不能抛原生异常。"""
    pv = "pv:\n  entity_id: entity://demo-electronics/pv/PV1\n"
    with pytest.raises(c.PlantkgError, match="pv.kwp"):
        load_pv_config(_pv_yaml(tmp_path, pv + "  kwp: true\n"), "pv.yaml")
    with pytest.raises(c.PlantkgError, match="未知配置项"):
        load_pv_config(_pv_yaml(tmp_path, pv + "  kwp: 500\n  kwpp: 500\n"), "pv.yaml")
    with pytest.raises(c.PlantkgError, match="没有 pv"):
        load_pv_config(_pv_yaml(tmp_path, "storage: {}\n"), "pv.yaml")
    with pytest.raises(c.PlantkgError, match="读取失败"):
        load_pv_config(tmp_path / "missing.yaml", "missing.yaml")
