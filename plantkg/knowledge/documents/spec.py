"""B26 专家资料清单：一份资料一条记录，决定入哪个专家库、来源等级、生效时间与数据性质。

清单是 `nexent_assets/knowledge/catalog.yaml`。真实平台结果写在 `nexent_assets/knowledge/indexes.json`
（每个专家的 Nexent 索引名、每份资料上传后的路径），由 `scripts/nexent_kb_upload.py` 生成；没有这份文件时
按 `knowledge_base/<文件名>` 派生注入路径，只用于离线演示与测试，不能用于真实运行。

与 B23 的接口：`KnowledgeLibrary.paths(expert, as_of)` 就是 `nexent.document_paths` 钩子的数据源，
网关按专家名分桶，只给该专家注入自己库里的可见文档。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from plantkg.core.contracts import DataNature, ErrorCode, PlantkgError, UtcDatetime

DEFAULT_CATALOG = Path("nexent_assets") / "knowledge" / "catalog.yaml"
DEFAULT_INDEXES = Path("nexent_assets") / "knowledge" / "indexes.json"
DERIVED_PREFIX = "knowledge_base/"  # 与 Nexent 上传后 file/upload 返回的路径前缀一致


class SourceLevel(str, Enum):
    """来源等级：国家标准/政策 > 设备手册 > 经验记录。检索排序里等级只做同级相关度的次序。"""

    national_standard = "national_standard"
    equipment_manual = "equipment_manual"
    experience_record = "experience_record"


LEVEL_RANK = {SourceLevel.national_standard: 3, SourceLevel.equipment_manual: 2, SourceLevel.experience_record: 1}
LEVEL_LABEL = {SourceLevel.national_standard: "国家标准/政策", SourceLevel.equipment_manual: "设备手册",
               SourceLevel.experience_record: "经验记录"}
STAGE_LABEL = {"dormant": "沉睡", "identified": "已标识", "in_graph": "已入图", "referenced": "被引用"}


class ActivationRef(BaseModel):
    """演示里把一份沉睡资料推进到下一阶段的登记事件；ref 写引用它的规则、动作或图谱版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    stage: Literal["identified", "in_graph", "referenced"]
    ref: str = ""
    at: UtcDatetime


class ExpertSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str
    agent: str  # Nexent Agent 名，B23 按它分桶注入
    index_name: str | None = None  # 真实 Nexent 索引名（形如 1-<hash>），由 indexes.json 覆盖


class DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    expert: str
    title: str
    path: str
    kind: Literal["text", "table", "image"]
    asset_type: str = Field(pattern=r"^[a-z0-9_-]+$")
    source_level: SourceLevel
    publisher: str = ""
    available_at: UtcDatetime
    nature: DataNature
    license: str | None = None
    tags: tuple[str, ...] = ()
    dormant: bool = False  # 沉睡资料（历史工单、点检表、维保记录）：入库时不标已标识，等被用到再推进
    kb_upload: bool = True  # 是否应上传 Nexent 知识库；Nexent 解析不了的原件设 false，不计入 failed 检查
    image_kind: Literal["nameplate", "chart"] | None = None  # 图片的结构化识读模板
    description_path: str | None = None  # 图片的结构化描述文件（Markdown + front matter），也是上传给 Nexent 的文本
    description_model: str | None = None
    asset_version_id: str | None = None  # 运行时上传资料的不可变 B06 原文引用

    @model_validator(mode="after")
    def _image_rules(self) -> "DocumentSpec":
        if self.kind == "image" and (self.image_kind is None or not self.description_path):
            raise ValueError(f"{self.document_id}：图片资料必须写 image_kind 与 description_path")
        if self.kind != "image" and (self.image_kind or self.description_path):
            raise ValueError(f"{self.document_id}：只有图片资料能写 image_kind / description_path")
        return self

    @property
    def upload_path(self) -> str:
        """上传给 Nexent 的文件：图片上传结构化描述（图片本身不能直接做文本检索）。"""
        return self.description_path or self.path


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    description: str = ""
    experts: dict[str, ExpertSpec]
    documents: tuple[DocumentSpec, ...]
    activations: tuple[ActivationRef, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "Manifest":
        seen: set[str] = set()
        for doc in self.documents:
            if doc.document_id in seen:
                raise ValueError(f"document_id 重复：{doc.document_id}")
            seen.add(doc.document_id)
            if doc.expert not in self.experts:
                raise ValueError(f"{doc.document_id} 的专家 {doc.expert} 不在 experts 里")
        for ref in self.activations:
            if ref.document_id not in seen:
                raise ValueError(f"激活事件指向不存在的资料 {ref.document_id}")
            document = next(doc for doc in self.documents if doc.document_id == ref.document_id)
            if ref.at < document.available_at:  # 资料还不存在时不能登记使用事件
                raise ValueError(f"{ref.document_id} 的激活事件 {ref.at.isoformat()} 早于资料生效时间 {document.available_at.isoformat()}")
        return self


class KnowledgeLibrary:
    """只读资料清单。一个实例对应一个厂区的全部专家库；不做检索，只给网关与本模块提供可见清单。"""

    def __init__(self, manifest: Manifest, indexes: Mapping[str, Any] | None = None, *, root: Path | None = None) -> None:
        self.manifest, self.root = manifest, Path(root) if root else Path.cwd()
        self._indexes = indexes or {}
        self._by_id = {doc.document_id: doc for doc in manifest.documents}
        self._index_name = {expert: spec.index_name for expert, spec in manifest.experts.items()}
        self._nexent_path: dict[str, str] = {}
        for expert, info in (self._indexes.get("experts") or {}).items():
            if info.get("index_name"):
                self._index_name[expert] = str(info["index_name"])
        for document_id, info in (self._indexes.get("documents") or {}).items():
            # 只有 Nexent 处理完成的文件才注入；PROCESS_FAILED 的文件注入了也检索不到
            if info.get("nexent_path") and str(info.get("status")) in ("COMPLETED", "completed", "SUCCESS"):
                self._nexent_path[document_id] = str(info["nexent_path"])
        self._uploaded_at = self._indexes.get("created_at")
        self.sources = []  # (expert, as_of) -> 只读增量资料；由装配层注册
        self.content_readers = []  # document -> 原文片段或 None

    @property
    def live(self) -> bool:
        """有真实上传结果（indexes.json）时为 True；此时只注入确实上传成功的路径。"""
        return bool(self._nexent_path)

    @property
    def uploaded_at(self) -> str | None:
        return self._uploaded_at

    @property
    def experts(self) -> Mapping[str, ExpertSpec]:
        return self.manifest.experts

    def index_name(self, expert: str) -> str | None:
        spec = self.manifest.experts.get(expert)
        return self._index_name.get(expert) or (spec.index_name if spec else None)

    def documents(self, expert: str | None = None, as_of: datetime | None = None) -> tuple[DocumentSpec, ...]:
        docs = self.manifest.documents if expert is None else tuple(d for d in self.manifest.documents if d.expert == expert)
        if as_of is not None:
            docs = tuple(d for d in docs if d.available_at <= as_of)
        for source in self.sources:
            docs += tuple(source(expert, as_of))
        return docs

    def document(self, document_id: str) -> DocumentSpec:
        try:
            return self._by_id[document_id]
        except KeyError:
            for source in self.sources:
                for document in source(None, None):
                    if document.document_id == document_id:
                        return document
            raise PlantkgError(ErrorCode.NOT_FOUND, f"资料清单里没有 {document_id}") from None

    def resolve(self, doc: DocumentSpec) -> Path:
        path = Path(doc.path)
        return path if path.is_absolute() else self.root / path

    def upload_file(self, doc: DocumentSpec) -> Path:
        path = Path(doc.upload_path)
        return path if path.is_absolute() else self.root / path

    def indexed_path(self, doc: DocumentSpec) -> str | None:
        """真实上传路径；没有上传结果时为 None。"""
        return self._nexent_path.get(doc.document_id)

    def injection_path(self, doc: DocumentSpec) -> str:
        """注入给 Nexent 检索的路径。没有真实上传结果时给派生路径，只用于离线演示。"""
        return self.indexed_path(doc) or DERIVED_PREFIX + self.upload_file(doc).name

    def paths(self, expert: str, as_of: datetime, *, strict: bool = False) -> tuple[str, ...]:
        """该专家在 as_of 下应注入的文档清单。

        strict=True 是网关真实运行的用法：没有上传结果或有关键文档未完成上传就抛 PRECONDITION，
        不退回派生路径、也不静默剔除，避免“清单非空但检索为空”的假成功。
        strict=False 只用于离线演示与测试，缺上传结果时按派生路径展示。
        """
        docs = tuple(doc for doc in self.documents(expert, as_of) if doc.kb_upload)
        if strict:
            if not self._indexes:
                raise PlantkgError(ErrorCode.PRECONDITION,
                                   f"没有 Nexent 上传结果（{DEFAULT_INDEXES}），拒绝注入派生路径；先跑 scripts/nexent_kb_upload.py")
            missing = [doc.document_id for doc in docs if not self.indexed_path(doc)]
            if missing:
                raise PlantkgError(ErrorCode.PRECONDITION,
                                   f"{expert} 的知识库文档未全部上传成功，拒绝运行：{missing}；先重跑上传脚本")
            return tuple(self.injection_path(doc) for doc in docs)
        if self.live:
            docs = tuple(doc for doc in docs if self.indexed_path(doc))
        return tuple(self.injection_path(doc) for doc in docs)

    def activation_refs(self) -> Sequence[ActivationRef]:
        return self.manifest.activations


def load_library(root: Path | None = None, *, catalog_path: str | Path | None = None,
                 indexes_path: str | Path | None = None) -> KnowledgeLibrary:
    root = Path(root) if root else Path.cwd()
    catalog = _resolve(root, catalog_path or DEFAULT_CATALOG)
    try:
        manifest = Manifest.model_validate(yaml.safe_load(catalog.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"没有资料清单 {catalog}") from None
    except (yaml.YAMLError, ValueError) as exc:
        raise PlantkgError(ErrorCode.PRECONDITION, f"资料清单 {catalog} 不合法：{exc}") from None
    indexes_file = _resolve(root, indexes_path or DEFAULT_INDEXES)
    try:
        indexes = json.loads(indexes_file.read_text(encoding="utf-8")) if indexes_file.is_file() else None
    except ValueError:
        raise PlantkgError(ErrorCode.PRECONDITION, f"上传结果 {indexes_file} 不是合法 JSON；删掉它可按离线派生路径运行") from None
    return KnowledgeLibrary(manifest, indexes, root=root)


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path
