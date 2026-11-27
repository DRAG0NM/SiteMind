"""资料入库（B06 资产版本）与激活起点。

`ingest(document)` 是任务卡里的接口：一份资料 → 一个带版本的资产。图片资料还会把
deepseek-flash 生成的结构化描述作为派生资产一起入库，检索看到的是描述文本。
非沉睡资料入库即登记“已标识”（已编目、已归到某位专家）；沉睡资料不登记，等被点检、
入图或规则引用时再由使用方推进（见 `activation.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from plantkg.core.contracts import AssetVersion, Context, ErrorCode, PlantkgError
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
from plantkg.knowledge.documents.image import load_description
from plantkg.knowledge.documents.spec import DocumentSpec, KnowledgeLibrary


@dataclass(frozen=True)
class IngestedDocument:
    document: DocumentSpec
    asset: AssetVersion
    description: AssetVersion | None = None


def ingest(catalog: SQLiteAssetCatalog, context: Context, document: DocumentSpec) -> IngestedDocument:
    """把一份资料接入资产目录；同内容重复接入返回原版本（B06 幂等）。"""
    source = document.path
    asset = catalog.ingest(context, source, document.asset_type, document.available_at, nature=document.nature.value,
                           title=document.title, license=document.license)
    if not document.dormant:
        catalog.record_activation(context, asset.asset_version_id, "identified", ref=_catalog_ref(document), available_at=document.available_at)
    description = None
    if document.kind == "image":
        description = _ingest_description(catalog, context, document)
    return IngestedDocument(document=document, asset=asset, description=description)


def ingest_all(catalog: SQLiteAssetCatalog, context: Context, library: KnowledgeLibrary) -> list[IngestedDocument]:
    return [ingest(catalog, context, document) for document in library.manifest.documents]


def _ingest_description(catalog: SQLiteAssetCatalog, context: Context, document: DocumentSpec) -> AssetVersion:
    path = Path(document.description_path or "")
    if not path.is_file():
        raise PlantkgError(ErrorCode.PRECONDITION,
                           f"图片 {document.document_id} 还没有结构化描述：先跑 python -m plantkg.knowledge.documents --refresh-images")
    front = load_description(path)
    if front.get("kind") != document.image_kind:
        raise PlantkgError(ErrorCode.PRECONDITION, f"{path} 的描述类型与清单不一致")
    asset = catalog.ingest(context, document.description_path or "", "process_doc", document.available_at,
                           nature="derived", title=f"{document.title}（结构化描述）")
    catalog.record_activation(context, asset.asset_version_id, "identified", ref="image_description", available_at=document.available_at)
    return asset


def _catalog_ref(document: DocumentSpec) -> str:
    return f"kb://{document.expert}"

