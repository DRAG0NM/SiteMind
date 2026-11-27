"""B47 预案动作（D55/D24）：从全局图谱 playbooks 取可执行动作，派生保护动作与工单协同处置动作。

图谱预案给出 ID、标签、人看的 steps 与机器读的动作模板（B25 schema：role/command/params/minutes/readiness，
见 graph.yaml）；本模块优先用图谱模板，图谱没给模板时回退下面的代码缺省映射（configured/synthetic）。
保护动作在开单时自动执行，协同处置动作等工单确认后执行。实体角色按场景装配（B17 `ids()`）解析；
装配不认识的实体跳过并记 note，动作不会瞎找一个对象执行。B19 分钟层装上时储能由分钟层独占，
预案保护动作里的电池动作跳过并记 note，避免同一电池被事件级与分钟级重复控制。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import Interval, ProposedAction, SimAction
from plantkg.world.scenario.runner import SCENARIO_MINUTE_HOOK

# 事件 → 预案处置动作默认播放文本（广播安抚，D24）
BROADCAST_TEXT = "救援人员已出发，请留在轿厢内不要扒门，保持通话"
ACTION_KINDS = ("protection", "disposal")
STORAGE_TAKEN_OVER = "储能由分钟层接管，跳过该保护动作"


@dataclass(frozen=True)
class ActionPlan:
    role: str  # 实体角色：$event / battery / zone:admin / 电梯群 ID
    command: str
    params: dict[str, Any]
    minutes: int
    readiness: str = "ready"


# 代码缺省映射：只在图谱预案没有 actions 模板时兜底（图谱优先）。按《愿景》与图谱预案按常理编写。
PLAYBOOK_PLANS: dict[str, dict[str, tuple[ActionPlan, ...]]] = {
    "transformer_overload": {
        "protection": (
            ActionPlan("battery", "discharge", {"power_kw": 400.0}, 30),
            ActionPlan("zone:admin", "set_mode", {"mode": "off"}, 15),
        ),
        "disposal": (
            ActionPlan("admin_low", "set_mode", {"mode": "zone"}, 45),
        ),
    },
    "elevator_entrapment": {
        "protection": (),
        "disposal": (
            ActionPlan("freight", "set_mode", {"mode": "takeover", "takeover_of": "$event"}, 120, "needs_human"),
            ActionPlan("$event", "ventilate", {"kw": 3.0}, 60, "needs_human"),
            ActionPlan("freight", "broadcast", {"text": BROADCAST_TEXT}, 5, "needs_human"),
        ),
    },
    "datacenter_power_loss": {"protection": (), "disposal": ()},
    "hazmat_ventilation_fault": {"protection": (), "disposal": ()},
    "air_leak": {"protection": (), "disposal": ()},
}


def playbook_for(registry: Any, event: Any) -> dict[str, Any] | None:
    """经 B25 登记的 router.playbooks 钩子取本事件的预案；带动作模板或代码有缺省映射的都算可用。"""
    for hook in reversed(registry.hooks_for("router.playbooks")):
        try:
            found = hook(event)
        except Exception:  # noqa: BLE001 — 图谱未就绪不挡开单
            continue
        items = [pb for pb in found or () if pb.get("actions") or pb.get("id") in PLAYBOOK_PLANS]
        if items:
            return items[0]
    return None


def _entity(role: str, runner: Any, event: Any) -> str | None:
    if role == "$event":
        return getattr(event, "entity_id", None)
    ids = runner.world.ids() if runner is not None else {}
    return ids.get(role) or ids.get(role.rsplit(":", 1)[-1]) or ids.get(role.rsplit("/", 1)[-1])


def _minute_layer_owns_storage(runner: Any) -> bool:
    """分钟层（B19）装上时登记 `scenario.minute_layer` 钩子：储能充放由它统一控制。"""
    registry = getattr(runner, "registry", None)
    return registry is not None and bool(registry.hooks_for(SCENARIO_MINUTE_HOOK))


def _templates(playbook: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """图谱模板优先；某个类别图谱没写时回退代码缺省映射（两类都可单独兜底）。"""
    graph_actions = playbook.get("actions") or {}
    plans = PLAYBOOK_PLANS.get(playbook.get("id") or "", {})
    out: dict[str, list[dict[str, Any]]] = {}
    for kind in ACTION_KINDS:
        if kind in graph_actions:
            out[kind] = list(graph_actions[kind])
        else:
            out[kind] = [asdict(spec) for spec in plans.get(kind, ())]
    return out


def plan(playbook: dict[str, Any] | None, event: Any, runner: Any, start: datetime
         ) -> tuple[tuple[SimAction, ...], tuple[ProposedAction, ...], tuple[dict[str, Any], ...]]:
    """返回（保护动作, 协同处置动作, 跳过记录）；动作生效区间从 start 起，实体解析不到的动作跳过并记 note。

    分钟层装上时储能由分钟层独占，保护动作里的电池动作跳过并记 note（图谱模板与代码缺省映射都过滤）。
    """
    if not playbook:
        return (), (), ()
    playbook_id = playbook.get("id")
    templates = _templates(playbook)
    protection: list[SimAction] = []
    disposal: list[ProposedAction] = []
    notes: list[dict[str, Any]] = []
    minute_storage = _minute_layer_owns_storage(runner)
    for kind, out in (("protection", protection), ("disposal", disposal)):
        for spec in templates[kind]:
            if minute_storage and kind == "protection" and spec["role"] == "battery":
                notes.append({"playbook_id": playbook_id, "role": spec["role"], "command": spec["command"],
                              "reason": STORAGE_TAKEN_OVER})
                continue
            entity = _entity(str(spec["role"]), runner, event)
            if not entity:
                notes.append({"playbook_id": playbook_id, "role": spec["role"], "command": spec["command"],
                              "reason": "实体解析不到，跳过"})
                continue
            params = {key: (event.entity_id if value == "$event" else value) for key, value in (spec.get("params") or {}).items()}
            interval = Interval(start=start, end=start + timedelta(minutes=int(spec["minutes"])))
            if kind == "protection":
                out.append(SimAction(entity_id=entity, command=spec["command"], params=params, interval=interval))
            else:
                out.append(ProposedAction(entity_id=entity, command=spec["command"], params=params, interval=interval,
                                          readiness=spec.get("readiness", "ready")))
    return tuple(protection), tuple(disposal), tuple(notes)
