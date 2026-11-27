"""B26 专家私有知识库：资料接入（B06 资产版本）、按专家隔离的可见文档清单、图片结构化描述、激活率与历史工单挖掘。

- 清单：`nexent_assets/knowledge/catalog.yaml`；真实上传结果：`nexent_assets/knowledge/indexes.json`。
- `ingest(catalog, context, document)`：一份资料 → 带版本的资产（图片另有结构化描述资产）。
- `make_document_paths_hook(library)`：给 B23 网关的 `nexent.document_paths` 钩子，按专家注入自己的可见文档。
- `search(library, expert, query, as_of=...)`：本地检索替身，只在该专家自己的资料里检索，按相关度与来源等级排序。
- `install_document_tools(registry, library)`：登记 `workorder_search`（主 Agent 慢通道用）。
- `mine(library, as_of=...)` / `publish(result, ...)`：历史工单确定性聚类 → Agent 段解读 → 候选规则（D54）。
- 演示：`python -m plantkg.knowledge.documents`。
"""

from __future__ import annotations

from plantkg.core.registry import Registry
from plantkg.knowledge.documents.activation import apply_activations, report
from plantkg.knowledge.documents.hook import DOCUMENT_PATHS_HOOK, install_document_paths, make_document_paths_hook
from plantkg.knowledge.documents.image import (
    BailianVision,
    ImageDescription,
    VISION_MODEL,
    describe_image,
    load_description,
    write_description,
)
from plantkg.knowledge.documents.ingest import IngestedDocument, ingest, ingest_all
from plantkg.knowledge.documents.mining import (
    AgentInterpreter,
    AssetCandidateStore,
    ClusterReading,
    MiningRecord,
    MiningResult,
    RuleBasedInterpreter,
    mine,
    publish,
)
from plantkg.knowledge.documents.retrieval import Hit, search
from plantkg.knowledge.documents.spec import (
    LEVEL_LABEL,
    LEVEL_RANK,
    STAGE_LABEL,
    DocumentSpec,
    ExpertSpec,
    KnowledgeLibrary,
    Manifest,
    SourceLevel,
    load_library,
)
from plantkg.knowledge.documents.workorders import (
    WorkOrder,
    WorkOrderCluster,
    cluster_workorders,
    load_workorders,
    register as register_workorder_tools,
    search_workorders,
)


def install_document_tools(registry: Registry, library: KnowledgeLibrary) -> None:
    """登记本模块提供的只读工具（workorder_search）；资料接入与检索仍走库与网关。"""
    register_workorder_tools(registry, library)


__all__ = [
    "DOCUMENT_PATHS_HOOK", "LEVEL_LABEL", "LEVEL_RANK", "STAGE_LABEL", "VISION_MODEL", "AgentInterpreter",
    "AssetCandidateStore", "BailianVision", "ClusterReading", "DocumentSpec", "ExpertSpec", "Hit", "ImageDescription",
    "IngestedDocument", "KnowledgeLibrary", "Manifest", "MiningRecord", "MiningResult", "RuleBasedInterpreter",
    "SourceLevel", "WorkOrder", "WorkOrderCluster", "apply_activations", "cluster_workorders", "describe_image",
    "ingest", "ingest_all", "install_document_paths", "install_document_tools", "load_description", "load_library",
    "load_workorders", "make_document_paths_hook", "mine", "publish", "register_workorder_tools", "report", "search",
    "search_workorders", "write_description",
]
