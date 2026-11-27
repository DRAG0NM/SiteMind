"""全局图谱的 MCP 工具（B25）。经领域包 global_graph 的 pack.yaml 登记，Nexent 侧名为 plantkg_<名>。

结果压成短文本（路径写成“A → B → C”，长列表截断并给总数），保证不超过 ToolResult 的 8KB 上限。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from plantkg.core.contracts import TOOL_TEXT_MAX_BYTES, Context, ErrorCode, PlantkgError, ToolResult
from plantkg.core.security import NO_CTX_NOTICE
from plantkg.knowledge.graph.graph import DEFAULT_CONFIG, MEDIA, GlobalGraph


# 工具规格的权威定义在 core.contracts.TOOL_SPECS（v0.8，#160）；这里只登记处理器。
TOOL_NAMES = ("trace_impact", "find_sources", "safety_tags", "priority_rules", "playbooks")


def _arrow(path: list[str]) -> str:
    return " → ".join(path)


def _fits(data: dict) -> bool:
    """按无会话降级的最坏形态量字节（多一行提示）；超过 8KB 硬上限时 to_agent_text 抛错，视为装不下。"""
    probe = ToolResult(ok=True, business_status="degraded", notice=NO_CTX_NOTICE, data=data)
    try:
        text = probe.to_agent_text()
    except PlantkgError as exc:
        if exc.code != ErrorCode.RESULT_TOO_LARGE:
            raise
        return False
    return len(text.encode("utf-8")) <= TOOL_TEXT_MAX_BYTES


def _fill(head: dict, *lists: tuple[str, list]) -> dict:
    """按顺序把各列表逐条装进 8KB 预算（列表已按重要性排好），装不下就截断；总数与 truncated 常驻。"""
    out = head | {f"{key}_total": len(items) for key, items in lists} | {"truncated": False} | {key: [] for key, _ in lists}
    for key, items in lists:
        for item in items:
            if not _fits(out | {key: out[key] + [item]}):
                out["truncated"] = True
                break
            out[key] = out[key] + [item]
    return out


def _tags(tags: list[dict]) -> list[dict]:
    return [{"id": t["id"], "label": t["label"], "clause": t["clause"], "clause_text": t["clause_text"],
             **({"via": t["via"]} if "via" in t else {}), "nodes": t["nodes"][:10], "path": _arrow(t["path"])} for t in tags]


def make_tools(graph: GlobalGraph) -> dict[str, Any]:
    def check(context: Context) -> None:
        if context.plant_id != graph.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "全局图谱不属于本厂区")

    def done(data: dict) -> ToolResult:
        return ToolResult(ok=True, business_status="done", data=data)

    def trace_impact(context: Context, entity: str, media: str | None = None) -> ToolResult:
        check(context)
        chosen = tuple(m.strip() for m in media.split(",") if m.strip()) if media else None
        if chosen and set(chosen) - set(MEDIA):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知介质 {sorted(set(chosen) - set(MEDIA))}")
        r = graph.trace_impact(entity, chosen)
        affected = [f"{a['code']} {a['label']}：{_arrow(a['path'])}" for a in r["affected"]]
        head = {"entity": r["entity"], "label": r["label"], "knowledge_version": r["knowledge_version"]}
        zones = [{"zone": z["zone"], "label": z["label"], "path": _arrow(z["path"])} for z in r["zones"]]
        # 依次装安全标记、区域、楼层、受影响设备
        return done(_fill(head, ("safety", _tags(r["safety"])), ("zones", zones), ("floors", r["floors"]), ("affected", affected)))

    def find_sources(context: Context, location: str, symptom: str) -> ToolResult:
        check(context)
        r = graph.find_sources(location, symptom)
        cands = [{"code": c["code"], "label": c["label"], "domain": c["domain"], "role": c["role"],
                  **({"rated_kw": c["rated_kw"]} if c["rated_kw"] else {}), "path": _arrow(c["path"])} for c in r["candidates"]]
        head = {"location": location, "symptom": symptom, "knowledge_version": r["knowledge_version"],
                "domains": r["domains"], **({"notes": r["notes"]} if "notes" in r else {})}
        return done(_fill(head, ("candidates", cands)))  # 候选已按症状排好序

    def safety_tags(context: Context, target: str, action: str, occupied: bool | None = None) -> ToolResult:
        check(context)
        r = graph.safety_tags(target, action, occupied)
        head = {k: v for k, v in r.items() if k not in ("tags", "present")}  # 判定按全部命中算，截断只影响列出的标记
        return done(_fill(head, ("tags", [_tags([t])[0] if "nodes" in t else t for t in r["tags"]])))

    def priority_rules(context: Context, zone: str | None = None, at: str | None = None) -> ToolResult:
        check(context)
        return done(graph.priority_rules(zone, at))

    def playbooks(context: Context, event: str | None = None, target: str | None = None) -> ToolResult:
        check(context)
        items = graph.playbooks(event, target)
        return done({"knowledge_version": graph.version, "playbooks": items})

    return {"trace_impact": trace_impact, "find_sources": find_sources, "safety_tags": safety_tags,
            "priority_rules": priority_rules, "playbooks": playbooks}


# 事件类型 → find_sources 的症状；其余按“找上游供给方”处理
SYMPTOMS = {"compressor_pressure_drop": "pressure_drop", "compressed_air_low": "pressure_drop", "load_spike": "load_surge",
            "sim_limit": "overload", "capacity_shed_request": "overload", "heat_wave": "temperature_deviation"}
# 事件类型 → 预案触发事件（graph.yaml playbooks[].trigger.event），B47 按图谱预案取保护与协同处置动作
PLAYBOOK_EVENTS = {"elevator_trapped": "elevator_stuck", "compressor_pressure_drop": "pressure_drop",
                   "compressed_air_low": "pressure_drop", "load_spike": "load_surge", "sim_limit": "load_surge",
                   "capacity_shed_request": "load_surge", "power_loss": "power_loss", "hazmat_leak": "ventilation_fault"}


def router_hooks(graph: GlobalGraph) -> dict[str, Any]:
    """值守层（B27）的图谱能力，只按钩子名登记，不 import 值守层。"""

    def locate(event: Any) -> tuple[str, ...]:
        node = graph.node(event.entity_id) if event.entity_id else None
        return (node["domain"],) if node and node["domain"] != "other" else ()

    def safety_hit(entity: str) -> bool:
        """D55：对象自身带安全标记即命中；图谱查不到的设备按涉及处理。区域、电梯群、空压系统是聚合对象，不算查不清。"""
        node = graph.node(entity)
        if node is not None:
            return bool(graph.direct_tags(node["code"]))
        return not (graph.is_zone(entity) or graph.is_aggregate(entity))

    def downstream(entity: str) -> list[str]:
        return [a["code"] for a in graph.trace_impact(entity)["affected"]] if graph.node(entity) else []

    def entity_type(entity: str) -> str | None:
        node = graph.node(entity)
        return node["type"] if node else None

    def entity_zone(entity: str) -> str | None:
        """图谱节点所在区域；区域本身和查不到的实体返回 None。"""
        node = graph.node(entity)
        return node["zone"] if node else None

    def recall_entity(identifier: str) -> dict | None:
        """B27 recall(entity://…)：实体摘要与一跳邻居；图谱里没有返回 None，由召回如实提示未找到。"""
        node = graph.node(identifier)
        if node is None:
            return None
        zone = f"，区域 {node['zone']}" if node["zone"] else ""
        return {"summary": f"{node['label']}（{node['type']}/{node['domain']}）{zone}", "next": graph.neighbors(identifier)}

    def sources(entity: str, event_type: str) -> list[str]:
        node = graph.node(entity)
        if node is None:
            return []
        found = graph.find_sources(node["code"], SYMPTOMS.get(event_type, "power_loss"))["candidates"]
        return [c["code"] for c in found if c["code"] != "GRID"]

    def priority(zone: str | None, at: Any) -> dict:
        """B28 第 3 层裁决：按（区域, 时刻）取图谱优先级顺序与知识版本。"""
        found = graph.priority_rules(zone, at)
        return {"order": found["order"], "window": found.get("window"), "knowledge_version": found["knowledge_version"]}

    # B47（D53）：分级执行要按图谱预案取动作、按动作种类取后果档位、按安全标记否决
    def playbooks_for(event: Any) -> list[dict]:
        trigger = PLAYBOOK_EVENTS.get(getattr(event, "event_type", ""))
        return graph.playbooks(trigger) if trigger else []

    def action_safety(entity: str, action: str) -> dict:
        return graph.safety_tags(entity, action)

    def action_consequence(action: str) -> str:
        return graph.action_consequence(action)

    return {"router.locate": locate, "router.safety_hit": safety_hit, "router.downstream_lines": downstream,
            "router.entity_type": entity_type, "router.entity_zone": entity_zone, "router.sources": sources,
            "router.playbooks": playbooks_for, "router.action_safety": action_safety,
            "router.action_consequence": action_consequence, "router.priority": priority,
            "router.recall_entity": recall_entity}


def register(registry: Any, settings: Any) -> None:
    """global_graph 包入口：按 settings.graph.config（默认工厂装配）建图并登记五个工具。"""
    cfg = (settings.section("graph") if settings is not None else None) or {}
    graph = GlobalGraph.load(Path(cfg.get("config") or DEFAULT_CONFIG))
    handlers = make_tools(graph)
    for name in TOOL_NAMES:
        registry.register_tool(name, handlers[name])
    for name, hook in router_hooks(graph).items():
        registry.register_hook(name, hook)
