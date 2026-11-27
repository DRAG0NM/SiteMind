"""B11 storage 包：初末 SOC 对照、极端配置与备用比例反例、负电价不同时充放、账本 SOC 与模型轨迹一致、参数只随版本清单变。"""

import math
import sys
from datetime import timedelta
from pathlib import Path

import pyomo.environ as pyo
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "planner"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_packs import FakePv, FakeTariff  # noqa: E402
from test_planner import CTX, DAY, FORECASTS, H0, SNAP, STEP, prices, request  # noqa: E402

from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger  # noqa: E402
from plantkg.decision.planner import SharedPlanner  # noqa: E402
from plantkg.packs.storage import (  # noqa: E402
    StorageConfig, StoragePack, load_storage_config, reserve_ratio_range, validate_reserve_ratio, validate_storage_config,
)

FIX = ROOT / "fixtures" / "packs" / "storage"
CONFIG_ID, PARAMS_ID = "fixtures/packs/storage/storage.yaml", "fixtures/packs/storage/parameters.yaml"
CONFIG = load_storage_config(FIX / "storage.yaml", CONFIG_ID)
PARAMS = yaml.safe_load((FIX / "parameters.yaml").read_text(encoding="utf-8"))
RAW = yaml.safe_load((FIX / "storage.yaml").read_text(encoding="utf-8"))["storage"]
BAT = CONFIG.entity_id
H = 0.25
E0 = SNAP.pack_states["storage"]["energy_kwh"]  # 500 kWh


def pack(config=CONFIG, params=None):
    return StoragePack(config, PARAMS if params is None else params, PARAMS_ID)


def solve(storage=None, tariff=None, pv=True, rules=(), manifest="vm-0003", energy=E0):
    packs = ([FakePv()] if pv else []) + [tariff or FakeTariff(prices()), storage or pack()]
    by_id = {r.rule_id: r for r in rules}
    ctx = CTX.model_copy(update={"version_manifest_id": manifest})
    snap = SNAP.model_copy(update={"version_manifest_id": manifest, "pack_states": {**SNAP.pack_states, "storage": {"energy_kwh": energy}}})
    pl = SharedPlanner({p.name: p for p in packs}, lambda _id: snap, lambda _ids: FORECASTS, lambda ids: [by_id[i] for i in ids])
    req = request(packs=tuple(p.name for p in packs), rule_ids=tuple(by_id)).model_copy(update={"context": ctx})
    return pl.solve(req), pl


def series(plan, quantity):
    out = [0.0] * 96
    for a in plan.actions:
        if a.quantity == quantity:
            out[int((a.interval.start - H0) / STEP)] = a.value
    return out


def energy(pl):
    return [pyo.value(pl.last_builder.model.storage__energy[t]) for t in range(96)]


def rule(kind, quantity, unit, bounds, rule_id="rule-bat", **kw):
    data = dict(rule_id=rule_id, kind=kind, subject_ids=(BAT,), quantity=quantity, unit=unit, bounds=bounds, effective_from=H0 - timedelta(days=1),
                available_at=H0 - timedelta(days=1), config_ref=f"config://{CONFIG_ID}#storage", nature="configured", review_status="trusted_config")
    data.update(kw)
    return c.Rule(**data)


# ---------------------------------------------------------------- 配置与参数


@pytest.mark.parametrize("key,value", [
    ("capacity_kwh", 0), ("capacity_kwh", -1000), ("capacity_kwh", math.nan), ("charge_kw", 0), ("discharge_kw", math.inf),
    ("eta_c", 0), ("eta_c", 1.2), ("eta_d", -0.5), ("soc_min", 0.9), ("soc_max", 1.5), ("end_soc", 0.95), ("degradation_cny_per_kwh", -0.01),
    ("capacity_kwh", True), ("entity_id", "entity://demo-electronics/pv/PV1"),
])
def test_invalid_device_config_rejected_before_solve(key, value):
    data = {**RAW, key: value}
    issues = validate_storage_config(data, f"config://{CONFIG_ID}#storage")
    assert issues and all(i.reason == "config_invalid" for i in issues)
    with pytest.raises(c.PlantkgError, match="求解前拒绝"):
        StorageConfig(**data, config_ref=f"config://{CONFIG_ID}#storage")


def test_config_needs_trusted_source_and_known_keys():
    assert any(i.subject == "config_ref" for i in validate_storage_config(RAW, "fixtures/storage.yaml"))
    assert any(i.subject == "unknown" for i in validate_storage_config({**RAW, "eta": 0.9}, f"config://{CONFIG_ID}#storage"))
    assert validate_storage_config(RAW, f"config://{CONFIG_ID}#storage") == []
    assert CONFIG.ledger_params() == c.StorageParams(capacity_kwh=1000, eta_c=0.95, eta_d=0.95, soc_min=0.1, soc_max=0.9)


@pytest.mark.parametrize("value", [-0.01, 0.51, math.nan, math.inf, None, "0.2", True])
def test_reserve_ratio_out_of_range_rejected(value):
    assert validate_reserve_ratio(value).reason == "config_invalid"
    with pytest.raises(c.PlantkgError, match="reserve_ratio"):
        pack(params={"vm-0003": {"reserve_ratio": value}})


def test_reserve_ratio_range_and_infeasible_just_above_it():
    lo, hi = reserve_ratio_range(CONFIG, 150.0, H)
    reachable = 150.0 + 0.95 * 250 * H  # 首个时段满功率充电后最多到 209.375 kWh
    assert lo == 0.0 and hi == pytest.approx((reachable / 1000 - 0.1) / 0.8)
    assert reserve_ratio_range(CONFIG, E0, H)[1] == 0.5  # 受参数范围上限约束
    ok, _ = solve(pack(params={"vm-0003": {"reserve_ratio": hi}}), energy=150.0)
    assert ok.solver_status == "optimal", ok.validation_errors
    bad, _ = solve(pack(params={"vm-0003": {"reserve_ratio": hi + 0.01}}), energy=150.0)
    assert bad.solver_status == "infeasible" and any("storage" in h for h in bad.conflict_hints)
    with pytest.raises(c.PlantkgError, match="到不了"):
        reserve_ratio_range(CONFIG, 10.0, H)


def test_baseline_and_candidate_differ_only_through_version_manifest():
    p = pack()
    base, pl_base = solve(p, manifest="vm-0003")
    cand, pl_cand = solve(p, manifest="vm-0004")
    assert base.solver_status == cand.solver_status == "optimal"
    floor = CONFIG.reserve_floor_kwh(0.3)  # 0.1 + 0.3 × 0.8 = 0.34 → 340 kWh
    assert min(energy(pl_base)) < floor - 1 and min(energy(pl_cand)) >= floor - 1e-6
    assert cand.objective_value > base.objective_value  # 留备用要付出电费
    assert base.plan.plan_id != cand.plan.plan_id
    with pytest.raises(TypeError):
        p.parameters["vm-0003"]["reserve_ratio"] = 0.4  # 包上的参数只读，只能换版本清单
    with pytest.raises(c.PlantkgError, match="没有储能参数"):
        solve(p, manifest="vm-9999")


@pytest.mark.parametrize("value", [None, -1.0, 50.0, 950.0, 1000.5, math.nan])
def test_initial_energy_outside_soc_range_rejected(value):
    with pytest.raises(c.PlantkgError, match="初始电量"):
        solve(energy=value)


def test_initial_energy_within_ledger_tolerance_is_clamped():
    res, pl = solve(energy=900.0 + 1e-4)  # 超出 SOC 上限 1e-4 kWh，在账本 soc_abs × 容量 = 1e-3 kWh 以内
    assert res.solver_status == "optimal", res.validation_errors


def test_parameters_and_config_files_reject_unknown_keys(tmp_path):
    with pytest.raises(c.PlantkgError, match="只能有 reserve_ratio"):
        pack(params={"vm-0003": {"reserve_ration": 0.1}})
    bad = tmp_path / "s.yaml"
    bad.write_text(yaml.safe_dump({"storage": {**RAW, "eta": 0.9}}), encoding="utf-8")
    with pytest.raises(c.PlantkgError, match="未知配置项"):
        load_storage_config(bad, "s.yaml")
    bad.write_text("pv: {}", encoding="utf-8")
    with pytest.raises(c.PlantkgError, match="没有 storage"):
        load_storage_config(bad, "s.yaml")


@pytest.mark.parametrize("key,value", [
    ("eta_c", 1e-320), ("eta_d", 0.49), ("degradation_cny_per_kwh", 1e308), ("degradation_cny_per_kwh", 2.001),
])
def test_extreme_values_rejected_before_solve(tmp_path, key, value):
    """eta=1e-320、degradation=1e308 过去能过求解前校验，到模型阶段才以期末约束失败（#132）。"""
    bad = tmp_path / "s.yaml"
    bad.write_text(yaml.safe_dump({"storage": {**RAW, key: value}}), encoding="utf-8")
    with pytest.raises(c.PlantkgError, match=key):
        load_storage_config(bad, "s.yaml")


def test_engineering_bounds_accept_boundary_values():
    cfg = StorageConfig(**{**RAW, "eta_c": 0.5, "eta_d": 1.0, "degradation_cny_per_kwh": 2.0}, config_ref=f"config://{CONFIG_ID}#storage")
    assert (cfg.eta_c, cfg.eta_d, cfg.degradation_cny_per_kwh) == (0.5, 1.0, 2.0)
    res, _ = solve(pack(cfg))
    assert res.solver_status == "optimal", res.validation_errors


def test_missing_config_file_is_domain_error():
    with pytest.raises(c.PlantkgError, match="读取失败"):
        load_storage_config(Path("no-such-storage.yaml"), "missing.yaml")


# ---------------------------------------------------------------- 模型


def test_end_soc_matches_initial_or_configured_target():
    res, pl = solve()
    assert res.solver_status == "optimal", res.validation_errors
    assert energy(pl)[-1] >= E0 - 1e-6
    target = StorageConfig(**{**RAW, "end_soc": 0.8}, config_ref=f"config://{CONFIG_ID}#storage")
    res2, pl2 = solve(pack(target))
    assert res2.solver_status == "optimal" and energy(pl2)[-1] >= 800 - 1e-6


def test_negative_prices_do_not_charge_and_discharge_together():
    neg = [-0.5 if 40 <= k < 64 else p for k, p in enumerate(prices())]  # 午间 6 小时负电价：同时充放会白赚“多用电”的钱
    res, pl = solve(tariff=FakeTariff(neg), pv=False)
    assert res.solver_status == "optimal", res.validation_errors
    ch, dis = series(res.plan, "charge_power"), series(res.plan, "discharge_power")
    assert sum(ch[40:64]) > 0
    assert all(not (x > 1e-6 and y > 1e-6) for x, y in zip(ch, dis))
    assert pl.last_builder.model.storage__is_charging[0].is_binary()


def test_extreme_efficiency_still_matches_ledger():
    for eta in (1.0, 0.5):
        cfg = StorageConfig(**{**RAW, "eta_c": eta, "eta_d": eta}, config_ref=f"config://{CONFIG_ID}#storage")
        res, pl = solve(pack(cfg))
        assert res.solver_status == "optimal", res.validation_errors
        assert sum(series(res.plan, "charge_power")) > 0  # 储能确实在动，账本核对不是空转
        out = ledger_of(res, pl, cfg)
        assert out.ledger.balanced, (eta, out.problems[:3])


def test_rules_soc_window_and_power_limit():
    noon = H0 + timedelta(hours=12)
    rules = [rule("storage_soc_window", "soc", "1", {"lower": 0.4}, "rule-soc"),
             rule("storage_power_limit", "power", "kW", {"upper": 100.0}, "rule-pow", effective_from=noon)]
    res, pl = solve(rules=rules)
    assert res.solver_status == "optimal", res.validation_errors
    assert min(energy(pl)) >= 400 - 1e-6
    ch, dis = series(res.plan, "charge_power"), series(res.plan, "discharge_power")
    assert max(ch[:48] + dis[:48]) > 100 and max(ch[48:] + dis[48:]) <= 100 + 1e-6
    refs = {r for a in res.plan.actions if a.quantity == "discharge_power" for r in a.refs}
    assert c.Ref(kind="rule", id="rule-pow") in refs


def test_bad_rules_become_pending_issues():
    soc, power = pack().rule_kinds
    assert soc.compile(rule("storage_soc_window", "soc", "%", {"lower": 40}), CTX).reason == "unit_unknown"
    assert soc.compile(rule("storage_soc_window", "soc", "1", {"lower": 1.2}), CTX).reason == "config_invalid"
    assert power.compile(rule("storage_power_limit", "power", "kW", {"upper": 10}, subject_ids=("entity://demo-electronics/battery/BAT2",)),
                         CTX).reason == "subject_unknown"
    assert power.compile(rule("storage_power_limit", "power", "kW", {"lower": 10}), CTX).reason == "config_invalid"


def test_degradation_cost_only_when_configured():
    plain, _ = solve()
    assert "storage.degradation" not in plain.objective_terms
    cfg = StorageConfig(**{**RAW, "degradation_cny_per_kwh": 0.05}, config_ref=f"config://{CONFIG_ID}#storage")
    worn, _ = solve(pack(cfg))
    throughput = lambda r: (sum(series(r.plan, "charge_power")) + sum(series(r.plan, "discharge_power"))) * H  # noqa: E731
    assert worn.objective_terms["storage.degradation"] == pytest.approx(0.05 * throughput(worn), abs=1e-6)
    assert throughput(worn) <= throughput(plain) + 1e-6


# ---------------------------------------------------------------- 账本


def ledger_of(res, pl, cfg=CONFIG):
    plan, m = res.plan, pl.last_builder.model
    ch, dis, cut = series(plan, "charge_power"), series(plan, "discharge_power"), series(plan, "pv_curtail")
    slots = [SlotInput(interval=c.Interval(start=H0 + k * STEP, end=H0 + (k + 1) * STEP), pv_available_kw=DAY["pv_available_kw"][k],
                       load_kw=DAY["base_load_kw"][k], charge_kw=ch[k], discharge_kw=dis[k], pv_curtailed_kw=cut[k],
                       price_cny_per_kwh=prices()[k], reported_grid_import_kw=pyo.value(m.grid_import[k]),
                       reported_energy_end_kwh=pyo.value(m.storage__energy[k])) for k in range(96)]
    meta = LedgerMeta("led-bat", "plan", CTX.run_id, plan.plan_id, plan.revision, CTX.version_manifest_id, ("fc-pv", "fc-load"))
    return compute_ledger(slots, meta, cfg.ledger_params(), E0, objective_value_cny=res.objective_value)


def test_ledger_soc_matches_model_trajectory():
    res, pl = solve()
    out = ledger_of(res, pl)
    assert out.ledger.balanced, out.problems[:5]
    model_soc = [v / CONFIG.capacity_kwh for v in energy(pl)]
    assert [e.soc_end for e in out.ledger.entries] == pytest.approx(model_soc, abs=c.LEDGER_TOLERANCE["soc_abs"])
