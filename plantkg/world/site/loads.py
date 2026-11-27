"""工厂负荷合成：生产区 D05 底座按设备分摊，D05 没有的分项按作息与人流编写（B18）。

- d05:<分项> 设备：分项（已 × 倍数、derived）按同分项内额定功率占比分到各台；超过额定功率标 suspect。
- 未计量余项挂在生产总配电柜 P-PROD 上，不分给任何设备。
- synthetic:<模型> 设备：人流、照明、暖通、特殊区域按常理编写（synthetic）；电梯负荷由人流与物料任务推出（simulated）。
- 全厂总负荷 = 各设备负荷 + 未计量余项；任一分项缺测时该时段总量为 None。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from plantkg.core.contracts import Interval, Locator, Quality, SourceBundle, SourceRecord, TransformStep

from .model import SiteModel
from .sources import d05, hipe

UTC = timezone.utc
SLOT = d05.SLOT
VERSION = "b18-site-1"
SOURCE_ID = "b18-factory-site"
UNMETERED = "P-PROD:unmetered"

# 电梯每次运行耗电（kWh）与待机功率（kW），按常理取值
PASSENGER_TRIP_KWH, PASSENGER_STANDBY_KW, PERSONS_PER_TRIP = 0.09, 1.2, 8
FREIGHT_TRIP_KWH, FREIGHT_STANDBY_KW = 0.3, 1.5


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _ramp(x: float, a: float, b: float) -> float:
    return min(1.0, max(0.0, (x - a) / (b - a)))


def admin_presence(minute: float, s: dict) -> float:
    """行政楼在楼比例（工作日）：早高峰进楼、午间部分外出就餐、晚高峰离楼、夜间值班。"""
    m0, m1 = map(_minutes, s["morning_peak"])
    l0, l1 = map(_minutes, s["lunch"])
    e0, e1 = map(_minutes, s["evening_peak"])
    night = 0.02
    arrive = _ramp(minute, m0 - 30, m1 + 15)  # 早高峰前后各有少量提前与迟到
    leave = _ramp(minute, e0, e1 + 30)
    level = night + (0.95 - night) * arrive - (0.95 - 0.12) * leave
    if l0 <= minute < l1:
        level -= 0.35 * math.sin(math.pi * (minute - l0) / (l1 - l0))
    if minute >= e1 + 30:
        level = 0.12 - 0.10 * _ramp(minute, e1 + 30, 22 * 60)
    return max(night, level)


def production_headcount(minute: float, s: dict) -> float:
    """车间在岗人数：三班连续生产，交接班半小时两班重叠。"""
    n = s["production_staff_per_shift"]
    for start in map(_minutes, s["production_shifts"]):
        if 0 <= (minute - start) % 1440 < s["shift_change_minutes"]:
            return 2 * n
    return n


@dataclass
class SiteLoads:
    site: SiteModel
    slots: list[datetime]
    series: dict[str, list[float | None]]
    natures: dict[str, str]
    models: dict[str, dict]
    base_slots: list[dict]
    data: d05.D05Data | SourceBundle  # D05 或 HIPE 回退包
    time_map: dict
    days: int
    tasks: list[dict] = field(default_factory=list)

    @property
    def base_kind(self) -> str:
        return "hipe" if isinstance(self.data, SourceBundle) else "d05"

    @property
    def base_digest(self) -> str:
        if self.base_kind == "d05":
            return self.data.sha256
        return self.data.bundle_id

    def total(self, i: int) -> float | None:
        return self.series["site_total"][i]

    def to_bundle(self, delay_seconds: float | None = None) -> SourceBundle:
        """全部分项、设备、总量与人流曲线的 B01 记录，来源身份统一为本场景（b18-factory-site）。

        底座来源（D05 或 HIPE）的身份、散列、表计和转换参数写在 transform_steps 里。
        delay_seconds 默认取 production_base.delay_seconds；派生记录的可见时间不早于所依赖的输入。
        """
        base = self.site.config["production_base"]
        delay_seconds = float(base.get("delay_seconds", 0)) if delay_seconds is None else float(delay_seconds)
        delay = timedelta(seconds=delay_seconds)
        tm = self.time_map
        out = []
        over = [self._over_rated(i) for i in range(len(self.slots))]
        for key, values in self.series.items():
            nature = self.natures[key]
            unit = "count" if key.startswith(("occupancy", "trips", "tasks")) else "kW"
            for i, t in enumerate(self.slots):
                slot = self.base_slots[i]
                steps = list(self._steps(key, slot, base, tm))
                value = values[i]
                flag = "missing" if value is None else ("suspect" if key in over[i] else "ok")
                if nature != "derived":
                    content_hash = "sha256:" + self.site.config_sha256
                elif slot["kind"] == "hipe" and slot["record"] is not None:
                    content_hash = slot["record"].content_hash
                else:
                    content_hash = "sha256:" + (self.data.sha256 if self.base_kind == "d05" else self.site.config_sha256)
                out.append(SourceRecord(
                    source_id=SOURCE_ID, source_version=VERSION, content_hash=content_hash, locator=Locator(meter=key, time_range=Interval(start=t, end=t + SLOT)),
                    nature=nature, event_time=t, interval=Interval(start=t, end=t + SLOT),
                    available_at=hipe.available_floor(slot, delay) if slot["kind"] == "hipe" else t + SLOT + delay,
                    source_tz=self.site.plant_tz, source_unit=unit, quantity=self._quantity(key), unit=unit,
                    value=value, transform_steps=tuple(steps),
                    quality=Quality(flag=flag, note="超过额定功率" if flag == "suspect" else None)))
        key = {"site": self.site.config_sha256, "base": self.base_kind, "data": self.base_digest,
               "time_map": tm, "days": self.days, "delay_seconds": delay_seconds, "version": VERSION}
        origin = d05.SOURCE_NOTE if self.base_kind == "d05" else f"HIPE 回退（B05 包 {self.data.bundle_id}，德国工厂，非本厂）"
        return SourceBundle(bundle_id=f"b18-site-{d05.fingerprint(key)}", source_id=SOURCE_ID, source_version=VERSION,
                            mapping_version=VERSION, records=tuple(out),
                            license=d05.LICENSE if self.base_kind == "d05" else self.data.license,
                            note=f"工厂装配负荷：生产区底座来自 {origin}，× 倍数后 derived；其余分项 synthetic/simulated")

    def _quantity(self, key: str) -> str:
        if key.startswith("occupancy"):
            return "occupancy"
        if key.startswith(("trips", "tasks")):
            return key.split(":")[0]
        return "active_power"

    def _over_rated(self, i: int) -> set[str]:
        return {e.code for e in self.site.equipment
                if e.code in self.series and self.series[e.code][i] is not None and self.series[e.code][i] > e.rated_kw + 1e-9}

    def _steps(self, key, slot, base, tm):
        model = self.models[key]
        if model["kind"] == "d05":
            if slot["kind"] == "hipe":
                yield from hipe.base_steps(self.data, base, slot, model["category"])
            else:
                yield from d05.base_steps(self.data, base, tm, slot, model["category"])
            if model.get("share") is not None:
                yield TransformStep(op="allocate_by_rated_power", version=VERSION, params={
                    "category": model["category"], "share": model["share"], "rated_kw": model["rated_kw"],
                    "category_rated_kw": model["category_rated_kw"]})
        else:
            yield TransformStep(op=model["kind"], version=VERSION, params={k: v for k, v in model.items() if k != "kind"},
                                note="按常理编写，非实测" if self.natures[key] == "synthetic" else "由人流/物料任务模型推出，非实测")


def synthesize(site: SiteModel, data: d05.D05Data | SourceBundle, days: int, plant_start: str | None = None) -> SiteLoads:
    """data 为 D05 数据时按 D05 底座；为 B05 HIPE SourceBundle 时走回退底座（hipe_fallback）。"""
    tm = dict(site.config["time_map"])
    if plant_start:
        shift = datetime.fromisoformat(plant_start) - datetime.fromisoformat(tm["plant_start"])
        tm["plant_start"] = plant_start
        tm["source_start"] = (datetime.fromisoformat(tm["source_start"]) + shift).date().isoformat()
    base = site.config["production_base"]
    s = site.config["schedule"]
    tz = ZoneInfo(site.plant_tz)
    if isinstance(data, SourceBundle):
        slots = hipe.production_base(data, base, tm, days)
    else:
        slots = d05.production_base(data, base, tm, days)
    times = [x["target"] for x in slots]
    n = len(times)
    series: dict[str, list] = {}
    natures: dict[str, str] = {}
    models: dict[str, dict] = {}

    def put(key, values, nature, model):
        series[key], natures[key], models[key] = values, nature, model

    # 生产区：D05 分项按额定功率分摊到设备，余项挂 P-PROD
    for cat in base["categories"]:
        members = [e for e in site.equipment if e.load == f"d05:{cat}"]
        rated = sum(e.rated_kw for e in members)
        for e in members:
            share = e.rated_kw / rated
            put(e.code, [x["scaled"][cat] * share if x["scaled"] else None for x in slots], "derived",
                {"kind": "d05", "category": cat, "share": share, "rated_kw": e.rated_kw, "category_rated_kw": rated})
    put(UNMETERED, [x["scaled"]["residual"] if x["scaled"] else None for x in slots], "derived",
        {"kind": "d05", "category": "residual", "share": None})

    # 作息与人流
    local = [t.astimezone(tz) for t in times]
    minute = [lt.hour * 60 + lt.minute + 7.5 for lt in local]  # 取时段中点
    weekday = [lt.weekday() < 5 for lt in local]
    presence = [admin_presence(m, s) * (1 if wd else s["weekend_admin_ratio"]) for m, wd in zip(minute, weekday)]
    admin_people = [s["admin_staff"] * p for p in presence]
    put("occupancy:admin", admin_people, "synthetic", {"kind": "people_flow_schedule", **{k: s[k] for k in
        ("admin_staff", "weekend_admin_ratio", "morning_peak", "lunch", "evening_peak")}})
    put("occupancy:production", [float(production_headcount(m, s)) for m in minute], "synthetic",
        {"kind": "shift_schedule", "production_shifts": s["production_shifts"],
         "production_staff_per_shift": s["production_staff_per_shift"], "shift_change_minutes": s["shift_change_minutes"]})

    # 行政楼人梯：进出楼与午间外出的人次 → 运行次数（往返计两次）→ 功率
    lunch_moves = [s["admin_staff"] * 0.35 * 2 * (1 if wd else 0) / (( _minutes(s["lunch"][1]) - _minutes(s["lunch"][0])) / 15)
                   if _minutes(s["lunch"][0]) <= m < _minutes(s["lunch"][1]) else 0.0 for m, wd in zip(minute, weekday)]
    moves = [abs(admin_people[i] - admin_people[i - 1]) if i else 0.0 for i in range(n)]
    trips = [2 * (mv + lm) * 0.85 / PERSONS_PER_TRIP + 1 for mv, lm in zip(moves, lunch_moves)]  # 85% 乘梯，另有零星运行
    put("trips:passenger_elevator", trips, "simulated", {"kind": "elevator_trips_from_people_flow",
        "persons_per_trip": PERSONS_PER_TRIP, "elevator_share": 0.85})
    lifts = [e for e in site.equipment if e.load == "synthetic:passenger_elevator"]
    for e in lifts:
        put(e.code, [min(e.rated_kw, PASSENGER_STANDBY_KW + tr / len(lifts) * PASSENGER_TRIP_KWH / 0.25) for tr in trips],
            "simulated", {"kind": "elevator_power_from_trips", "trip_kwh": PASSENGER_TRIP_KWH,
                          "standby_kw": PASSENGER_STANDBY_KW, "rated_kw": e.rated_kw})

    # 生产节拍与物料搬运：任务数随生产线负荷率变化，四台货梯轮流承担
    line_rated = sum(e.rated_kw for e in site.equipment if e.load == "d05:process")
    load_rate = [min(1.0, x["scaled"]["process"] / line_rated) if x["scaled"] else 0.0 for x in slots]
    per_slot = [s["material_trips_per_hour"] / 4 * (0.3 + 0.7 * r) for r in load_rate]
    put("tasks:material_handling", per_slot, "simulated", {"kind": "material_tasks_from_process_load",
        "material_trips_per_hour": s["material_trips_per_hour"], "line_rated_kw": line_rated})
    freight = [e for e in site.equipment if e.load == "synthetic:freight_elevator"]
    for e in freight:
        put(e.code, [min(e.rated_kw, FREIGHT_STANDBY_KW + k / len(freight) * FREIGHT_TRIP_KWH / 0.25) for k in per_slot],
            "simulated", {"kind": "elevator_power_from_tasks", "trip_kwh": FREIGHT_TRIP_KWH,
                          "standby_kw": FREIGHT_STANDBY_KW, "rated_kw": e.rated_kw})
    tasks = _material_tasks(times, per_slot, [e.code for e in freight])

    # 照明、暖通与特殊区域
    c0, c1 = _minutes(s["office_cooling_start"]), _minutes(s["office_cooling_end"])
    outdoor = [0.5 + 0.5 * math.cos(2 * math.pi * (m - 14 * 60) / 1440) for m in minute]  # 14:00 最热，0～1
    for e in site.equipment:
        rule = e.load.removeprefix("synthetic:")
        if not e.load.startswith("synthetic:") or e.code in series:
            continue
        if rule == "production_lighting":
            vals = [e.rated_kw * (0.7 if 360 <= m < 1080 else 1.0) for m in minute]
            model = {"day_ratio": 0.7, "night_ratio": 1.0, "day": ["06:00", "18:00"]}
        elif rule == "office_lighting":
            vals = [e.rated_kw * (0.08 + 0.87 * p) for p in presence]
            model = {"base_ratio": 0.08, "presence_ratio": 0.87}
        elif rule == "office_hvac":
            vals = [e.rated_kw * ((0.3 + 0.35 * p + 0.3 * o) if wd and c0 <= m < c1 else 0.04)
                    for p, o, m, wd in zip(presence, outdoor, minute, weekday)]
            model = {"cooling_hours": [s["office_cooling_start"], s["office_cooling_end"]], "setpoint": "24～26 ℃",
                     "formula": "额定 ×（0.3 + 0.35 × 在楼比例 + 0.3 × 室外热度），非供冷时段 0.04"}
        elif rule == "cleanroom_hvac":
            vals = [e.rated_kw * (0.72 + 0.08 * o) for o in outdoor]
            model = {"formula": "额定 ×（0.72 + 0.08 × 室外热度）", "constraint": "温湿度硬要求，削峰时不能动"}
        elif rule == "hazmat_ventilation":
            vals = [e.rated_kw * 0.6] * n
            model = {"ratio": 0.6, "constraint": "连续排风，安全标记"}
        elif rule == "datacenter_it":
            vals = [e.rated_kw * (0.68 + 0.07 * p) for p in presence]
            model = {"formula": "额定 ×（0.68 + 0.07 × 在楼比例）", "constraint": "不能断电"}
        elif rule == "datacenter_cooling":
            vals = [e.rated_kw * (0.45 + 0.2 * o) for o in outdoor]
            model = {"formula": "额定 ×（0.45 + 0.2 × 室外热度）", "constraint": "不能断电"}
        else:
            raise ValueError(f"{e.code} 的负荷模型 {e.load} 未实现")
        put(e.code, vals, "synthetic", {"kind": f"profile_{rule}", "rated_kw": e.rated_kw, **model})

    # 汇总：设备 + 余项 → 各变压器、各区域、全厂
    load_keys = [e.code for e in site.equipment if e.load != "none"] + [UNMETERED]
    owner = {e.code: e for e in site.equipment}

    def total_of(keys):
        return [None if any(series[k][i] is None for k in keys) else sum(series[k][i] for k in keys) for i in range(n)]

    for zone in site.zones:
        keys = [k for k in load_keys if (owner[k].zone if k in owner else "production") == zone]
        if keys:
            put(f"zone_total:{zone}", total_of(keys), "derived" if zone == "production" else "synthetic",
                {"kind": "sum", "items": keys})
    for tx in (nid for nid, node in site.nodes.items() if node.type == "transformer"):
        keys = [k for k in load_keys if site.transformer_of(k if k in owner else "P-PROD").id == tx]
        put(f"transformer_load:{tx}", total_of(keys), "derived", {"kind": "sum", "items": keys,
            "limit_kw": site.transformer_limit_kw(tx)})
    put("site_total", total_of(load_keys), "derived", {"kind": "sum", "items": load_keys})
    return SiteLoads(site=site, slots=times, series=series, natures=natures, models=models, base_slots=slots,
                     data=data, time_map=tm, days=days, tasks=tasks)


def _material_tasks(times, per_slot, elevators):
    """确定性生成物料搬运任务（synthetic）：每时段按期望数取整、累计余数，轮流派给货梯。"""
    tasks, carry, k = [], 0.0, 0
    for t, expected in zip(times, per_slot):
        carry += expected
        count, carry = int(carry), carry - int(carry)
        for j in range(count):
            tasks.append({"task_id": f"MT-{t:%Y%m%d%H%M}-{j + 1:02d}", "release_at": (t + SLOT * j / max(count, 1)).isoformat(),
                          "elevator": elevators[k % len(elevators)], "origin": "原料库", "destination": f"LINE-{k % 3 + 1}",
                          "priority": "urgent" if k % 7 == 0 else "normal", "nature": "synthetic"})
            k += 1
    return tasks
