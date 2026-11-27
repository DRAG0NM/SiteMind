"""历史工单（D54）：确定性聚类与 `workorder_search` 工具。

流程段只做确定性的事：按（设备，故障现象）分组，再按时间间隔切窗口（默认 90 天）聚成类；
不做语义归并，不同写法的现象不会被硬凑成一类。Agent 段的解读与候选规则生成见 `mining.py`。

`workorder_search` 经注册表登记给主 Agent 慢通道用；专家私有库不受影响，仍只经自己的
`document_paths` 检索（B23 网关按专家注入）。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from plantkg.core.contracts import Context, ErrorCode, PlantkgError, ToolResult, UtcDatetime
from plantkg.core.registry import Registry
from plantkg.knowledge.documents.spec import DocumentSpec, KnowledgeLibrary

WORKORDER_TAG = "work_order"
WINDOW_DAYS = 90
MAX_PAGE = 50  # 与契约 _PAGE_SIZE 一致；大页只回摘要字段控制返回大小
RICH_PAGE = 20  # 不超过这个页大小才带工单原文片段
DEFAULT_PAGE = 10
_TEXT_LIMIT = 160  # 单条工单返回给模型的原文片段上限
_CURSOR = "workorder_search"
_UTC = TypeAdapter(UtcDatetime)


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    occurred_at: datetime
    equipment: str
    phenomenon: str
    action: str
    downtime_min: int | None
    owner: str
    repeated: bool
    text: str  # 表头 + 本行值的原文，供规则引用与工作台点开查看

    def summary(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "occurred_at": self.occurred_at.isoformat(), "equipment": self.equipment,
                "phenomenon": self.phenomenon, "action": self.action, "downtime_min": self.downtime_min,
                "owner": self.owner, "repeated": self.repeated, "text": self.text}


@dataclass(frozen=True)
class WorkOrderCluster:
    cluster_id: str
    equipment: str
    phenomenon: str
    orders: tuple[WorkOrder, ...]

    @property
    def count(self) -> int:
        return len(self.orders)

    @property
    def first_at(self) -> datetime:
        return self.orders[0].occurred_at

    @property
    def last_at(self) -> datetime:
        return self.orders[-1].occurred_at

    @property
    def citations(self) -> tuple[str, ...]:
        return tuple(order.order_id for order in self.orders)

    def summary(self) -> dict[str, Any]:
        return {"cluster_id": self.cluster_id, "equipment": self.equipment, "phenomenon": self.phenomenon,
                "count": self.count, "first_at": self.first_at.isoformat(), "last_at": self.last_at.isoformat(),
                "order_ids": list(self.citations)}


def load_workorders(library: KnowledgeLibrary, *, as_of: datetime) -> list[WorkOrder]:
    """读 as_of 下可见、带 `work_order` 标签的表格资料；未来才生效的工单不读。"""
    orders: list[WorkOrder] = []
    for document in library.documents(as_of=as_of):
        if document.kind == "table" and WORKORDER_TAG in document.tags:
            orders.extend(_read_csv(library.resolve(document), document))
    return sorted(orders, key=lambda order: (order.occurred_at, order.order_id))


def cluster_workorders(orders: list[WorkOrder], *, window_days: int = WINDOW_DAYS) -> list[WorkOrderCluster]:
    """按（设备，现象）分组后按时间间隔切窗口；窗口内相邻两条间隔不超过 window_days 即同一类。"""
    if window_days <= 0:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "window_days 必须大于 0")
    groups: dict[tuple[str, str], list[WorkOrder]] = defaultdict(list)
    for order in orders:
        groups[(order.equipment.strip(), _normalize(order.phenomenon))].append(order)
    clusters: list[WorkOrderCluster] = []
    for (equipment, phenomenon), items in groups.items():
        items = sorted(items, key=lambda order: (order.occurred_at, order.order_id))
        current = [items[0]]
        for order in items[1:]:
            if order.occurred_at - current[-1].occurred_at > timedelta(days=window_days):
                clusters.append(_cluster(equipment, phenomenon, current))
                current = [order]
            else:
                current.append(order)
        clusters.append(_cluster(equipment, phenomenon, current))
    return sorted(clusters, key=lambda cluster: (-cluster.count, cluster.first_at, cluster.cluster_id))


def search_workorders(library: KnowledgeLibrary, *, as_of: datetime, equipment: str | None = None,
                      phenomenon: str | None = None, from_time: datetime | None = None, to_time: datetime | None = None
                      ) -> tuple[list[WorkOrder], list[WorkOrderCluster]]:
    """按设备、现象关键字与时间范围筛 as_of 可见的工单；聚类在筛选结果上重新算，避免跨范围拼接。"""
    orders = [order for order in load_workorders(library, as_of=as_of)
              if _matches(order, equipment, phenomenon, from_time, to_time)]
    return orders, cluster_workorders(orders)


def register(registry: Registry, library: KnowledgeLibrary) -> None:
    """登记 workorder_search（规格来自契约 TOOL_SPECS）；只读，不要求 ctx 里的写权限。"""
    registry.register_tool("workorder_search", _handler(library))


def _handler(library: KnowledgeLibrary):
    def workorder_search(context: Context, equipment: str | None = None, phenomenon: str | None = None,
                         from_time: str | None = None, to_time: str | None = None, cursor: str | None = None,
                         page_size: int | None = None) -> ToolResult:
        start, end = _parse_time(from_time, "from_time"), _parse_time(to_time, "to_time")
        if start and end and start > end:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "from_time 不能晚于 to_time")
        orders, clusters = search_workorders(library, as_of=context.as_of, equipment=equipment, phenomenon=phenomenon,
                                             from_time=start, to_time=end)
        size = page_size or DEFAULT_PAGE
        if not 1 <= size <= MAX_PAGE:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"page_size 须在 1～{MAX_PAGE}")
        offset = _decode_cursor(cursor)
        page = orders[offset:offset + size]
        next_cursor = _encode_cursor(offset + size) if offset + size < len(orders) else None
        # 大页只回摘要字段，避免一次返回超过 8KB；小页带原文片段
        rich = size <= RICH_PAGE
        items = []
        for order in page:
            if rich:
                items.append({**order.summary(), "text": order.text[:_TEXT_LIMIT]})
            else:
                items.append({"order_id": order.order_id, "occurred_at": order.occurred_at.isoformat(),
                              "equipment": order.equipment, "phenomenon": order.phenomenon})
        data = {"items": items,
                "clusters": [cluster.summary() for cluster in clusters[:10]],
                "total": len(orders), "matched_clusters": len(clusters),
                "window_days": WINDOW_DAYS,
                "note": "聚类按（设备，故障现象）与时间间隔确定性切分，不做语义归并；"
                        "工单按 as_of 可见；page_size>20 时只回摘要字段，原文用更小的页取"}
        return ToolResult(ok=True, business_status="done", data=data,
                          page={"page_size": size, "next_cursor": next_cursor}, run_id=context.run_id)
    return workorder_search


def _read_csv(path: Path, document: DocumentSpec) -> list[WorkOrder]:
    try:
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"找不到工单文件 {path}") from None
    orders = []
    for row in rows:
        values = {key.strip(): (value or "").strip() for key, value in row.items() if key}
        try:
            occurred = _parse_time(values.get("日期"), "日期")
            assert occurred is not None
            downtime = values.get("停机min")
            orders.append(WorkOrder(
                order_id=values["工单号"], occurred_at=occurred, equipment=values["设备"], phenomenon=values["现象"],
                action=values.get("处理", ""), downtime_min=int(downtime) if downtime else None,
                owner=values.get("负责人", ""), repeated=values.get("是否复发") == "是",
                text="；".join(f"{key} {value}" for key, value in values.items())))
        except (KeyError, ValueError):
            raise PlantkgError(ErrorCode.PRECONDITION, f"{document.document_id} 的工单行不合法：{values}") from None
    return orders


def _matches(order: WorkOrder, equipment: str | None, phenomenon: str | None,
             from_time: datetime | None, to_time: datetime | None) -> bool:
    if equipment and order.equipment.casefold() != equipment.strip().casefold():
        return False
    if phenomenon and _normalize(phenomenon) not in _normalize(order.phenomenon):
        return False
    if from_time and order.occurred_at < from_time:
        return False
    if to_time and order.occurred_at > to_time:
        return False
    return True


def _cluster(equipment: str, phenomenon: str, orders: list[WorkOrder]) -> WorkOrderCluster:
    digest = hashlib.sha1("|".join(order.order_id for order in orders).encode()).hexdigest()[:8]
    return WorkOrderCluster(cluster_id=f"wo-cluster://{equipment}/{_slug(phenomenon)}/{orders[0].occurred_at:%Y%m%d}-{digest}",
                            equipment=equipment, phenomenon=orders[0].phenomenon, orders=tuple(orders))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text.casefold()).strip("-.") or "unknown"


def _parse_time(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc) if len(text) == 10 else _UTC.validate_python(text)
    except ValueError:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"{name} 须是 ISO-8601 时间") from None
    return parsed.astimezone(timezone.utc)


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"tool": _CURSOR, "offset": offset}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = int(data["offset"])
        if data.get("tool") != _CURSOR or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "cursor 无效") from None
