"""`sensor_query` 正式实现（B22 / D59）：读观测层最近快照，覆盖 B27 的 fallback。

工具经注册表登记，Nexent 侧名为 `plantkg_sensor_query`。场景与 MCP 在同一进程时从 ObservationBus 取数；
没有运行中的场景时返回 degraded 并说明，不编造读数。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from plantkg.core.contracts import Context, ToolResult
from plantkg.core.registry import Registry

from .bus import ObservationBus
from .layer import query_snapshot


def make_handler(settings: Any):
    plant_id = getattr(settings, "plant_id", "demo-electronics")

    def sensor_query(context: Context | None = None, entity: str | None = None, sensor_id: str | None = None,
                     minutes: int = 5, since: str | None = None) -> ToolResult:
        pid = getattr(context, "plant_id", None) or plant_id
        rid = getattr(context, "run_id", None) or None
        notice = None
        box = ObservationBus.latest(pid, rid)
        fallback = box is None and rid is not None
        if fallback:  # 当前运行还没发布：回退该厂区最近一次发布，并说明可能来自其他运行
            box = ObservationBus.latest(pid)
            notice = f"运行 {rid} 没有观测快照，回退到该厂区最近一次发布；读数可能属于其他运行"
        if not box:
            return ToolResult(ok=True, business_status="degraded", notice="观测层没有运行中的场景，无法给出读数与残差",
                              data={"readings": [], "relations": [], "virtuals": [], "candidates": [], "alarms": []})
        data = query_snapshot(box["snapshot"], box["history"], entity=entity, sensor_id=sensor_id,
                              minutes=max(1, min(int(minutes or 5), 60)), since=since)
        # 回退时用快照自身的运行标识（缺失即 None），不冒充请求的运行（#201）
        run_id = data.get("run_id") if fallback else data.get("run_id") or rid
        return ToolResult(ok=True, business_status="done", data=data, run_id=run_id, notice=notice,
                          evidence_ids=(data.get("config_ref") or "",) if data.get("config_ref") else ())

    return sensor_query


def make_readings(settings: Any):
    """值守层 router.readings：实体窗口内的读数行（at、value、quality、sensor_id、residual_alarm）。没有运行中的场景返回空。"""
    plant_id = getattr(settings, "plant_id", "demo-electronics")

    def readings(entity: str, start: datetime, end: datetime, run_id: str | None = None) -> list[dict[str, Any]]:
        box = ObservationBus.latest(plant_id, run_id)
        if box is None and run_id:  # 该运行还没发布时回退最近一次发布；该运行自己的快照存在时只读自己的
            box = ObservationBus.latest(plant_id)
        if not box:
            return []
        data = query_snapshot(box["snapshot"], box["history"], entity=_sensor_entity(entity), minutes=60)
        alarmed = {sid for relation in data["relations"] if relation.get("alarm") for sid in relation.get("sensors", ())}
        rows = [{**item, "sensor_id": sid, "residual_alarm": sid in alarmed, "nature": "simulated"}
                for sid, items in data["history"].items() for item in items if start <= datetime.fromisoformat(item["at"]) <= end]
        return sorted(rows, key=lambda row: row["at"])

    return readings


def _sensor_entity(entity: str) -> str:
    """entity:// 标识 → sensors.yaml 的 entity 写法（AC-2、zone:admin、AIR-HDR）。"""
    if not entity.startswith("entity://"):
        return entity
    kind, code = entity.rsplit("/", 2)[-2:]
    return {"zone": f"zone:{code}", "air_system": "AIR-HDR"}.get(kind, code)


def install(registry: Registry, settings: Any) -> None:
    registry.register_tool("sensor_query", make_handler(settings))
    registry.register_hook("router.readings", make_readings(settings))
