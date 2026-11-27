"""观测层最近一次快照的进程内总线（B22）。

场景运行与 MCP 工具在同一进程时，`sensor_query` 从这里取最近一分钟的观测；跨进程或没有运行中的
场景时返回空，由工具如实说明。只保留最近快照，不承担历史存储。

键是 `(plant_id, run_id)`：同一进程跑多个运行时各记一份，避免后跑的运行覆盖先跑的读数（#138/#186）。
另记一份 `(plant_id, None)` 作为“该厂区最近一次发布”，只给不带运行标识的旧调用兜底。
"""
from __future__ import annotations

from typing import Any


class ObservationBus:
    _latest: dict[tuple[str, str | None], dict[str, Any]] = {}
    MAX_RUNS = 32  # 每个厂区按运行保留的快照数上限；超出淘汰最旧的运行，避免长期 live 进程无限增长

    @classmethod
    def publish(cls, plant_id: str, snapshot: dict[str, Any], history: dict[str, Any] | None = None,
                run_id: str | None = None) -> None:
        box = {"snapshot": snapshot, "history": history or {}}
        if run_id is not None and (plant_id, run_id) not in cls._latest:
            runs = [key for key in cls._latest if key[0] == plant_id and key[1] is not None]
            for key in runs[:max(0, len(runs) - cls.MAX_RUNS + 1)]:
                cls._latest.pop(key, None)
        cls._latest[(plant_id, run_id)] = box
        if run_id is not None:
            cls._latest[(plant_id, None)] = box

    @classmethod
    def latest(cls, plant_id: str, run_id: str | None = None) -> dict[str, Any] | None:
        """按运行取快照；不给 run_id 时返回该厂区最近一次发布（旧调用兼容，调用方需标注可能来自其他运行）。"""
        return cls._latest.get((plant_id, run_id))

    @classmethod
    def clear(cls) -> None:
        cls._latest.clear()
