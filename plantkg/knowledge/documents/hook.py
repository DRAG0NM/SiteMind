"""B23 网关的 `nexent.document_paths` 钩子：按专家返回该专家库在 as_of 下的可见文档。

网关在 `prepare` 里对每个参与者调用这个钩子，把结果注入该 Agent 桶的 `knowledge_base_search`。
绑定知识库但清单为空时网关拒绝运行（P21c 失败关闭），所以这里不返回 None 兜底：
专家不在清单里就直接返回空元组，让运行失败，而不是退回去读 yaml 里的旧路径。
默认 strict：没有上传结果或有关键文档未完成上传就抛 PRECONDITION，不退回派生路径，也不静默剔除。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from plantkg.core.registry import Registry
from plantkg.knowledge.documents.spec import KnowledgeLibrary

DOCUMENT_PATHS_HOOK = "nexent.document_paths"  # 与 plantkg/integrations/nexent/__init__.py 的常量一致


def make_document_paths_hook(library: KnowledgeLibrary, *, strict: bool = True):
    def hook(asset: Any, as_of: datetime) -> tuple[str, ...] | None:
        if getattr(asset, "knowledge", None) is None:
            return None
        domain = getattr(asset, "domain", None)
        if domain not in library.experts:
            return ()
        return library.paths(domain, as_of, strict=strict)

    return hook


def install_document_paths(registry: Registry, library: KnowledgeLibrary, hook_name: str = DOCUMENT_PATHS_HOOK) -> None:
    registry.register_hook(hook_name, make_document_paths_hook(library))
