"""评测与证据链共用的文本哈希：先解 BOM、按 LF 归一化后取 SHA-256，跨平台可复现（#116/#148/#205）。

工作副本按平台换行检出，直接对文件字节取哈希会让同一份提交在不同平台得到不同值；
这里统一读文本、去掉开头的 UTF-8 BOM，把 CRLF 和孤立 CR 都归一成 LF，再对 UTF-8 字节取哈希。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def hash_text(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def hash_file(path) -> str:
    return hash_text(Path(path).read_text(encoding="utf-8"))
