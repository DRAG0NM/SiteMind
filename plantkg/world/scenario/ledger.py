"""场景账本（B17）：逐分钟用 B08 compute_ledger 结算，槽末把 15 条分钟记录求和成 ScenarioSlotSummary。

- 分钟账本：每个 1 分钟子步一条 1 分钟 SlotInput，充放互斥、SOC 上下限、弃光与购电互斥都在这里校验。
- 槽汇总：电量、费用按分钟相加（不先平均功率再结算），允许槽内先充后放，不走 Ledger.recheck；
  minutes_balanced = 槽内每分钟都平衡。分钟峰值取分钟购电功率最大值，十五分钟需量取槽内购电电量 × 4。
- 容量：B08 账本不含厂区容量，另按 B18 配电拓扑逐分钟算各节点潮流，超限记入 capacity_violations。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from plantkg.core.contracts import LEDGER_TOLERANCE, Interval, LedgerEntry, LoadCategory, ScenarioSlotSummary, StorageParams
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger
from plantkg.world.sim.base import MINUTE
from plantkg.world.site.loads import UNMETERED

from .sims import SiteIndex

CATEGORY_BY_TYPE = {
    "passenger_elevator": LoadCategory.elevator, "freight_elevator": LoadCategory.elevator,
    "ahu": LoadCategory.hvac, "chiller": LoadCategory.hvac, "crac": LoadCategory.hvac, "exhaust_fan": LoadCategory.hvac,
    "air_compressor": LoadCategory.air_compressor, "lighting": LoadCategory.lighting,
    "production_line": LoadCategory.process, "pump_station": LoadCategory.process,
}


@dataclass(frozen=True)
class MinuteRecord:
    entry: LedgerEntry
    balanced: bool
    violations: tuple[str, ...]
    capacity_violations: tuple[str, ...]
    flows_kw: Mapping[str, float] = field(default_factory=dict)

    @property
    def import_kw(self) -> float:
        return self.entry.grid_import_kwh * 60

    def to_json(self) -> dict:
        return {"entry": self.entry.model_dump(mode="json"), "balanced": self.balanced, "violations": list(self.violations),
                "capacity_violations": list(self.capacity_violations), "flows_kw": dict(self.flows_kw)}

    @classmethod
    def from_json(cls, data: dict) -> MinuteRecord:
        return cls(entry=LedgerEntry.model_validate(data["entry"]), balanced=data["balanced"], violations=tuple(data["violations"]),
                   capacity_violations=tuple(data["capacity_violations"]), flows_kw=data.get("flows_kw", {}))


class Grid:
    """配电拓扑上的逐分钟潮流与容量上限。光伏与储能接在 P-PARK（LV1 → TX1）下。"""

    def __init__(self, index: SiteIndex, capacity_kw: Mapping[str, float]) -> None:
        site = index.site
        self.index = index
        # 所有声明了 capacity_kva 的配电节点（变压器与配电柜）都按功率因数折 kW 进上限；剧本 capacity_kw 再覆盖
        self.limits = {n: node.capacity_kva * site.power_factor for n, node in site.nodes.items() if (node.capacity_kva or 0) > 0}
        self.limits.update(capacity_kw)
        self.paths = {code: site.path(code if code != UNMETERED else "P-PROD")[1 if code != UNMETERED else 0:]
                      for code in [e.code for e in site.equipment if e.load != "none"] + [UNMETERED]}
        self.source_path = site.path("P-PARK")

    def flows(self, power_kw: Mapping[str, float], supply_kw: float, import_kw: float) -> dict[str, float]:
        """supply_kw = 光伏使用 + 放电 − 充电（在 P-PARK 注入）；GRID 取购电。"""
        out: dict[str, float] = {}
        for code, kw in power_kw.items():
            for node in self.paths.get(code, ()):
                out[node] = out.get(node, 0.0) + kw
        for node in self.source_path:
            out[node] = out.get(node, 0.0) - supply_kw
        out["P-PARK"] = supply_kw
        out["GRID"] = import_kw
        return {k: abs(v) for k, v in out.items()}

    def violations(self, at: datetime, flows: Mapping[str, float]) -> tuple[str, ...]:
        return tuple(f"{at.isoformat()} {n} {flows.get(n, 0.0):.1f} kW > 容量 {limit:.1f} kW"
                     for n, limit in sorted(self.limits.items()) if flows.get(n, 0.0) > limit + LEDGER_TOLERANCE["power_zero_kw"])


def breakdown(index: SiteIndex, power_kw: Mapping[str, float]) -> dict[str, float]:
    out = {c.value: 0.0 for c in LoadCategory}
    for code, kw in power_kw.items():
        e = index.by_code.get(code)
        cat = CATEGORY_BY_TYPE.get(e.type, LoadCategory.base) if e else LoadCategory.base
        out[cat.value if isinstance(cat, LoadCategory) else cat] += kw
    return out


def minute_ledger(at: datetime, *, pv_kw: float, power_kw: Mapping[str, float], charge_kw: float, discharge_kw: float,
                  price: float | None, storage: StorageParams | None, energy_start_kwh: float, meta: LedgerMeta,
                  index: SiteIndex, grid: Grid) -> MinuteRecord:
    parts = breakdown(index, power_kw)
    load_kw = sum(parts.values())
    slot = SlotInput(interval=Interval(start=at, end=at + MINUTE), pv_available_kw=pv_kw, load_kw=load_kw, charge_kw=charge_kw,
                     discharge_kw=discharge_kw, price_cny_per_kwh=price, load_breakdown_kw=parts)
    result = compute_ledger([slot], meta, storage, energy_start_kwh)
    entry = result.ledger.entries[0]
    import_kw = entry.grid_import_kwh * 60
    flows = grid.flows(power_kw, entry.pv_used_kwh * 60 + discharge_kw - charge_kw, import_kw)
    return MinuteRecord(entry=entry, balanced=result.ledger.balanced, violations=tuple(result.problems),
                        capacity_violations=grid.violations(at, flows), flows_kw={k: round(v, 3) for k, v in flows.items() if k in grid.limits or k == "GRID"})


def summarize_slot(start: datetime, minutes: Sequence[MinuteRecord]) -> ScenarioSlotSummary:
    """15 条分钟账本 → 槽汇总。电量、费用逐分钟相加。"""
    es = [m.entry for m in minutes]

    def total(attr: str) -> float:
        return sum(getattr(e, attr) for e in es)

    costs = [e.cost_cny for e in es]
    grid_import = total("grid_import_kwh")
    return ScenarioSlotSummary(
        interval=Interval(start=start, end=start + timedelta(minutes=15)), charge_kwh=total("charge_kwh"),
        discharge_kwh=total("discharge_kwh"), net_storage_kwh=total("charge_kwh") - total("discharge_kwh"),
        load_kwh=total("load_kwh"), pv_available_kwh=total("pv_available_kwh"), pv_used_kwh=total("pv_used_kwh"),
        grid_import_kwh=grid_import, grid_export_kwh=total("grid_export_kwh"),
        pv_curtailed_kwh=total("pv_available_kwh") - total("pv_used_kwh"), soc_end=es[-1].soc_end,
        cost_cny=None if any(c is None for c in costs) else sum(costs), peak_minute_kw=max(e.grid_import_kwh * 60 for e in es),
        demand_15m_kw=grid_import * 4, minutes_balanced=len(es) == 15 and all(m.balanced for m in minutes),
        capacity_violations=tuple(v for m in minutes for v in m.capacity_violations))


def check_slot_summary(s: ScenarioSlotSummary) -> list[str]:
    """槽汇总的复核：能量守恒、光伏守恒、净储能与需量口径。槽内先充后放合法，不查同时充放。"""
    tol = max(LEDGER_TOLERANCE["energy_abs_kwh"], LEDGER_TOLERANCE["energy_rel"] * (s.load_kwh + s.grid_import_kwh + s.pv_used_kwh))
    problems = []
    residual = s.pv_used_kwh + s.grid_import_kwh + s.discharge_kwh - s.load_kwh - s.charge_kwh - s.grid_export_kwh
    if abs(residual) > tol:
        problems.append(f"{s.interval.start.isoformat()} 槽能量残差 {residual:.6g} kWh")
    if abs(s.pv_used_kwh + s.pv_curtailed_kwh - s.pv_available_kwh) > tol:
        problems.append(f"{s.interval.start.isoformat()} 光伏使用 + 弃光 ≠ 可发")
    if abs(s.demand_15m_kw - s.grid_import_kwh * 4) > tol:
        problems.append(f"{s.interval.start.isoformat()} 需量不等于购电电量 × 4")
    if s.peak_minute_kw + tol < s.demand_15m_kw:
        problems.append(f"{s.interval.start.isoformat()} 分钟峰值低于 15 分钟平均需量")
    return problems
