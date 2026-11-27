"""资产激活：按清单里的登记事件推进阶段，并输出激活率。

阶段沿用 B06：沉睡 → 已标识 → 已入图 → 被规则或动作引用。事件是追加登记，
`referenced` 必须带引用它的规则/动作/配置 ID。激活率只统计资料被使用的程度，是辅助指标。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from plantkg.core.contracts import Context
from plantkg.knowledge.assets.catalog import ACTIVATION_NOTE, STAGES, SQLiteAssetCatalog
from plantkg.knowledge.documents.ingest import IngestedDocument
from plantkg.knowledge.documents.spec import KnowledgeLibrary, LEVEL_LABEL, STAGE_LABEL


def apply_activations(catalog: SQLiteAssetCatalog, context: Context, library: KnowledgeLibrary,
                      ingested: list[IngestedDocument]) -> list[dict[str, Any]]:
    """按清单顺序登记激活事件；同一事件重复登记幂等（B06）。"""
    by_document = {item.document.document_id: item for item in ingested}
    applied: list[dict[str, Any]] = []
    for event in library.activation_refs():
        item = by_document[event.document_id]
        catalog.record_activation(context, item.asset.asset_version_id, event.stage, event.ref, available_at=event.at)
        applied.append({"document_id": event.document_id, "stage": event.stage, "ref": event.ref, "at": event.at.isoformat()})
    return applied


def report(catalog: SQLiteAssetCatalog, context: Context, ingested: list[IngestedDocument]) -> dict[str, Any]:
    """as_of 可见资料的当前阶段与分布；激活率 = 被引用的资料数 / 可见资料总数。"""
    rows = []
    for item in ingested:
        if item.document.available_at > context.as_of:  # 还没到生效时间的资料不参与本时点的统计
            continue
        stage = catalog.activation(context, item.asset.asset_version_id)
        rows.append({"document_id": item.document.document_id, "title": item.document.title, "expert": item.document.expert,
                     "level": LEVEL_LABEL[item.document.source_level], "stage": stage, "stage_label": STAGE_LABEL[stage],
                     "asset_version_id": item.asset.asset_version_id})
    counts = Counter(row["stage"] for row in rows)
    total = len(rows)
    by_stage = {stage: counts.get(stage, 0) for stage in STAGES}
    return {"documents": rows, "total": total, "by_stage": by_stage,
            "referenced_rate": by_stage["referenced"] / total if total else None,
            "stages": [STAGE_LABEL[stage] for stage in STAGES], "note": ACTIVATION_NOTE,
            "catalog_summary": catalog.activation_summary(context)}
