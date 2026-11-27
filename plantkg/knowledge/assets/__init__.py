"""B06 资产目录：资料版本身份、稳定实体、血缘与激活状态。

装配层调用 `register(registry, catalog)` 即接入 AssetRepository 端口、ingest/list_assets/get_asset 工具
与 GET /assets、/assets/{asset_version_id}；它们按契约替换 mock 的 Fake。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from plantkg.core.contracts import AssetVersion, Context, ErrorCode, Page, PlantkgError, ToolResult
from plantkg.core.ports import AssetContentReader, AssetRepository
from plantkg.core.registry import Registry
from plantkg.knowledge.assets.catalog import ACTIVATION_NOTE, STAGES, SQLiteAssetCatalog

__all__ = ["ACTIVATION_NOTE", "STAGES", "SQLiteAssetCatalog", "make_tools", "make_rest", "register"]


def _summary(asset: AssetVersion) -> dict[str, Any]:
    return {"asset_version_id": asset.asset_version_id, "asset_type": asset.asset_type, "title": asset.title,
            "short_hash": asset.content_hash[7:15], "available_at": asset.available_at.isoformat(), "nature": asset.nature}


def make_tools(catalog: SQLiteAssetCatalog) -> dict[str, Any]:
    """工具处理函数（handler(context, **params) -> ToolResult）；规格取自契约 TOOL_SPECS。"""

    def ingest(context: Context, source_uri: str, asset_type: str, available_at: str) -> ToolResult:
        asset, existed = catalog.ingest_detail(context, source_uri, asset_type, available_at)
        data = {**_summary(asset), "content_hash": asset.content_hash, "parent_version_id": asset.parent_version_id,
                "entity_ids": list(asset.entity_ids), "deduplicated": existed}
        notice = "相同内容已接入，返回原版本" if existed else None
        return ToolResult(ok=True, business_status="done", data=data, notice=notice)

    def list_assets(context: Context, asset_type: str | None = None, cursor: str | None = None,
                    page_size: int | None = None) -> ToolResult:
        size = page_size or 50
        items, next_cursor = catalog.list(context, cursor, size, asset_type=asset_type)
        return ToolResult(ok=True, business_status="done", data={"items": [_summary(a) for a in items]},
                          page={"page_size": size, "next_cursor": next_cursor})

    def get_asset(context: Context, asset_version_id: str) -> ToolResult:
        asset = catalog.get(context, asset_version_id)
        data = {**asset.model_dump(mode="json", exclude_none=True), "short_hash": asset.content_hash[7:15],
                "lineage": catalog.lineage(context, asset_version_id),
                "activation": catalog.activation(context, asset_version_id)}
        return ToolResult(ok=True, business_status="done", data=data)

    return {"ingest": ingest, "list_assets": list_assets, "get_asset": get_asset}


def make_rest(catalog: SQLiteAssetCatalog) -> dict[str, Any]:
    def assets(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Page[AssetVersion]:
        try:
            size = int(query.get("page_size", 50))
        except ValueError:
            size = 0  # 交给 list 统一报 INVALID_REQUEST
        items, next_cursor = catalog.list(context, query.get("cursor"), size, asset_type=query.get("asset_type"))
        return Page[AssetVersion](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)

    def asset(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> AssetVersion:
        return catalog.get(context, path["asset_version_id"])

    return {"/assets": assets, "/assets/{asset_version_id}": asset}


def recall_asset(catalog: SQLiteAssetCatalog):
    """B27 recall(asset://…)：读资产目录的一行摘要与关联实体；返回 None 表示目录里没有这条资料。"""

    def lookup(identifier: str) -> dict[str, Any] | None:
        context = Context(plant_id=catalog.plant_id, industry_pack="-", actor=None, channel="none", trace_id="recall",
                          as_of=datetime.now(timezone.utc))
        try:
            asset = catalog.get(context, identifier)
        except PlantkgError as exc:
            if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.NOT_VISIBLE):
                return None
            raise
        summary = f"{asset.title or asset.asset_type}（{asset.asset_type}/{asset.nature}）：{asset.source_uri}"
        return {"summary": summary, "next": list(asset.entity_ids)}

    return lookup


def register(registry: Registry, catalog: SQLiteAssetCatalog) -> None:
    registry.register(AssetRepository, catalog)
    registry.register(AssetContentReader, catalog)
    for name, handler in make_tools(catalog).items():
        registry.register_tool(name, handler)
    for path, provider in make_rest(catalog).items():
        registry.register_rest("GET", path, provider)
    registry.register_hook("router.recall_asset", recall_asset(catalog))
