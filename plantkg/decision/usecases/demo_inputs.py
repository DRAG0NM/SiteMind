"""B19 演示输入适配：B17 `ScenarioInputProvider` 的演示实现（`plantkg/decision/usecases/`）。

在 `SiteInputProvider`（B18 装配 + PVGIS + 分时电价）之上增加两件事：

1. **接收分项记录并结算**：B22 的空压泄漏偏离等专业分支还没有产出时，演示与测试可以按通用
   `SourceRecord` 注入记录；记录原有的 nature 原样保留（可 derived / simulated / synthetic /
   configured），minute() 把它们叠加进对应表计的分钟负荷，minute_records() 把它们展开成 1 分钟记录。
2. **性质闸门**：场景输入里不出现 `measured`，避免被回放路径的“在线只收 measured”当成本厂实测；
   `production` profile 只收 measured，其余性质一律拒绝。两个方向由同一张表决定。

负荷与光伏的 15 分钟汇总值按槽缓存：日前计划与每 15 分钟的重解共用同一份 B18 装配，不重复插值。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, SourceRecord

# 场景输入是 1 分钟步长、15 分钟整格（与 B17 仿真时钟一致）；本文件属 decision 平面，不导入 world。
MINUTE = timedelta(minutes=1)
SLOT = timedelta(minutes=15)

SCENARIO_NATURES = frozenset({"derived", "simulated", "synthetic", "configured"})
PRODUCTION_NATURES = frozenset({"measured"})
PROFILES = frozenset({"mock", "dev", "demo", "evaluation"})


def accepted_natures(profile: str) -> frozenset[str]:
    """场景运行只收非实测性质；production 只收实测。未知 profile 按 production 处理，失败关闭。"""
    return SCENARIO_NATURES if profile in PROFILES else PRODUCTION_NATURES


def accept_records(records: Iterable[SourceRecord], profile: str) -> tuple[SourceRecord, ...]:
    """校验记录性质，返回原记录（不改 nature、不改来源）。被拒绝时一次列出全部问题。"""
    allowed = accepted_natures(profile)
    records = tuple(records)
    bad = [r for r in records if r.nature not in allowed]
    if bad:
        detail = "、".join(f"{r.source_id}@{r.locator.meter or '?'}:{r.nature}" for r in bad[:5])
        raise PlantkgError(ErrorCode.PRECONDITION,
                           f"{profile} profile 只接收 {sorted(allowed)} 性质的输入，拒绝 {detail}"
                           f"（共 {len(bad)} 条）")
    return records


class DemoScenarioInputProvider:
    """B17 场景输入 + B22 偏离记录接收。属性与 `SiteInputProvider` 一致，`ScenarioWorld.build` 直接可用。"""

    def __init__(self, base, *, profile: str = "dev", records: Sequence[SourceRecord] = ()) -> None:
        """base：B17 的 `SiteInputProvider`（鸭子类型：minute / provenance / minute_records / slot_index / loads / site）。"""
        self.base = base
        self.profile = profile
        self.provider_id = f"{base.provider_id}:demo-inputs"
        self._extra: dict[str, list[SourceRecord]] = {}
        self._slot_cache: dict[datetime, tuple[float, float]] = {}
        self.accept(records)

    # ---- 接收
    def accept(self, records: Iterable[SourceRecord]) -> tuple[SourceRecord, ...]:
        checked = accept_records(records, self.profile)
        for r in checked:
            meter = r.locator.meter
            if not meter or r.interval is None or r.value is None:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "接收的分项记录必须有表计、时段与数值")
            self._extra.setdefault(meter, []).append(r)
        return checked

    @property
    def extra_records(self) -> tuple[SourceRecord, ...]:
        return tuple(r for rows in self._extra.values() for r in rows)

    def extra_load_kw(self, at: datetime) -> float:
        """该分钟仍在生效的额外负荷（B22 偏离记录）；用于预测与账本口径说明。"""
        return sum(float(r.value) for r in self.extra_records if r.interval.start <= at < r.interval.end)

    # ---- B17 接口
    def data_sources(self):
        """数据来源沿用底层 B17 provider（#182）。"""
        return self.base.data_sources()

    def minute(self, at: datetime):
        minutes = self.base.minute(at)
        extra = self.extra_load_kw(at)
        if not extra:
            return minutes
        equipment = dict(minutes.equipment_kw)
        for r in self.extra_records:
            if r.interval.start <= at < r.interval.end:
                equipment[r.locator.meter] = equipment.get(r.locator.meter, 0.0) + float(r.value)
        return minutes.__class__(at=minutes.at, equipment_kw=equipment, pv_kw=minutes.pv_kw,
                                 price_cny_per_kwh=minutes.price_cny_per_kwh, outdoor_c=minutes.outdoor_c,
                                 occupancy=minutes.occupancy)

    def provenance(self) -> dict[str, dict]:
        out = dict(self.base.provenance())
        extras: dict[str, list[dict]] = {}
        for r in self.extra_records:
            extras.setdefault(r.locator.meter, []).append(
                {"source_id": r.source_id, "source_version": r.source_version, "nature": r.nature, "note": r.quantity})
        if extras:
            out["extra_records"] = {"nature": "derived", "by_meter": extras,
                                    "note": "B22 等专业分支按通用记录接收，nature 原样保留"}
        return out

    def minute_records(self, key: str, start: datetime, minutes: int) -> Sequence[SourceRecord]:
        out = list(self.base.minute_records(key, start, minutes))
        for r in self._extra.get(key, ()):
            for m in range(minutes):
                t = start + m * MINUTE
                if r.interval.start <= t < r.interval.end:
                    out.append(r.model_copy(update={
                        "event_time": t, "interval": Interval(start=t, end=t + MINUTE),
                        "locator": r.locator.model_copy(update={"time_range": Interval(start=t, end=t + MINUTE)})}))
        return tuple(out)

    # ---- 15 分钟汇总（日前计划与滚动重解共用）
    def slot_profile(self, start: datetime) -> "_SlotProfile":
        cached = self._slot_cache.get(start)
        if cached is None:
            i = self.base.slot_index[start]
            load = float(self.base.loads.series["site_total"][i])
            pv = sum(self.base.pv_kw(start + m * MINUTE) for m in range(15)) / 15
            cached = (load, pv)
            self._slot_cache[start] = cached
        return _SlotProfile(start=start, end=start + SLOT, load_kw=cached[0], pv_kw=cached[1])

    def source_table(self) -> list[dict]:
        """账本来源表：每项的性质、来源与转换步骤，写进场景执行记录的 sources。"""
        rows: dict[str, dict] = {}
        for key, entry in self.base.provenance().items():
            if key == "extra_records":
                continue
            rows[key] = {"meter": key, "nature": entry.get("nature"), "source": entry.get("source"),
                         "transform_steps": list(entry.get("transform_steps", ()))}
        for meter, records in self._extra.items():
            row = rows.setdefault(meter, {"meter": meter, "nature": None, "source": "b19-demo-inputs"})
            row.setdefault("received", []).extend(
                {"nature": r.nature, "source_id": r.source_id, "value": r.value,
                 "interval": r.interval.model_dump(mode="json")} for r in records)
            row["note"] = "接收的专业分支记录（nature 原样保留）"
        return list(rows.values())

    def __getattr__(self, name):
        return getattr(self.base, name)


class _SlotProfile:
    __slots__ = ("start", "end", "load_kw", "pv_kw")

    def __init__(self, start: datetime, end: datetime, load_kw: float, pv_kw: float) -> None:
        self.start, self.end, self.load_kw, self.pv_kw = start, end, load_kw, pv_kw

    def __repr__(self) -> str:
        return f"SlotProfile({self.start.isoformat()}, load={self.load_kw:.1f}kW, pv={self.pv_kw:.1f}kW)"
