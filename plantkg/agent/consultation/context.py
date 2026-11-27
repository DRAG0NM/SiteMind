"""会诊现场上下文（B29）：按（运行, 事件单, 轮次）寄存本轮专家运行要注入的事件与状态。

live 专家经自己的 propose 工具取本轮事件与状态；这里只做本进程内的短期寄存，查不到本轮内容时
返回空，不回退到最近事件。容量有上限，写入与读取都在锁内。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from plantkg.core.contracts import RunBinding

CAPACITY = 64  # 最多寄存多少轮的现场：超出按最早写入淘汰


class ConsultationContext:
    def __init__(self, capacity: int = CAPACITY) -> None:
        self._items: OrderedDict[tuple[str, str, int], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = capacity

    def put(self, binding: RunBinding, payload: Mapping[str, Any]) -> None:
        key = (binding.run_id, binding.ticket_id, binding.round)
        with self._lock:
            self._items[key] = dict(payload)
            self._items.move_to_end(key)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, binding: RunBinding) -> dict[str, Any] | None:
        key = (binding.run_id, binding.ticket_id, binding.round)
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
                return dict(found)
            return None
