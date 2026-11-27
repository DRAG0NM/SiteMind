"""Catalog-wide asset-version snapshot for day exports."""
from __future__ import annotations

from collections import Counter
import sqlite3

from plantkg.core.contracts import AssetStateCounts, Context, DayMetric
from plantkg.knowledge.assets.catalog import STAGES


def asset_metrics_provider(catalog):
    """Adapt the catalog's public read API to the world export injection point."""
    def snapshot(as_of, complete):
        ctx = Context(plant_id=catalog.plant_id, industry_pack="-", actor=None,
                      channel="offline", trace_id="asset-day-summary", as_of=as_of)
        note = ("scope=plant_catalog；同厂区可见资产版本（含历史版本），非运行专属；"
                + ("完整日" if complete else "非完整日") + "；引用不等于业务收益")
        try:
            summary = catalog.activation_summary(ctx)
            natures = Counter()
            cursor = None
            while True:
                items, cursor = catalog.list(ctx, cursor)
                natures.update(item.nature for item in items)
                if cursor is None:
                    break
        except (sqlite3.Error, OSError) as exc:
            message = str(exc).lower()
            if isinstance(exc, sqlite3.Error) and not any(
                reason in message for reason in ("no such table: asset_", "unable to open database",
                                                 "database is locked", "database is malformed",
                                                 "database disk image is malformed")
            ):
                raise
            summary = None
            note += f"；目录不可读：{type(exc).__name__}: {exc}"
        if summary is not None:
            counts = AssetStateCounts(**summary["by_stage"])
            if sum(getattr(counts, stage) for stage in STAGES) != summary["total"]:
                raise ValueError("asset catalog counts do not sum to total")
            note += "；原资料 nature=" + (", ".join(f"{k}:{v}" for k, v in sorted(natures.items())) or "无")
            if not summary["total"]:
                note += "；分母为 0，激活率不可计算"
            values = (summary["total"], counts.referenced, None, summary["referenced_rate"])
        else:
            counts = None
            values = (None, None, None, None)
        names = ("asset_total", "asset_activated", "asset_state_counts", "asset_activation_rate")
        return tuple(DayMetric(metric=name, group="knowledge", value=value,
                               unit="ratio" if name == "asset_activation_rate" else "asset_version",
                               counts=counts if name == "asset_state_counts" else None,
                               as_of=as_of, nature="derived",
                               note=note + ("；value=null 表示结构化统计" if name == "asset_state_counts" and counts else ""))
                     for name, value in zip(names, values))
    return snapshot
