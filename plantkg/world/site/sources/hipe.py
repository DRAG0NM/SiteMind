"""HIPE 回退底座：D05 不可用时，用 B05 已映射到厂区时间的 HIPE 总表记录做生产区总量（B18）。

输入是 B05 `HipeAdapter.normalize` 输出的 SourceBundle（只读契约，不导入 plantkg.adapters）：
取 `nature=configured`、`locator.meter` 为配置表计（默认 MainTerminal）的记录，它们已按 B05 time_map
映射到 Asia/Shanghai 并乘 B05 负荷因子。HIPE 没有分项表计，分项按 site.yaml 里 hipe_fallback.category_shares
（configured，取自 D05 全年各类叶子占比）拆分，余项 = 总量 × (1 − 占比合计)。可见时间不早于 HIPE 输入记录。
"""
from __future__ import annotations

from datetime import timedelta

from plantkg.core.contracts import SourceBundle, TransformStep

from . import d05

VERSION = "b18-hipe-fallback-1"


def production_base(bundle: SourceBundle, base: dict, time_map: dict, days: int) -> list[dict]:
    cfg = base["hipe_fallback"]
    shares = cfg["category_shares"]
    if set(shares) != set(base["categories"]) or not 0 <= sum(shares.values()) <= 1:
        raise ValueError("hipe_fallback.category_shares 必须覆盖全部分项且合计在 0～1")
    m = float(cfg.get("multiplier", 1.0))
    if not m > 0:
        raise ValueError("hipe_fallback.multiplier 必须为正数")
    by_time = {r.event_time: r for r in bundle.records if r.nature == "configured" and r.locator.meter == cfg.get("meter", "MainTerminal")}
    if not by_time:
        raise ValueError("HIPE 包里没有配置表计的 configured 记录")
    out = []
    for target in d05.plant_slots(time_map, days):
        r = by_time.get(target)
        d = scaled = None
        if r is not None and r.value is not None:
            total = r.value
            d = {"net_import": None, "total": total, "basis": "hipe", "meters": {}, "leaf_sum": None, "excluded": {},
                 "categories": {c: total * s for c, s in shares.items()}, "residual": total * (1 - sum(shares.values()))}
            scaled = {"total": total * m, "residual": d["residual"] * m, **{c: v * m for c, v in d["categories"].items()}}
        out.append({"kind": "hipe", "target": target, "source": None, "handling": "b05_mapped", "record": r,
                    "decomposition": d, "scaled": scaled, "available_at": r.available_at if r else None})
    return out


def base_steps(bundle: SourceBundle, base: dict, slot: dict, item: str) -> list[TransformStep]:
    cfg = base["hipe_fallback"]
    r = slot["record"]
    select = TransformStep(op="hipe_fallback_select", version=VERSION, note="HIPE 德国工厂（B05 映射与放大），非本厂", params={
        "bundle_id": bundle.bundle_id, "source_id": bundle.source_id, "source_version": bundle.source_version,
        "meter": cfg.get("meter", "MainTerminal"), "record_content_hash": r.content_hash if r else None,
        "record_line_start": r.locator.line_start if r else None, "record_line_end": r.locator.line_end if r else None,
        "record_value_kw": r.value if r else None, "record_available_at": r.available_at.isoformat() if r else None})
    split = TransformStep(op="split_by_configured_shares", version=VERSION, params={
        "category_shares": dict(cfg["category_shares"]), "shares_nature": "configured", "item": item})
    scale = TransformStep(op="scale_by_declared_multiplier", version=VERSION, params={
        "multiplier": float(cfg.get("multiplier", 1.0)), "multiplier_nature": "configured", "item": item})
    return [select, split, scale]


def available_floor(slot: dict, delay: timedelta):
    """派生记录的可见时间不早于 HIPE 输入记录的可见时间。"""
    end = slot["target"] + d05.SLOT + delay
    return max(end, slot["available_at"]) if slot["available_at"] else end
