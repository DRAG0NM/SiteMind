"""B12 tariff 包：跨午夜、季节与版本切换、缺段/重叠/未知类别反例、手算 4 时段核对模型与账本、未审核或不可见规则拒绝编译。"""

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyomo.environ as pyo
import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "planner"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_packs import FakePv, FakeStorage  # noqa: E402
from test_planner import BASE_REQ, CTX, DAY, FORECASTS, H0, SNAP, STEP, forecast, request, series  # noqa: E402

from plantkg.core import contracts as c  # noqa: E402
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger  # noqa: E402
from plantkg.decision.planner import SharedPlanner  # noqa: E402
from plantkg.packs.tariff import (  # noqa: E402
    TariffPack, acceptance_blockers, cost_report, load_tariff_config, load_tariff_rules, parse_schedule, price_schedule,
)

FIX = ROOT / "fixtures" / "packs" / "tariff"
SH = ZoneInfo("Asia/Shanghai")


def rules_of(name):
    return load_tariff_rules(FIX / f"{name}.yaml", f"fixtures/packs/tariff/{name}.yaml")


PACK, DEMO_BASE = load_tariff_config(FIX / "tariff.yaml", "fixtures/packs/tariff/tariff.yaml")
DEMO = {r.rule_id: r for r in rules_of("demo_tou")}
OLD, NEW = DEMO["demo-tou-old"], DEMO["demo-tou-new"]
REAL = rules_of("jiangsu_2025_426")[0]
BASE = 0.62


def local(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=SH).astimezone(timezone.utc)


def slots(start, n, minutes=15):
    return [c.Interval(start=start + timedelta(minutes=minutes * i), end=start + timedelta(minutes=minutes * (i + 1))) for i in range(n)]


def approved(rule, **kw):
    return rule.model_copy(update={"review_status": "approved", "reviewer": "u-reviewer-1", **kw})


def with_periods(rule, periods, **kw):
    return rule.model_copy(update={"condition": {**rule.condition, "periods": periods}, **kw})


def solve(packs, rules, horizon=None, forecasts=FORECASTS):
    by_id = {r.rule_id: r for r in rules}
    pl = SharedPlanner({p.name: p for p in packs}, lambda _id: SNAP, lambda _ids: forecasts, lambda ids: [by_id[i] for i in ids])
    req = request(packs=tuple(p.name for p in packs), rule_ids=tuple(by_id))
    if horizon is not None:
        req = req.model_copy(update={"horizon": horizon})
    return pl.solve(req), pl


# ---------------------------------------------------------------- 原文登记与演示标记


def test_real_policy_files_parse_with_review_state():
    for name in ("jiangsu_2025_426", "jiangsu_pre2025"):
        for r in rules_of(name):
            assert not isinstance(parse_schedule(r), tuple), r.rule_id
            assert r.condition["demo"] is False and r.condition["source"]["urls"]
            assert all(p["locator"] for p in r.condition["periods"])
    old = rules_of("jiangsu_pre2025")[0]
    assert old.review_status == "pending" and old.reviewer is None  # 只有媒体转述，未对原件复核前不能进规划
    assert REAL.review_status == "approved" and REAL.reviewer
    src = REAL.condition["source"]
    assert src["doc_no"] == "苏发改价格发〔2025〕426号"
    assert hashlib.sha256((ROOT / src["original"]).read_bytes()).hexdigest() == src["original_sha256"]  # 原件未被替换


def test_demo_config_blocks_formal_acceptance():
    blockers = acceptance_blockers([OLD, NEW], DEMO_BASE)
    assert any("演示配置" in b for b in blockers) and any("计价基础" in b for b in blockers)
    assert any("pending" in b for b in acceptance_blockers([REAL.model_copy(update={"review_status": "pending"})], False))
    assert acceptance_blockers([REAL], False) == []
    assert acceptance_blockers([], False) == ["没有任何电价规则"]


@pytest.mark.parametrize("text", ["- a\n", "demo: false\nrules: [null]\n", "demo: [\n", "demo: false\nrules: [{rule_id: x}]\n"])
def test_malformed_yaml_raises_domain_error(tmp_path, text):
    f = tmp_path / "t.yaml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(c.PlantkgError):
        load_tariff_rules(f, "t.yaml")
    with pytest.raises(c.PlantkgError):
        load_tariff_config(tmp_path / "missing.yaml", "missing.yaml")


def test_yaml_must_declare_demo_and_reject_unknown_keys(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("rules: []\n", encoding="utf-8")
    with pytest.raises(c.PlantkgError, match="demo"):
        load_tariff_rules(f, "t.yaml")
    f.write_text((FIX / "demo_tou.yaml").read_text(encoding="utf-8").replace("flat_factor: 1.0", "flat_factor: 1.0\n    price: 1", 1),
                 encoding="utf-8")
    with pytest.raises(c.PlantkgError, match="未知键"):
        load_tariff_rules(f, "t.yaml")


# ---------------------------------------------------------------- 时段表


def test_cross_midnight_and_season_switch():
    rule = approved(REAL)
    # 春秋季平段 22:00-02:00 跨午夜；5-31 → 6-01 从春季表切到夏季表（01:00 由平变谷）
    ts = slots(local(2026, 5, 31, 21), 6 * 4)
    ps = price_schedule([rule], {"2026-05": BASE, "2026-06": BASE}, ts)
    by_hour = {(t.start.astimezone(SH).day, t.start.astimezone(SH).hour): p.period for t, p in zip(ts, ps)}
    assert by_hour[(31, 21)] == "peak" and by_hour[(31, 22)] == "flat" and by_hour[(31, 23)] == "flat"
    assert by_hour[(1, 0)] == "valley" and by_hour[(1, 1)] == "valley"  # 夏季 0-6 谷
    spring = price_schedule([rule], {"2026-04": BASE}, slots(local(2026, 4, 10, 0), 8))
    assert [p.period for p in spring[::4]] == ["flat", "flat"]  # 春季 0:00-2:00 仍属前一晚 22:00 起的平段
    assert price_schedule([rule], {"2026-04": BASE}, slots(local(2026, 4, 10, 2), 1))[0].period == "valley"


def test_sharp_overlay_price():
    ps = price_schedule([NEW], {"2026-07": BASE}, slots(local(2026, 7, 2, 13), 12))
    assert [p.period for p in ps[::2]] == ["flat", "flat", "sharp", "sharp", "peak", "peak"]
    assert ps[4].price_cny_per_kwh == pytest.approx(BASE * 1.8 * 1.2)
    assert ps[8].price_cny_per_kwh == pytest.approx(BASE * 1.8)


def test_holiday_deep_valley_price():
    rule, base = approved(REAL), {"2025-10": BASE, "2025-09": BASE, "2026-02": BASE, "2026-05": BASE, "2026-10": BASE, "2027-10": BASE}
    # 国庆 10:00：谷段 × 0.8 = 0.62 × 0.35 × 0.8 = 0.1736；前一天同一时刻仍是普通谷段 0.217
    at = lambda *t: price_schedule([rule], base, slots(local(*t), 1))[0]  # noqa: E731
    assert at(2025, 10, 1, 10).period == "deep_valley" and at(2025, 10, 1, 10).price_cny_per_kwh == pytest.approx(0.1736)
    assert at(2025, 9, 30, 10).period == "valley" and at(2025, 9, 30, 10).price_cny_per_kwh == pytest.approx(0.217)
    assert at(2025, 10, 8, 13, 45).period == "deep_valley" and at(2025, 10, 9, 10).period == "valley"  # 节日最后一天与次日
    assert at(2025, 10, 1, 9, 45).period == "flat" and at(2025, 10, 1, 14).period == "flat"  # 10:00-14:00 边界
    # 春节在冬季：10-11 原属平段，深谷照原文按谷段电价下浮 20%
    assert at(2026, 2, 17, 10).period == "deep_valley" and at(2026, 2, 17, 10).price_cny_per_kwh == pytest.approx(0.1736)
    assert acceptance_blockers([rule], False) == []  # 节日已编码，验收与定价一致
    assert at(2026, 5, 3, 12).period == "deep_valley"  # 五一
    with pytest.raises(c.PlantkgError, match="节日日历只登记到 2026-12-31"):
        price_schedule([rule], base, slots(local(2027, 10, 1, 10), 1))  # 未登记的年份拒绝定价，不按普通谷段计
    with pytest.raises(c.PlantkgError, match="节日日历只登记到 2026-12-31"):
        price_schedule([rule], {"2026-12": BASE}, slots(local(2026, 12, 31, 23, 30), 1, minutes=60))  # 跨过 through 的时段
    # 缺月份不是节日日历的问题，不能报成“节日日历只登记到”
    summer_only = with_periods(rule, [{**p, "dates": ["2026-02-17"]} if "dates" in p else p
                                      for p in rule.condition["periods"] if p.get("months") != [3, 4, 5, 9, 10, 11]])
    with pytest.raises(c.PlantkgError, match=r"不在规则 \S+ 的定义内（缺段）"):
        price_schedule([summer_only], {"2026-09": BASE}, slots(local(2026, 9, 2, 10), 1))


def test_version_switch_by_slot_start():
    ts = slots(local(2026, 7, 1, 23), 8)  # 23:00 → 次日 01:00，新版 07-02 00:00 生效
    ps = price_schedule([OLD, NEW], {"2026-07": BASE}, ts)
    assert [p.rule_id for p in ps] == ["demo-tou-old"] * 4 + ["demo-tou-new"] * 4
    assert ps[0].price_cny_per_kwh == pytest.approx(BASE)          # 旧版 22-24 平
    assert ps[4].price_cny_per_kwh == pytest.approx(BASE * 0.35)   # 新版 0-6 谷


@pytest.mark.parametrize("periods,reason", [
    ([{"name": "flat", "months": [7], "windows": [["00:00", "23:00"]], "ratio": 0.0, "locator": "x"}], "missing_price"),
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"},
      {"name": "peak", "months": [7], "windows": [["14:00", "15:00"]], "ratio": 0.8, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"},
      {"name": "sharp", "months": [7], "windows": [["14:00", "15:00"]], "ratio": 0.2, "locator": "x"}], "conflict"),  # 尖峰不在峰段内
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": -1.5, "locator": "x"}], "negative_price"),
    ([{"name": "shoulder", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0}], "conflict"),  # 无原文定位
    ([{"name": "flat", "months": [True], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7, 7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [[0, 1320]], "ratio": 0.0, "locator": "x"}], "conflict"),  # YAML 未加引号的 22:00
    # B25 抽取可能给出的非法类型：不得抛 TypeError
    ([{"name": "flat", "months": [[7]], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": ["peak"], "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": {"n": 1}, "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [{"m": 7}], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": 7, "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": "00:00-24:00", "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [[["00:00"], {"h": 24}]], "ratio": 0.0, "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": [0.0], "locator": "x"}], "conflict"),
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": "0", "locator": "x"}], "conflict"),
    ([["flat", [7]]], "conflict"),
    ({"name": "flat"}, "missing_price"),
    ([{"name": "deep_valley", "dates": [["2026-10-01"]], "through": "2026-12-31", "windows": [["10:00", "14:00"]], "ratio": -0.2,
       "locator": "x"}], "conflict"),
    ([{"name": "deep_valley", "dates": ["2026-10-01"], "through": ["2026-12-31"], "windows": [["10:00", "14:00"]], "ratio": -0.2,
       "locator": "x"}], "conflict"),
    ([{"name": "deep_valley", "dates": ["2027-10-01"], "through": "2026-12-31", "windows": [["10:00", "14:00"]], "ratio": -0.2,
       "locator": "x"}], "conflict"),  # 日期晚于 through
    ([{"name": "peak", "dates": ["2026-10-01"], "through": "2026-12-31", "windows": [["10:00", "14:00"]], "ratio": 0.8,
       "locator": "x"}], "conflict"),  # 基础时段不能按日期给
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x"},
      {"name": "deep_valley", "months": [7], "windows": [["15:00", "16:00"]], "ratio": -0.2, "locator": "x"}], "conflict"),  # 深谷须按日期
    ([{"name": "flat", "months": [7], "windows": [["00:00", "24:00"]], "ratio": 0.0, "locator": "x", "through": "2020-01-01"}],
     "conflict"),  # through 只能随 dates
])
def test_bad_schedule_becomes_pending_issue(periods, reason):
    kind = PACK.rule_kinds[0]
    issue = kind.compile(with_periods(NEW, periods), CTX)
    assert isinstance(issue, c.PendingIssue) and issue.reason == reason, issue


@pytest.mark.parametrize("tz", ["Asia", "Etc", "a" * 300, ["Asia/Shanghai"], 8, None])
def test_bad_timezone_becomes_pending_issue(tz):
    issue = PACK.rule_kinds[0].compile(NEW.model_copy(update={"condition": {**NEW.condition, "timezone": tz}}), CTX)
    assert isinstance(issue, c.PendingIssue) and issue.reason == "conflict", issue


def test_category_subject_and_unit_mismatch():
    kind = PACK.rule_kinds[0]
    other = NEW.model_copy(update={"condition": {**NEW.condition, "category": "single_lt100kva"}})
    assert kind.compile(other, CTX).reason == "conflict"
    unknown = NEW.model_copy(update={"condition": {**NEW.condition, "category": None}})
    assert kind.compile(unknown, CTX).reason == "subject_unknown"
    assert kind.compile(NEW.model_copy(update={"subject_ids": ("entity://demo-electronics/pv/PV1",)}), CTX).reason == "subject_unknown"
    assert kind.compile(NEW.model_copy(update={"unit": "CNY/kWh"}), CTX).reason == "unit_unknown"
    assert isinstance(kind.compile(NEW, CTX), c.ConstraintContribution)


def test_uncovered_overlapping_or_misaligned_slots_rejected():
    with pytest.raises(c.PlantkgError, match="没有电价规则覆盖"):
        price_schedule([NEW], {"2026-07": BASE}, slots(local(2026, 7, 1, 23), 8))  # 07-02 前无新版
    both = OLD.model_copy(update={"effective_to": None})
    with pytest.raises(c.PlantkgError, match="多条电价规则同时覆盖"):
        price_schedule([both, NEW], {"2026-07": BASE}, slots(local(2026, 7, 2, 0), 4))
    with pytest.raises(c.PlantkgError, match="计价基础"):
        price_schedule([NEW], {"2026-06": BASE}, slots(local(2026, 7, 2, 0), 4))
    with pytest.raises(c.PlantkgError, match="不在规则"):
        price_schedule([with_periods(NEW, NEW.condition["periods"][:3])], {"2026-09": BASE}, slots(local(2026, 9, 2, 0), 4))
    with pytest.raises(c.PlantkgError, match="60 分钟步长"):
        price_schedule([NEW], {"2026-07": BASE}, slots(local(2026, 7, 2, 19), 1, minutes=60))  # 19:00-20:00 跨 19:30 尖峰起点


# ---------------------------------------------------------------- 规划与账本


def test_hand_calc_four_slots_model_and_ledger():
    """当地 07-02 13:30-14:30，负荷恒 100 kW、无光伏无储能：13:30/13:45 平 0.62，14:00/14:15 尖峰 0.62×1.8×1.2 = 1.3392。
    每时段 25 kWh，手算 = 25×0.62×2 + 25×1.3392×2 = 31.00 + 66.96 = 97.96 元。"""
    start = local(2026, 7, 2, 13, 30)
    ts = slots(start, 4)
    horizon = c.Interval(start=start, end=ts[-1].end)
    load = forecast("fc-load", "base_load", [100.0] * 4).model_copy(update={"target": horizon})
    res, pl = solve([PACK], [NEW], horizon, c.ForecastBundle(forecasts=(load,)))
    assert res.solver_status == "optimal", res.validation_errors
    assert res.objective_value == pytest.approx(97.96, abs=1e-6)
    assert res.objective_terms == {"tariff.energy_cost": pytest.approx(97.96, abs=1e-6)}
    ps = PACK.prices([NEW], ts)
    m = pl.last_builder.model
    slot_in = [SlotInput(interval=t, pv_available_kw=0.0, load_kw=100.0, price_cny_per_kwh=p.price_cny_per_kwh,
                         reported_grid_import_kw=pyo.value(m.grid_import[k])) for k, (t, p) in enumerate(zip(ts, ps))]
    meta = LedgerMeta("led-tariff", "plan", CTX.run_id, res.plan.plan_id, 0, CTX.version_manifest_id, ("fc-load",))
    out = compute_ledger(slot_in, meta, None, objective_value_cny=res.objective_value)
    assert out.ledger.balanced, out.problems
    assert out.ledger.total_cost_cny == pytest.approx(97.96, abs=1e-6)
    rep = cost_report(ps, ts, [100.0] * 4)
    assert rep["by_period"]["sharp"]["cny"] == pytest.approx(66.96) and rep["peak_import_kw"] == 100.0


def test_full_day_with_storage_balances_and_keeps_rule_refs():
    packs = [FakePv(), FakeStorage(DAY["storage"]), PACK]
    res, pl = solve(packs, [OLD, NEW])  # 规划日 07-01 只落在旧版内，新版不在区间内被跳过
    assert res.solver_status == "optimal", res.validation_errors
    b = pl.last_builder
    assert b.state.constraints["tariff__price_demo_tou_old"].refs[0] == c.Ref(kind="rule", id="demo-tou-old")
    assert "tariff__price_demo_tou_new" not in b.state.constraints
    ts = b.time_set
    ps = PACK.prices([OLD], ts)
    ch, dis, cut = series(res.plan, "charge_power"), series(res.plan, "discharge_power"), series(res.plan, "pv_curtail")
    m = b.model
    slot_in = [SlotInput(interval=ts[k], pv_available_kw=DAY["pv_available_kw"][k], load_kw=DAY["base_load_kw"][k], charge_kw=ch[k],
                         discharge_kw=dis[k], pv_curtailed_kw=cut[k], price_cny_per_kwh=ps[k].price_cny_per_kwh,
                         reported_grid_import_kw=pyo.value(m.grid_import[k])) for k in range(96)]
    meta = LedgerMeta("led-day", "plan", CTX.run_id, res.plan.plan_id, 0, CTX.version_manifest_id, ("fc-pv", "fc-load"))
    p = DAY["storage"]
    params = c.StorageParams(capacity_kwh=p["capacity_kwh"], eta_c=p["eta_c"], eta_d=p["eta_d"], soc_min=p["soc_min"], soc_max=p["soc_max"])
    out = compute_ledger(slot_in, meta, params, SNAP.pack_states["storage"]["energy_kwh"], objective_value_cny=res.objective_value)
    assert out.ledger.balanced, out.problems[:5]
    peak_hours = [k for k in range(96) if ps[k].period == "peak"]
    assert sum(dis[k] for k in peak_hours) > 0  # 储能在峰段放电，电价确实驱动了计划


def test_unreviewed_or_invisible_rule_rejected_by_planner():
    day = c.Interval(start=local(2026, 7, 2), end=local(2026, 7, 3))
    load = forecast("fc-load", "base_load", [100.0] * 96).model_copy(update={"target": day})
    fc = c.ForecastBundle(forecasts=(load,))
    for rule, reason in [(REAL.model_copy(update={"effective_from": day.start, "review_status": "pending"}), "not_reviewed"),
                         (NEW.model_copy(update={"available_at": CTX.as_of + timedelta(hours=1)}), "not_visible")]:
        with pytest.raises(c.PlantkgError, match="没有电价规则覆盖"):  # 规则被筛掉后缺价，拒绝建模而不是按 0 元规划
            solve([PACK], [rule], day, fc)
        pl = SharedPlanner({"tariff": PACK}, lambda _id: SNAP, lambda _ids: fc, lambda ids: [rule])
        compiled = pl.rule_registry.compile([rule], CTX, day, ("tariff",))
        assert [i.reason for i in compiled.pending] == [reason]


def test_base_price_config_validated():
    with pytest.raises(c.PlantkgError, match="计价基础"):
        TariffPack(PACK.grid_entity, PACK.category, {"2026-7": 0.6}, PACK.config_ref)
    with pytest.raises(c.PlantkgError, match="计价基础"):
        TariffPack(PACK.grid_entity, PACK.category, {"2026-07": -0.1}, PACK.config_ref)


def test_extreme_factor_rejected_before_solve():
    """极大有限系数过去只查 factor < 0，乘积溢出成 inf 后规划以 unbounded 失败（#130）。"""
    huge = REAL.model_copy(update={"value": 1e308})
    parsed = parse_schedule(huge)
    assert isinstance(parsed, tuple) and parsed[0] == "conflict" and "工程范围" in parsed[1]
    with pytest.raises(c.PlantkgError, match="工程范围"):
        price_schedule([huge], {"2026-07": BASE}, slots(local(2026, 7, 1, 14), 4))


def test_price_product_overflow_rejected_before_solve():
    with pytest.raises(c.PlantkgError, match="工程上界"):
        price_schedule([REAL], {"2026-07": 1e308}, slots(local(2026, 7, 1, 14), 4))
