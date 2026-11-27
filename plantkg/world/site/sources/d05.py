"""D05 工厂设备级用电（Flynn et al. 2026，Zenodo 19180972，CC BY 4.0）→ B01 SourceRecord。

D05 是爱尔兰一家约 150 人连续生产的工厂，不是本厂。原始记录标 measured、注明外部工厂；
映射到厂区时间并乘声明倍数后的生产区基础负荷与分项标 derived，倍数本身是 configured。

读法（取舍理由见 plantkg/world/site/IMPLEMENTATION.md）：
- 功率取 energy_kwh_15m 的 ALL 相 × 4（kW）。avg_power_kw_15m 的 ALL 相实为三相平均，不用。
- 只用 is_reliable_window=1 的时段；24 条计量故障记录覆盖的表计时段剔除（这些记录留给 B22 作事件素材）。
- 叶子表计按资产类型归入分项；配电柜、热泵、光伏表和总进线不算叶子。
- mi_a 是扣掉该厂自有光伏后的净购电（原包 EdgeList 与光伏校验可证；Gold 层光伏表 mi_b 读数接近 0），
  白天会低于叶子合计。为不把对方光伏的午间凹陷带进本厂负荷，底座总量取 max(mi_a, 叶子合计)：
  叶子未超时总量 = mi_a；超出时总量 = 叶子合计、余项为 0。取用依据写入 transform_steps。
  未计量余项 = 底座总量 − 分项合计，必然 ≥ 0。（任务卡原定"叶子超出时按比例缩到 mi_a"，2026-11-04 改为此规则。）
- 时间映射同 B05：UTC → Europe/Dublin 钟面 → 平移日期 → Asia/Shanghai 同一钟面；
  源重复的一小时只取第一次（keep_first），源不存在的一小时在厂区标 missing。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plantkg.core.contracts import Interval, Locator, Quality, SourceBundle, SourceRecord, TransformStep

UTC = timezone.utc
SLOT = timedelta(minutes=15)
SOURCE_ID = "d05-ireland-factory"
SOURCE_VERSION = "zenodo-19180972-v1.0"
SOURCE_NOTE = "爱尔兰工厂 D05，非本厂（Flynn et al. 2026, Zenodo 19180972, CC BY 4.0）"
LICENSE = "CC BY 4.0"
TOTAL_METER = "mi_a"
NOT_LEAF_TYPES = {"Electrical_GridImport", "Electrical_PVGeneration", "Electrical_Distribution", "HVAC_HeatPump"}
VERSION = "b18-d05-1"
ENERGY_ALL_SIGNAL = 4  # energy_kwh_15m / ALL


@dataclass(frozen=True)
class QualityEvent:
    event_id: int
    asset_id: str
    issue_type: str
    start: datetime
    end: datetime


@dataclass
class D05Data:
    readings: dict[str, dict[datetime, tuple[float, bool]]]  # asset → 时段起点 UTC → (kW, 可靠)
    asset_types: dict[str, str]
    events: tuple[QualityEvent, ...]
    sha256: str
    source_file: str


def _utc(t) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def fingerprint(params: dict) -> str:
    """影响输出内容的全部有效参数 → 稳定短指纹，用作 bundle_id 后缀。同参同 ID，参数不同 ID 不同。"""
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def load_d05(path: str | Path, start: datetime | None = None, end: datetime | None = None) -> D05Data:
    """读取 D05：星型表目录（dim/、fact/）或本仓库一周小样本 parquet。start/end 只为少读数据。"""
    import pyarrow.parquet as pq

    path = Path(path)
    window = [("time", ">=", start.astimezone(UTC).replace(tzinfo=None))] if start else []
    window += [("time", "<", end.astimezone(UTC).replace(tzinfo=None))] if end else []
    readings: dict[str, dict] = {}
    if path.is_dir():
        fact = path / "fact" / "fact_energy_15m.parquet"
        reliable = {r["observation_window_id"]: r["is_reliable_window"] == 1
                    for r in pq.read_table(path / "dim" / "dim_observation_window.parquet").to_pylist()}
        types = {r["asset_id"]: r["asset_type"] for r in pq.read_table(path / "dim" / "dim_asset.parquet").to_pylist()}
        events = [QualityEvent(r["event_id"], r["asset_id"], r["issue_type"], _utc(r["issue_start"]), _utc(r["issue_end"]))
                  for r in pq.read_table(path / "fact" / "fact_data_quality_event.parquet").to_pylist()]
        table = pq.read_table(fact, filters=[("signal_id", "=", ENERGY_ALL_SIGNAL)] + window,
                              columns=["asset_id", "time", "value_float", "observation_window_id"])
        for a, t, v, w in zip(*(table.column(c).to_pylist() for c in table.column_names)):
            readings.setdefault(a, {})[_utc(t)] = (v * 4, reliable[w])
        digest, name = _sha256(fact), "fact/fact_energy_15m.parquet"
    else:
        table = pq.read_table(path, filters=window or None)
        meta = json.loads(table.schema.metadata[b"d05"])
        types = meta["asset_types"]
        events = [QualityEvent(e["event_id"], e["asset_id"], e["issue_type"], _utc(datetime.fromisoformat(e["start"])),
                               _utc(datetime.fromisoformat(e["end"]))) for e in meta["quality_events"]]
        for a, t, kwh, ok in zip(*(table.column(c).to_pylist() for c in ("asset_id", "time", "energy_kwh", "is_reliable_window"))):
            readings.setdefault(a, {})[_utc(t)] = (kwh * 4, bool(ok))
        digest, name = _sha256(path), path.name
    return D05Data(readings=readings, asset_types=types, events=tuple(events), sha256=digest, source_file=name)


# ---------------------------------------------------------------- 表计取用与分项归一


def exclusion(data: D05Data, asset: str, t: datetime, rules: dict) -> str | None:
    """返回该表计该时段不能用的原因；能用返回 None。"""
    if asset in rules.get("exclude_meters", ()):
        return "excluded_meter"
    since = rules.get("meter_valid_from", {}).get(asset)
    if since and t < _utc(datetime.fromisoformat(since.replace("Z", "+00:00"))):
        return "before_valid_from"
    reading = data.readings.get(asset, {}).get(t)
    if reading is None:
        return "no_reading"
    if not reading[1]:
        return "unreliable_window"
    for e in data.events:
        if e.asset_id == asset and t < e.end and t + SLOT > e.start:
            return f"quality_event:{e.event_id}"
    return None


def category_of(data: D05Data, asset: str, rules: dict) -> str | None:
    kind = data.asset_types.get(asset)
    if kind in NOT_LEAF_TYPES:
        return None
    return next((c for c, kinds in rules["categories"].items() if kind in kinds), None)


def decompose(data: D05Data, t: datetime, rules: dict) -> dict:
    """单个源时段：净购电、底座总量及取用依据、各分项、未计量余项和剔除原因。总进线不可用时 total=None。"""
    excluded = {}
    total = None
    why = exclusion(data, TOTAL_METER, t, rules)
    if why is None:
        total = data.readings[TOTAL_METER][t][0]
    else:
        excluded[TOTAL_METER] = why
    meters: dict[str, dict[str, float]] = {c: {} for c in rules["categories"]}
    for asset in sorted(data.readings):
        cat = category_of(data, asset, rules)
        if cat is None:
            continue
        why = exclusion(data, asset, t, rules)
        if why:
            excluded[asset] = why
        else:
            meters[cat][asset] = data.readings[asset][t][0]
    leaf_sum = sum(v for m in meters.values() for v in m.values())
    if total is None:
        return {"t": t, "net_import": None, "total": None, "basis": None, "meters": meters, "leaf_sum": leaf_sum,
                "categories": {c: None for c in meters}, "residual": None, "excluded": excluded}
    categories = {c: sum(m.values()) for c, m in meters.items()}
    gross = max(total, leaf_sum)
    return {"t": t, "net_import": total, "total": gross, "basis": "leaf_sum" if leaf_sum > total else "mi_a",
            "meters": meters, "leaf_sum": leaf_sum, "categories": categories,
            "residual": 0.0 if leaf_sum > total else total - leaf_sum, "excluded": excluded}


# ---------------------------------------------------------------- 时间映射（同 B05 time_map）


def source_slot(target: datetime, time_map: dict) -> tuple[datetime | None, str]:
    """厂区时段起点（UTC）→ 源时段起点（UTC）。返回 (源时段, 处理说明)；源钟面不存在时为 None。"""
    src_tz, dst_tz = ZoneInfo(time_map["source_tz"]), ZoneInfo(time_map["plant_tz"])
    shift = date.fromisoformat(time_map["source_start"]) - date.fromisoformat(time_map["plant_start"])
    local = target.astimezone(dst_tz)
    wall = datetime.combine(local.date() + shift, local.time().replace(tzinfo=None))
    candidate = wall.replace(tzinfo=src_tz, fold=0)
    back = candidate.astimezone(UTC).astimezone(src_tz)
    if back.replace(tzinfo=None) != wall:
        return None, "source_wall_time_missing"  # 春季拨快跳过的一小时
    repeated = wall.replace(tzinfo=src_tz, fold=1).utcoffset() != candidate.utcoffset()
    return candidate.astimezone(UTC), "keep_first" if repeated else "direct"


def plant_slots(time_map: dict, days: int) -> list[datetime]:
    tz = ZoneInfo(time_map["plant_tz"])
    start = datetime.combine(date.fromisoformat(time_map["plant_start"]), time.min, tz).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(time_map["plant_start"]) + timedelta(days=days), time.min, tz).astimezone(UTC)
    out, t = [], start
    while t < end:
        out.append(t)
        t += SLOT
    return out


def time_map_step(time_map: dict, source: datetime | None, handling: str) -> TransformStep:
    return TransformStep(op="time_map_local_wall_clock", version=VERSION, params={
        **time_map, "source_slot_utc": source.isoformat() if source else None,
        "source_slot_local": source.astimezone(ZoneInfo(time_map["source_tz"])).isoformat() if source else None,
        "dst_handling": handling})


# ---------------------------------------------------------------- 记录


def _record(*, digest, locator, nature, start, available, quantity, value, steps, unit="kW", flag=None, note=None):
    return SourceRecord(source_id=SOURCE_ID, source_version=SOURCE_VERSION, content_hash="sha256:" + digest,
                        locator=locator, nature=nature, event_time=start, interval=Interval(start=start, end=start + SLOT),
                        available_at=available, source_tz="Europe/Dublin", source_unit="kWh/15min", quantity=quantity,
                        unit=unit, value=value, transform_steps=tuple(steps),
                        quality=Quality(flag=flag or ("missing" if value is None else "ok"), note=note))


def measured_bundle(data: D05Data, meters: list[str], start: datetime, end: datetime, delay_seconds: float = 0) -> SourceBundle:
    """原始 D05 记录：UTC 时段、measured、注明外部工厂。不可靠窗口标 suspect 并保留数值。"""
    delay = timedelta(seconds=delay_seconds)
    meters = sorted(set(meters))  # 记录顺序与包 ID 共用同一规范化序列，传入顺序不影响输出
    out = []
    for asset in meters:
        for t, (kw, ok) in sorted(data.readings.get(asset, {}).items()):
            if not start <= t < end:
                continue
            step = TransformStep(op="d05_energy_to_mean_kw", version=VERSION, note=SOURCE_NOTE, params={
                "source_file": data.source_file, "asset_id": asset, "asset_type": data.asset_types.get(asset),
                "signal": "energy_kwh_15m/ALL", "formula": "kW = kWh × 4", "is_reliable_window": ok})
            out.append(_record(digest=data.sha256, locator=Locator(meter=asset, time_range=Interval(start=t, end=t + SLOT)),
                               nature="measured", start=t, available=t + SLOT + delay, quantity="active_power",
                               value=kw, steps=[step], flag=None if ok else "suspect",
                               note=None if ok else "is_reliable_window=0"))
    key = {"data": data.sha256, "meters": meters, "start": start.isoformat(), "end": end.isoformat(),
           "delay_seconds": float(delay_seconds), "version": VERSION}
    return SourceBundle(bundle_id=f"b18-d05-measured-{fingerprint(key)}", source_id=SOURCE_ID, source_version=SOURCE_VERSION,
                        mapping_version=VERSION, records=tuple(out), license=LICENSE, note=SOURCE_NOTE)


def production_base(data: D05Data, base: dict, time_map: dict, days: int) -> list[dict]:
    """厂区每个 15 分钟：源时段、时间映射说明、分解结果 × 倍数。供 base_bundle 与装配负荷合成共用。"""
    m = float(base["multiplier"])
    if not m > 0:
        raise ValueError("production_base.multiplier 必须为正数")
    out = []
    for target in plant_slots(time_map, days):
        src, handling = source_slot(target, time_map)
        d = decompose(data, src, base) if src and TOTAL_METER in data.readings and src in data.readings[TOTAL_METER] else None
        scaled = None
        if d and d["total"] is not None:
            scaled = {"total": d["total"] * m, "residual": d["residual"] * m,
                      **{c: v * m for c, v in d["categories"].items()}}
        out.append({"kind": "d05", "target": target, "source": src, "handling": handling, "decomposition": d, "scaled": scaled})
    return out


def base_steps(data: D05Data, base: dict, time_map: dict, slot: dict, item: str) -> list[TransformStep]:
    d = slot["decomposition"]
    select = TransformStep(op="d05_select_meters", version=VERSION, note=SOURCE_NOTE, params={
        "source_id": SOURCE_ID, "source_version": SOURCE_VERSION, "source_sha256": data.sha256,
        "source_file": data.source_file, "total_meter": TOTAL_METER,
        "meters": sorted(d["meters"].get(item, {})) if d and item in d["meters"] else
                  ([TOTAL_METER] if item in ("total", "residual") else []),
        "raw_kw": d["meters"].get(item) if d and item in d["meters"] else None,
        "net_import_kw": d["net_import"] if d else None, "excluded": d["excluded"] if d else None,
        "rule": "is_reliable_window=1，剔除计量故障区间与不用的表计"})
    gross = TransformStep(op="d05_gross_base", version=VERSION, params={
        "net_import_kw": d["net_import"] if d else None, "leaf_sum_kw": d["leaf_sum"] if d else None,
        "base_total_kw": d["total"] if d else None, "basis": d["basis"] if d else None,
        "rule": "底座总量 = max(mi_a 净购电, 叶子合计)；余项 = 底座总量 − 分项合计 ≥ 0"})
    scale = TransformStep(op="scale_by_declared_multiplier", version=VERSION, params={
        "multiplier": float(base["multiplier"]), "multiplier_nature": "configured", "item": item})
    return [select, gross, time_map_step(time_map, slot["source"], slot["handling"]), scale]


def base_bundle(data: D05Data, base: dict, time_map: dict, days: int, slots: list[dict] | None = None) -> SourceBundle:
    """生产区基础负荷：总量、四个分项与未计量余项，均为厂区时间、× 倍数、derived。"""
    slots = slots if slots is not None else production_base(data, base, time_map, days)
    delay = timedelta(seconds=float(base.get("delay_seconds", 0)))
    items = ["total", *base["categories"], "residual"]
    out = []
    for slot in slots:
        t = slot["target"]
        for item in items:
            value = slot["scaled"][item] if slot["scaled"] else None
            out.append(_record(digest=data.sha256, locator=Locator(meter=f"{TOTAL_METER}:{item}", time_range=Interval(start=t, end=t + SLOT)),
                               nature="derived", start=t, available=t + SLOT + delay, quantity=f"production_base_{item}",
                               value=value, steps=base_steps(data, base, time_map, slot, item)))
    key = {"data": data.sha256, "base": base, "time_map": time_map, "days": days, "version": VERSION}
    return SourceBundle(bundle_id=f"b18-d05-base-{fingerprint(key)}", source_id=SOURCE_ID,
                        source_version=SOURCE_VERSION, mapping_version=VERSION, records=tuple(out), license=LICENSE,
                        note=f"生产区基础负荷：D05 形状 × {base['multiplier']}（configured）；{SOURCE_NOTE}")
