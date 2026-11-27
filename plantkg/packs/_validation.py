"""B10～B13 领域包共用的配置校验：bool 不算数值、要求有限数，读 YAML 时把原生异常包成 DOMAIN_INVALID。

只放跨包装载器共用的那一层；各包自己的工程上下界（如储能效率、电价系数）留在包内，
因为 B11/B12 的越界要变成 PendingIssue/_Bad 而不是在这里直接抛异常。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from plantkg.core.contracts import ErrorCode, PlantkgError


def is_number(value: Any) -> bool:
    """配置里的数值：排除 bool（True/False 会被 isinstance 当成 1/0），并要求有限。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value: Any, key: str) -> float:
    """取单个配置数值：bool、非数、NaN/inf 都拒绝，错误文本带配置键。"""
    if not is_number(value):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置项 {key}={value!r} 必须是有限数（bool 不算数值）")
    return float(value)


def read_yaml(path: Path, config_id: str) -> dict:
    """读配置 YAML：缺文件、无权限、YAML 语法错、顶层不是映射，统一包成 DOMAIN_INVALID 并指向配置文件。"""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 顶层须是映射")
    return data
