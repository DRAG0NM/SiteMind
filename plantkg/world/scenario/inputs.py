"""ScenarioInputProvider（B17）：按分钟给出基础负荷、光伏、电价、室外温度与人流。

默认实现 `SiteInputProvider` 读 B18 工厂装配（15 分钟负荷与人流，D05 底座 × 倍数 + 作息合成）、
PVGIS 小时出力与气温、江苏分时电价（B12）。到 1 分钟：负荷、人流、电价按所在时段保持取值；
光伏与气温按小时中点线性插值。插值后的值标 derived 并写 transform_steps，原记录不改（`minute_records`）。
演示用的完整实现由 B19 提供，替换本类即可。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

import yaml

from plantkg.core.contracts import DataSourceRef, Interval, Locator, SourceRecord, TransformStep
from plantkg.world.sim.base import MINUTE, SLOT, MinuteInputs
from plantkg.world.site.loads import UNMETERED, SiteLoads, synthesize
from plantkg.world.site.model import SiteModel
from plantkg.world.site.sources import d05

from .script import Scenario

UTC = timezone.utc
SOURCE_ID = "b17-scenario-inputs"
VERSION = "b17-inputs-1"
_HOLD = TransformStep(op="hold_15min_to_1min", params={"from_minutes": 15, "to_minutes": 1}, version=VERSION,
                      note="1 分钟取所在 15 分钟时段的值")
_INTERP = TransformStep(op="interpolate_hourly_linear", params={"anchor": "hour_center", "to_minutes": 1}, version=VERSION,
                        note="小时值视为该小时中点，相邻中点间线性插值")


@runtime_checkable
class ScenarioInputProvider(Protocol):
    provider_id: str

    def minute(self, at: datetime) -> MinuteInputs: ...

    def provenance(self) -> dict[str, dict]: ...

    def data_sources(self) -> tuple[DataSourceRef, ...]: ...


class SiteInputProvider:
    """B18 装配 + PVGIS + 分时电价。剧本的 pv_scale、outdoor_offset_c 窗口在这里叠加（标 synthetic）。

    prices 由装配层注入（world 平面不直接依赖电价包）：给定 15 分钟时段列表，返回 [(元/kWh, 时段名)]。"""

    def __init__(self, scenario: Scenario, prices: Callable[[list[Interval]], list[tuple[float, str]]],
                 site: SiteModel | None = None, loads: SiteLoads | None = None, price_note: dict | None = None) -> None:
        self.scenario = scenario
        self.site = site or SiteModel.load(scenario.resolve(scenario.sources.site))
        self.loads = loads or synthesize(self.site, d05.load_d05(scenario.resolve(scenario.sources.d05)), scenario.days,
                                         plant_start=scenario.date)
        if self.loads.slots[0] != scenario.start:
            raise ValueError(f"B18 负荷起点 {self.loads.slots[0]} 与剧本起点 {scenario.start} 不一致")
        self.slot_index = {t: i for i, t in enumerate(self.loads.slots)}
        self.load_keys = [e.code for e in self.site.equipment if e.load != "none"] + [UNMETERED]
        pv = yaml.safe_load(scenario.resolve(scenario.sources.pv).read_text(encoding="utf-8"))["pv"]
        self.kwp = float(pv["kwp"])
        raw = json.loads(scenario.resolve(scenario.sources.pvgis).read_text(encoding="utf-8"))
        self.pvgis_sha = raw["source_sha256"]
        rows = raw["rows"]
        self.pv_days = [[r["P"] for r in rows[i * 24:(i + 1) * 24]] for i in range(len(rows) // 24)]
        self.t2m_days = [[r["T2m"] for r in rows[i * 24:(i + 1) * 24]] for i in range(len(rows) // 24)]
        priced = prices([Interval(start=t, end=t + SLOT) for t in self.loads.slots])
        self.prices = [p for p, _ in priced]
        self.price_periods = [name for _, name in priced]
        self.price_note = price_note or {}
        self.pv_windows = [(scenario.instant(w.start), scenario.instant(w.end), w.factor) for w in scenario.params.pv_scale]
        self.heat_windows = [(scenario.instant(w.start), scenario.instant(w.end), w.delta_c) for w in scenario.params.outdoor_offset_c]
        self.provider_id = f"{SOURCE_ID}:{scenario.scenario_id}:{self.loads.to_bundle().bundle_id}"

    # ---- 分钟输入
    def minute(self, at: datetime) -> MinuteInputs:
        slot = at - timedelta(minutes=at.minute % 15, seconds=at.second, microseconds=at.microsecond)
        i = self.slot_index[slot]
        series = self.loads.series
        equipment = {k: float(series[k][i] or 0.0) for k in self.load_keys}
        pv = self.pv_kw(at)
        for a, b, f in self.pv_windows:
            if a <= at < b and f is not None:
                pv *= f
        outdoor = self.outdoor_c(at)
        for a, b, d in self.heat_windows:
            if a <= at < b and d is not None:
                outdoor += d
        occupancy = {"occupancy:admin": float(series["occupancy:admin"][i]), "occupancy:production": float(series["occupancy:production"][i]),
                     "trips:passenger_elevator": float(series["trips:passenger_elevator"][i]) / 15,
                     "tasks:material_handling": float(series["tasks:material_handling"][i]) / 15}
        return MinuteInputs(at=at, equipment_kw=equipment, pv_kw=pv, price_cny_per_kwh=self.prices[i], outdoor_c=outdoor, occupancy=occupancy)

    def price_period(self, at: datetime) -> str:
        slot = at - timedelta(minutes=at.minute % 15)
        return self.price_periods[self.slot_index[slot]]

    def _hourly(self, days: list[list[float]], at: datetime) -> float:
        """连续 UTC 时间索引：剧本首日的 UTC 0 点对应源第 0 行，PVGIS 时刻为 UTC 小时中点（HH:30），逐日循环。"""
        anchor = datetime.fromisoformat(str(self.scenario.date)).replace(tzinfo=UTC)
        h = (at - anchor) / timedelta(hours=1) - 0.5
        k = int(h // 1)
        frac = h - k
        flat = [v for d in days for v in d]
        a, b = flat[k % len(flat)], flat[(k + 1) % len(flat)]
        return a + (b - a) * frac

    def pv_kw(self, at: datetime) -> float:
        return max(0.0, self._hourly(self.pv_days, at) / 1000 * self.kwp)  # PVGIS P 为 1 kWp 的 W

    def outdoor_c(self, at: datetime) -> float:
        return self._hourly(self.t2m_days, at)

    # ---- 性质与来源
    def data_source_refs(self) -> tuple[DataSourceRef, ...]:
        """Record the actual load base and the transformation/input versions."""
        bundle = self.loads.to_bundle()
        refs = (DataSourceRef(source=self.loads.base_kind, dataset_version=self.loads.base_digest),
                DataSourceRef(source="b18-factory-site", dataset_version=bundle.bundle_id),
                DataSourceRef(source=SOURCE_ID, dataset_version=VERSION),
                DataSourceRef(source="pvgis", dataset_version=self.pvgis_sha))
        return refs + tuple(DataSourceRef(source=f"scenario-{name}", dataset_version=hashlib.sha256(
            self.scenario.resolve(getattr(self.scenario.sources, name)).read_bytes()).hexdigest())
            for name in ("tariff_rules", "tariff_config", "site", "storage", "pv", "pvgis"))

    def data_sources(self) -> tuple[DataSourceRef, ...]:
        """本次运行用到的数据来源与版本（D52④）：负荷底座区分 D05/HIPE 回退，其余为输入装配、光伏与电价。"""
        return (DataSourceRef(source=self.loads.base_kind, dataset_version=self.loads.base_digest),
                DataSourceRef(source="b17-inputs", dataset_version=self.provider_id),
                DataSourceRef(source="pvgis", dataset_version=self.pvgis_sha),
                DataSourceRef(source="tariff", dataset_version=str(self.price_note.get("source", "tariff-scenario"))))

    def provenance(self) -> dict[str, dict]:
        # 保持取值不改性质；只有插值得到的值标 derived
        out = {k: {"nature": self.loads.natures[k], "transform_steps": [_HOLD.model_dump()], "source": "b18-factory-site"}
               for k in self.load_keys}
        pv_steps = [TransformStep(op="unit_w_per_kwp_to_kw", params={"source_peak_kwp": 1, "target_peak_kwp": self.kwp}, version=VERSION),
                    TransformStep(op="map_day", params={"source": "PVGIS 2017-06-30～07-02 逐日循环", "sha256": self.pvgis_sha}, version=VERSION),
                    _INTERP]
        out["pv"] = {"nature": "derived", "source": "pvgis", "transform_steps": [s.model_dump() for s in pv_steps],
                     "windows": [w.model_dump() for w in self.scenario.params.pv_scale]}
        out["outdoor_c"] = {"nature": "derived", "source": "pvgis:T2m", "transform_steps": [_INTERP.model_dump()],
                            "windows": [w.model_dump() for w in self.scenario.params.outdoor_offset_c]}
        out["price"] = {"nature": "configured", **self.price_note, "transform_steps": [_HOLD.model_dump()]}
        return out

    def minute_records(self, key: str, start: datetime, minutes: int) -> Sequence[SourceRecord]:
        """把一条 15 分钟原记录展开成 1 分钟记录（不改原记录）：保持取值，性质沿用原记录，transform_steps = 原步骤 + 保持取值。"""
        bundle = self.loads.to_bundle()
        originals = {(r.locator.meter, r.interval.start): r for r in bundle.records}
        out = []
        for m in range(minutes):
            t = start + m * MINUTE
            slot = t - timedelta(minutes=t.minute % 15)
            src = originals[(key, slot)]
            out.append(src.model_copy(update={
                "source_id": SOURCE_ID, "source_version": VERSION, "event_time": t,
                "interval": Interval(start=t, end=t + MINUTE), "locator": Locator(meter=key, time_range=Interval(start=t, end=t + MINUTE)),
                "available_at": max(src.available_at, t + MINUTE),
                "transform_steps": (*src.transform_steps, _HOLD)}))
        return tuple(out)
