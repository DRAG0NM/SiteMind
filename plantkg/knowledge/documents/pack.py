"""B26 领域包入口：登记 workorder_search；Nexent 启用时再登记按专家注入 document_paths 的钩子。

由 pack.yaml 的 entrypoint 调用，签名 (registry, settings) -> None。领域包按 settings.packs 的顺序加载，
都发生在 bootstrap 的 B23 `nexent.install` 之前，所以网关读钩子时已经登记好。
清单根目录相对仓库根；资料路径与上传结果都在 nexent_assets/knowledge/ 下。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plantkg.core.registry import Registry
from plantkg.knowledge.documents import install_document_paths, install_document_tools, load_library
from plantkg.knowledge.documents.hook import DOCUMENT_PATHS_HOOK

ROOT = Path(__file__).resolve().parents[3]


def install(registry: Registry, settings: Any = None) -> None:
    library = load_library(ROOT)
    registry.register_hook('knowledge.documents.library', lambda: library)
    install_document_tools(registry, library)
    if settings is not None and getattr(getattr(settings, "nexent", None), "enabled", False):
        install_document_paths(registry, library, DOCUMENT_PATHS_HOOK)
