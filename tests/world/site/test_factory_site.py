"""B18 工厂装配：设备与拓扑、D05 底座取用与时间映射、负荷合成一致性、数据性质与溯源。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from plantkg.world.site import SiteModel
from plantkg.world.site.loads import UNMETERED, synthesize
from plantkg.world.site.sources import d05

ROOT = Path(__file__).resolve().parents[3]
SITE = ROOT / "configs/sites/factory/site.yaml"
SAMPLE = ROOT / "fixtures/sites/factory/d05_week.parquet"
UTC = timezone.utc
DUBLIN, SHANGHAI = ZoneInfo("Europe/Dublin"), ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def site():
    return SiteModel.load(SITE)


@pytest.fixture(scope="module")
def data():
    return d05.load_d05(SAMPLE)


@pytest.fixture(scope="module")
def loads(site, data):
    return synthesize(site, data, 7)


# ---------------------------------------------------------------- 装配与拓扑


def test_equipment_and_floors_match_config(site):
    raw = yaml.safe_load(SITE.read_text(encoding="utf-8"))
    expected = sum(raw["zones"][e["zone"]]["floors"] if e.get("per_floor") else 1 for e in raw["equipment"])
    assert len(site.equipment) == expected
    assert site.zones["admin"]["floors"] == 12
    admin_floors = {e.floor for e in site.equipment if e.code.startswith("LT-A")}
    assert admin_floors == set(range(1, 13))
    by_type = lambda t: [e for e in site.equipment if e.type == t]
    assert len(by_type("passenger_elevator")) == 6 and len(by_type("freight_elevator")) == 4
    assert len(by_type("air_compressor")) == 3
    assert {e.zone for e in site.equipment} >= {"production", "admin", "cleanroom", "hazmat", "datacenter", "park"}


def test_every_device_reaches_grid_through_a_transformer(site):
    for e in site.equipment:
        path = site.path(e.code)
        assert path[0] == e.code and path[-1] == "GRID"
        assert site.transformer_of(e.code).capacity_kva > 0
    assert site.path("AC-1") == ["AC-1", "P-AIR", "P-PROD", "LV1", "TX1", "GRID"]
    assert site.transformer_of("DC-IT1").id == "TX2"


@pytest.mark.parametrize("edit, message", [
    (lambda d: d["distribution"]["nodes"].append({"id": "P-X", "type": "panel", "parent": "NOWHERE", "label": "x"}), "断开"),
    (lambda d: d["distribution"]["nodes"][1].update(parent="P-PROD"), "成环"),
    (lambda d: d["equipment"][0].update(panel="TX1"), "不是配电柜"),
    (lambda d: d["distribution"]["nodes"][1].pop("capacity_kva"), "容量"),
    (lambda d: d["distribution"]["nodes"][4].update(parent="GRID"), "没有变压器"),
    (lambda d: d["equipment"][0].update(zone="admin"), "配电柜"),
    (lambda d: d["equipment"][0].update(load="d05:unknown"), "负荷分项"),
    (lambda d: d["equipment"][0].update(rated_kw=0), "额定功率"),
])
def test_broken_topology_is_rejected(tmp_path, edit, message):
    raw = yaml.safe_load(SITE.read_text(encoding="utf-8"))
    edit(raw)
    bad = tmp_path / "site.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        SiteModel.load(bad)


def test_entities_follow_b06_identity(site):
    entities = site.entities()
    assert all(e.entity_id.startswith("entity://demo-electronics/") for e in entities)
    ids = {e.entity_id for e in entities}
    assert "entity://demo-electronics/pv/PV1" in ids and "entity://demo-electronics/battery/BAT1" in ids


def test_pv_and_storage_configs_load_in_b10_b11_format():
    from plantkg.packs.pv import load_pv_config
    from plantkg.packs.storage.pack import load_storage_config

    pv = load_pv_config(ROOT / "configs/sites/factory/pv.yaml", "factory")
    storage = load_storage_config(ROOT / "configs/sites/factory/storage.yaml", "factory")
    assert pv.kwp == 1000
    assert (storage.capacity_kwh, storage.charge_kw, storage.discharge_kw) == (2000, 500, 500)


# ---------------------------------------------------------------- D05 小样本与取用规则


def test_sample_has_only_complete_dublin_days_after_mains_start(data):
    days = {}
    for t in data.readings["mi_a"]:
        days.setdefault(t.astimezone(DUBLIN).date(), 0)
        days[t.astimezone(DUBLIN).date()] += 1
    assert len(days) == 7 and set(days.values()) == {96}
    assert min(days) > datetime(2025, 5, 22).date()


def test_faulty_and_unused_meters_are_excluded(data):
    rules = yaml.safe_load(SITE.read_text(encoding="utf-8"))["production_base"]
    t = next(iter(sorted(data.readings["mi_a"])))
    d = d05.decompose(data, t, rules)
    assert d["excluded"]["ahu_a"] == d["excluded"]["ahu_b"] == "excluded_meter"
    assert d["excluded"]["u_a"].startswith("quality_event:")  # 反接 CT，2025-12-10 才修复
    assert d["excluded"]["p_m"].startswith("quality_event:")
    used = {a for m in d["meters"].values() for a in m}
    assert not used & {"sb_a", "sb_b", "sb_c", "hp_a", "hp_b", "mi_b", "mi_a"}
    assert {"comp_a", "comp_b"} <= set(d["meters"]["compressed_air"])
    assert set(d["meters"]["hvac"]) <= {"ex_a", "ex_b", "ahu_c"}


def test_residual_nonnegative_and_items_sum_to_base_times_multiplier(loads, site):
    m = site.config["production_base"]["multiplier"]
    for slot in loads.base_slots:
        d, s = slot["decomposition"], slot["scaled"]
        assert d["residual"] >= 0
        assert d["total"] == pytest.approx(max(d["net_import"], d["leaf_sum"]))
        parts = sum(v for k, v in s.items() if k != "total")
        assert parts == pytest.approx(d["total"] * m) == pytest.approx(s["total"])


def test_leaf_sum_above_net_import_becomes_base_with_zero_residual(data, site):
    rules = site.config["production_base"]
    t = next(iter(sorted(data.readings["mi_a"])))
    readings = {a: dict(v) for a, v in data.readings.items()}
    readings["mi_a"][t] = (1.0, True)  # 人为让总进线远低于叶子合计
    fake = d05.D05Data(readings=readings, asset_types=data.asset_types, events=data.events,
                       sha256=data.sha256, source_file=data.source_file)
    d = d05.decompose(fake, t, rules)
    assert d["basis"] == "leaf_sum" and d["residual"] == 0 and d["total"] == pytest.approx(d["leaf_sum"])
    slot = {"decomposition": d, "source": t, "handling": "direct"}
    step = next(s for s in d05.base_steps(fake, rules, site.config["time_map"], slot, "residual") if s.op == "d05_gross_base")
    assert step.params["basis"] == "leaf_sum" and step.params["net_import_kw"] == 1.0


# ---------------------------------------------------------------- 时间映射


def test_dublin_wall_clock_maps_to_same_shanghai_wall_clock(loads, data):
    tm = loads.site.config["time_map"]
    target = datetime(2026, 7, 1, 7, 0, tzinfo=SHANGHAI).astimezone(UTC)
    src, handling = d05.source_slot(target, tm)
    assert src.astimezone(DUBLIN).replace(tzinfo=None) == datetime(2025, 6, 4, 7, 0) and handling == "direct"
    i = loads.slots.index(target)
    assert loads.base_slots[i]["decomposition"]["net_import"] == data.readings["mi_a"][src][0]


def test_weekday_morning_shift_lands_in_plant_morning(loads, data):
    """D05 周三当地 06:00～08:00 的负荷曲线，原样出现在厂区周三 06:00～08:00。"""
    m = loads.site.config["production_base"]["multiplier"]
    for hour in (6, 7):
        target = datetime(2026, 7, 1, hour, tzinfo=SHANGHAI).astimezone(UTC)
        src = datetime(2025, 6, 4, hour, tzinfo=DUBLIN).astimezone(UTC)
        i = loads.slots.index(target)
        expected = max(data.readings["mi_a"][src][0], loads.base_slots[i]["decomposition"]["leaf_sum"]) * m
        assert loads.base_slots[i]["scaled"]["total"] == pytest.approx(expected)
        assert target.astimezone(SHANGHAI).weekday() == src.astimezone(DUBLIN).weekday() == 2


def test_dst_repeat_keeps_first_and_missing_hour_is_missing():
    autumn = {"source_start": "2025-10-26", "plant_start": "2026-10-26", "source_tz": "Europe/Dublin", "plant_tz": "Asia/Shanghai"}
    src, handling = d05.source_slot(datetime(2026, 10, 26, 1, 15, tzinfo=SHANGHAI).astimezone(UTC), autumn)
    assert handling == "keep_first" and src == datetime(2025, 10, 26, 0, 15, tzinfo=UTC)  # 第一次 01:15 是 IST（UTC+1）
    spring = {**autumn, "source_start": "2025-03-30", "plant_start": "2026-03-30"}
    src, handling = d05.source_slot(datetime(2026, 3, 30, 1, 30, tzinfo=SHANGHAI).astimezone(UTC), spring)
    assert src is None and handling == "source_wall_time_missing"


# ---------------------------------------------------------------- 合成负荷一致性与人流


def test_site_total_equals_sum_of_items_zones_and_transformers(loads, site):
    items = [e.code for e in site.equipment if e.load != "none"] + [UNMETERED]
    zones = [k for k in loads.series if k.startswith("zone_total:")]
    txs = [k for k in loads.series if k.startswith("transformer_load:")]
    for i in range(len(loads.slots)):
        total = loads.total(i)
        assert total == pytest.approx(sum(loads.series[k][i] for k in items))
        assert total == pytest.approx(sum(loads.series[k][i] for k in zones))
        assert total == pytest.approx(sum(loads.series[k][i] for k in txs))
    for tx in ("TX1", "TX2"):
        assert max(loads.series[f"transformer_load:{tx}"]) <= site.transformer_limit_kw(tx)


def test_d05_category_split_by_rated_power(loads):
    for i, slot in enumerate(loads.base_slots):
        air = sum(loads.series[c][i] for c in ("AC-1", "AC-2", "AC-3"))
        assert air == pytest.approx(slot["scaled"]["compressed_air"])
        assert loads.series["AC-1"][i] == pytest.approx(loads.series["AC-3"][i])


def test_people_flow_has_morning_and_evening_peaks(loads):
    def at(day, hh, mm):
        t = datetime(2026, 7, day, hh, mm, tzinfo=SHANGHAI).astimezone(UTC)
        return loads.slots.index(t)
    occ, trips = loads.series["occupancy:admin"], loads.series["trips:passenger_elevator"]
    assert occ[at(1, 7, 0)] < 0.1 * occ[at(1, 10, 0)]
    assert occ[at(1, 20, 0)] < 0.2 * occ[at(1, 16, 0)]
    morning = max(trips[at(1, 8, 0):at(1, 9, 0)])
    evening = max(trips[at(1, 17, 30):at(1, 18, 45)])
    assert morning > 5 * trips[at(1, 10, 30)] and evening > 5 * trips[at(1, 15, 0)]
    assert occ[at(4, 10, 0)] < 0.2 * occ[at(1, 10, 0)]  # 2026-07-04 周六
    assert loads.tasks and all(t["nature"] == "synthetic" for t in loads.tasks)


# ---------------------------------------------------------------- 数据性质与溯源


def test_raw_d05_records_are_measured_and_marked_external(data):
    start = min(data.readings["mi_a"])
    bundle = d05.measured_bundle(data, ["mi_a", "comp_a"], start, start + timedelta(hours=2))
    assert len(bundle.records) == 16
    assert {r.nature for r in bundle.records} == {"measured"}
    assert "非本厂" in bundle.note and "非本厂" in bundle.records[0].transform_steps[0].note
    r = bundle.records[0]
    assert r.event_time == start and r.available_at >= r.interval.end and r.locator.meter in ("mi_a", "comp_a")


def test_scaled_records_are_derived_and_trace_back_to_d05(loads, site):
    bundle = loads.to_bundle()
    by_key = {}
    for r in bundle.records:
        by_key.setdefault(r.locator.meter, []).append(r)
    assert all(r.nature == "derived" for r in by_key["AC-1"] + by_key[UNMETERED] + by_key["site_total"])
    assert all(r.nature != "measured" for r in bundle.records)  # 场景输入不含 measured
    assert by_key["occupancy:admin"][0].nature == "synthetic" and by_key["PE-1"][0].nature == "simulated"
    rec = by_key["AC-1"][40]
    ops = {s.op: s.params for s in rec.transform_steps}
    assert set(ops["d05_select_meters"]["meters"]) == {"comp_a", "comp_b"}
    assert ops["d05_select_meters"]["raw_kw"]
    assert ops["scale_by_declared_multiplier"] == {"multiplier": 2.0, "multiplier_nature": "configured", "item": "compressed_air"}
    assert ops["time_map_local_wall_clock"]["source_slot_utc"]
    assert ops["allocate_by_rated_power"]["share"] == pytest.approx(1 / 3)
    assert rec.content_hash == "sha256:" + loads.data.sha256 and "D05" in bundle.note
    assert all(r.available_at >= r.interval.end for r in bundle.records)


# ---------------------------------------------------------------- 与 B03 存储、幂等和可见时间的兼容


def _store(tmp_path):
    from plantkg.storage.migration import MigrationRunner
    from plantkg.storage.series import ParquetSeriesStore
    from plantkg.storage.sqlite import SQLiteWriter

    writer = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(writer).apply()
    refs = []
    return writer, refs, ParquetSeriesStore(tmp_path / "series", writer, "demo-electronics", lambda context, quantity: refs)


def test_scene_bundles_write_to_b03_and_read_back(tmp_path, site, data):
    from plantkg.core.contracts import Context, Interval

    day = synthesize(site, data, 1)
    bundle = day.to_bundle()
    assert {r.nature for r in bundle.records} == {"derived", "synthetic", "simulated"}
    assert {(r.source_id, r.source_version) for r in bundle.records} == {(bundle.source_id, bundle.source_version)}
    writer, refs, store = _store(tmp_path)
    try:
        ref = store.write_partition(bundle)
        assert store.write_partition(day.to_bundle()) == ref  # 同参重试幂等
        start = min(data.readings["mi_a"])
        for extra in (d05.base_bundle(data, site.config["production_base"], day.time_map, 1),
                      d05.measured_bundle(data, ["mi_a"], start, start + timedelta(hours=1))):
            store.write_partition(extra)
        refs.append(ref)
        context = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=None, channel="none",
                          trace_id="t", as_of=day.slots[-1] + timedelta(days=1), version_manifest_id="vm-1")
        back = store.query(context, "active_power", Interval(start=day.slots[0], end=day.slots[-1] + timedelta(minutes=15)))
        assert len(back.records) == sum(1 for r in bundle.records if r.quantity == "active_power")
        assert {r.nature for r in back.records} == {"derived", "synthetic", "simulated"}
    finally:
        writer.close()


def test_bundle_ids_change_with_every_effective_parameter(tmp_path, site, data):
    import copy

    base = site.config["production_base"]
    day = synthesize(site, data, 1)
    other_day = synthesize(site, data, 1, plant_start="2026-07-02")
    ids = {day.to_bundle().bundle_id, day.to_bundle(delay_seconds=300).bundle_id, other_day.to_bundle().bundle_id}
    assert len(ids) == 3 and day.to_bundle().bundle_id == synthesize(site, data, 1).to_bundle().bundle_id
    doubled = copy.deepcopy(base) | {"multiplier": 3.0}
    base_ids = {d05.base_bundle(data, b, day.time_map, 1).bundle_id for b in (base, doubled)}
    start = min(data.readings["mi_a"])
    hour = start + timedelta(hours=1)
    measured_ids = {d05.measured_bundle(data, m, start, end, delay).bundle_id
                    for m, end, delay in ((["mi_a"], hour, 0), (["comp_a"], hour, 0), (["mi_a"], hour + timedelta(hours=1), 0), (["mi_a"], hour, 60))}
    assert len(base_ids) == 2 and len(measured_ids) == 4
    writer, _, store = _store(tmp_path)
    try:
        for b in (day.to_bundle(), day.to_bundle(delay_seconds=300)):
            store.write_partition(b)  # 不同内容各自入库，不触发 IDEMPOTENCY_MISMATCH
    finally:
        writer.close()


def test_configured_delay_applies_to_base_devices_and_totals(tmp_path, data):
    raw = yaml.safe_load(SITE.read_text(encoding="utf-8"))
    raw["production_base"]["delay_seconds"] = 300
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    delayed = SiteModel.load(path)
    day = synthesize(delayed, data, 1)
    late = timedelta(minutes=15, seconds=300)
    for r in day.to_bundle().records:
        assert r.available_at == r.event_time + late
    for r in d05.base_bundle(data, delayed.config["production_base"], day.time_map, 1).records:
        assert r.available_at == r.event_time + late


# ---------------------------------------------------------------- HIPE 回退底座


def _hipe_bundle(tmp_path):
    from plantkg.adapters import HipeAdapter

    berlin = ZoneInfo("Europe/Berlin")
    rows = ["SensorDateTime,Machine,P_kW"]
    t = datetime(2017, 10, 23, tzinfo=berlin)
    while t < datetime(2017, 10, 24, 0, 1, tzinfo=berlin):
        rows.append(f"{t.isoformat()},MainTerminal,{10 + 6 * (8 <= t.hour < 18):.1f}")
        t += timedelta(minutes=1)
    csv_path = tmp_path / "main.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return HipeAdapter().normalize(str(csv_path), {"source_start": "2017-10-23", "plant_start": "2026-07-01", "days": 1,
                                                   "load_scale": {"factor": 25}}, {"delay_seconds": 600})


def test_hipe_fallback_base_splits_by_configured_shares(tmp_path, site):
    hipe_bundle = _hipe_bundle(tmp_path)
    fallback = site.config["production_base"]["hipe_fallback"]
    day = synthesize(site, hipe_bundle, 1)
    assert day.base_kind == "hipe" and len(day.slots) == 96
    complete = [i for i, s in enumerate(day.base_slots) if s["scaled"]]
    assert len(complete) >= 95
    for i in complete:
        s = day.base_slots[i]
        assert s["scaled"]["total"] == pytest.approx(s["record"].value * fallback["multiplier"])
        assert s["scaled"]["compressed_air"] == pytest.approx(s["scaled"]["total"] * fallback["category_shares"]["compressed_air"])
        assert sum(v for k, v in s["scaled"].items() if k != "total") == pytest.approx(s["scaled"]["total"])
        assert day.total(i) == pytest.approx(sum(day.series[k][i] for k in day.series if k.startswith("zone_total:")))
    bundle = day.to_bundle()
    rec = next(r for r in bundle.records if r.locator.meter == "AC-1" and r.value is not None)
    ops = {s.op: s.params for s in rec.transform_steps}
    assert ops["hipe_fallback_select"]["bundle_id"] == hipe_bundle.bundle_id
    assert ops["split_by_configured_shares"]["shares_nature"] == "configured"
    source = next(s["record"] for s in day.base_slots if s["target"] == rec.event_time)
    assert rec.available_at >= source.available_at and rec.content_hash == source.content_hash
    assert {(r.source_id, r.source_version) for r in bundle.records} == {(bundle.source_id, bundle.source_version)}


def test_measured_bundle_ignores_meter_order(tmp_path, data):
    start = min(data.readings["mi_a"])
    a = d05.measured_bundle(data, ["mi_a", "comp_a"], start, start + timedelta(hours=1))
    b = d05.measured_bundle(data, ["comp_a", "mi_a", "comp_a"], start, start + timedelta(hours=1))
    assert a == b
    writer, _, store = _store(tmp_path)
    try:
        assert store.write_partition(a) == store.write_partition(b)
    finally:
        writer.close()
