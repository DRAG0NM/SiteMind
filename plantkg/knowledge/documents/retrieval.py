"""本地检索替身与排序：离线演示与冒烟测试用，不替代 Nexent 检索。

真实检索由 Nexent 知识库完成，范围由网关注入的 `document_paths` 决定（B23）。
这里按同样的隔离规则（只看该专家、只看 as_of 前可见的资料）给一份可复现的文本检索结果，
用于展示“电梯专家检索不到暖通资料”“按相关度与来源等级排序”“引用规程原文”。
排序规则：相关度优先；相关度相同的（四舍五入到 4 位）按来源等级 国家标准/政策 > 设备手册 > 经验记录。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.knowledge.documents.spec import DocumentSpec, KnowledgeLibrary, LEVEL_RANK, SourceLevel

TEXT_SUFFIXES = {".md", ".txt", ".csv"}
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./%-]*")


@dataclass(frozen=True)
class Hit:
    document: DocumentSpec
    locator: str
    text: str
    score: float
    level: SourceLevel


def search(library: KnowledgeLibrary, expert: str, query: str, *, as_of: datetime, top_k: int = 5) -> list[Hit]:
    """只在该专家自己的、as_of 前可见的资料里检索；每份资料取最相关的一段。"""
    wanted = tokens(query)
    if not wanted:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "检索词为空")
    best: dict[str, Hit] = {}
    for document in library.documents(expert, as_of):
        for locator, text in chunks(library, document):
            score = len(wanted & tokens(text)) / len(wanted)
            if score <= 0:
                continue
            current = best.get(document.document_id)
            if current is None or score > current.score:
                best[document.document_id] = Hit(document=document, locator=locator, text=text, score=round(score, 4),
                                                 level=SourceLevel(document.source_level))
    return rank(best.values())[:top_k]


def rank(hits) -> list[Hit]:
    return sorted(hits, key=lambda h: (-round(h.score, 4), -LEVEL_RANK[SourceLevel(h.level)], h.document.document_id))


def chunks(library: KnowledgeLibrary, document: DocumentSpec) -> list[tuple[str, str]]:
    """把资料切成（小标题/行号, 文本）。PDF 等二进制不在这里抽取，真实运行由 Nexent 解析。"""
    for reader in library.content_readers:
        result = reader(document)
        if result is not None:
            return result
    path = _readable_path(library, document)
    if path is None:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".csv":
        return _csv_chunks(text)
    return _markdown_chunks(text)


def tokens(text: str) -> set[str]:
    """中文按二元字组、英文数字按词切；足够覆盖规程条款与铭牌字段这类短文本。"""
    found: set[str] = set()
    for run in _CJK_RUN.findall(text):
        found.update({run} if len(run) == 1 else {run[i:i + 2] for i in range(len(run) - 1)})
    found.update(word.lower() for word in _WORD.findall(text))
    return found


def _readable_path(library: KnowledgeLibrary, document: DocumentSpec) -> Path | None:
    raw = document.description_path if document.kind == "image" else document.path
    path = Path(raw or "")
    path = path if path.is_absolute() else library.root / path
    return path if path.suffix.lower() in TEXT_SUFFIXES else None


def _markdown_chunks(text: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    heading, buffer = "正文", []

    def flush() -> None:
        if buffer:
            chunks.append((heading, "\n".join(buffer).strip()))
            buffer.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip() or "正文"
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()
    return chunks


def _csv_chunks(text: str) -> list[tuple[str, str]]:
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    return [(f"第 {number} 行", "；".join(f"{name} {value}" for name, value in zip(header, row)))
            for number, row in enumerate(body, start=2)]
