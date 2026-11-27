"""通用召回工具 plantkg_recall(id)（B27）：按 ID 前缀展开一跳。

注入与返回统一为“对象 ID + 一行摘要 + 下一跳 ID”。单次返回不超过 8KB：装不下就截断，并把没装下的
对象 ID 放进 next 当续读入口，不整条失败。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from plantkg.core.contracts import Context, ErrorCode, PlantkgError, ToolResult

SCHEMES = ("event", "ticket", "proposal", "entity", "asset")


def _entry(identifier: str, summary: str, *, next_: Iterable[str] = ()) -> dict[str, Any]:
    return {"id": identifier, "summary": summary[:500], "next": list(next_)[:20]}


def _fits(data: dict[str, Any]) -> bool:
    try:
        ToolResult(ok=True, business_status="done", data=data).to_agent_text()
    except PlantkgError:
        return False
    return True


class Recall:
    """一跳召回：事件 → 事件单、事件单 → 提案与事件、提案 → 事件单与依据、实体 → 订阅它的事件单。"""

    def __init__(self, store: Any, sink: Any, *, plant_id: str, entity_lookup: Callable[[str], dict | None] | None = None,
                 asset_lookup: Callable[[str], dict | None] | None = None) -> None:
        self.store, self.sink, self.plant_id = store, sink, plant_id
        self.entity_lookup, self.asset_lookup = entity_lookup, asset_lookup

    def __call__(self, context: Context, id: str) -> ToolResult:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "召回对象不属于本厂区")
        identifier = (id or "").strip()
        if not identifier:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "recall 需要对象 ID")
        scheme, sep, bare = identifier.partition("://")
        if not sep or scheme not in SCHEMES or not bare:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知 ID 前缀 {identifier!r}；可用 {'、'.join(s + '://' for s in SCHEMES)}")
        head, items = getattr(self, f"_{scheme}")(bare, identifier)
        data: dict[str, Any] = {"id": identifier, "kind": scheme, **head, "items_total": len(items), "items": [], "truncated": False}
        for item in items:
            if not _fits({**data, "items": data["items"] + [item]}):
                data["truncated"] = True
                break
            data["items"].append(item)
        while data["truncated"] and data["items"]:  # 续读入口本身也要装得下
            data["next"] = [i["id"] for i in items[len(data["items"]):]][:10]
            if _fits(data):
                break
            data["items"].pop()
        if data["truncated"]:
            data["next"] = [i["id"] for i in items[len(data["items"]):]][:10]
        return ToolResult(ok=True, business_status="done", data=data)

    # ---- 各前缀
    def _event(self, event_id: str, identifier: str) -> tuple[dict, list[dict]]:
        found = self.store.event(event_id) or self.store.event(identifier)
        if found is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件 {identifier} 不存在")
        event, result, ticket_id = found["event"], found["result"], found["ticket_id"]
        head = {"summary": f"{event.event_type}（严重度 {event.severity}，准入 {result}）：{event.summary}"[:500],
                "at": event.at.isoformat(), "entity_id": event.entity_id}
        items = [_entry(ticket_id, f"事件单，事件 {event.event_id} 已并入", next_=[ticket_id])] if ticket_id else []
        return head, items

    def _ticket(self, ticket_id: str, identifier: str) -> tuple[dict, list[dict]]:
        ticket = self.store.ticket(identifier)
        if ticket is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"事件单 {identifier} 不存在")
        head = {"summary": f"{ticket.status} 第 {ticket.round} 轮，专家 {','.join(ticket.experts) or '无'}：{ticket.summary}"[:500],
                "status": ticket.status, "round": ticket.round, "opened_at": ticket.opened_at.isoformat()}
        items = [_entry(p.proposal_id, f"{p.expert} 提案，置信度 {p.confidence:.2f}，{len(p.actions)} 个动作",
                        next_=p.evidence_refs) for p in self.sink.list(identifier) if p.proposal_id]
        items += [_entry(f"event://{eid}", "本事件单收到的事件", next_=[identifier]) for eid, _ in self.store.events_of(identifier)]
        items += [_entry(sub.entity_id, f"订阅事件类型 {','.join(sub.event_types)}", next_=[sub.entity_id])
                  for sub in ticket.subscriptions]
        return head, items

    def _proposal(self, proposal_id: str, identifier: str) -> tuple[dict, list[dict]]:
        proposal = self.sink.get(identifier)
        head = {"summary": f"{proposal.expert} 提案，置信度 {proposal.confidence:.2f}：{proposal.diagnosis or '无诊断'}"[:500],
                "ticket_id": proposal.ticket_id,
                "actions": [f"{a.command} {a.entity_id} {a.interval.start.isoformat()}" for a in proposal.actions][:10]}
        items = [_entry(proposal.ticket_id, "所属事件单", next_=[proposal.ticket_id])]
        items += [_entry(ref, "依据") for ref in proposal.evidence_refs]
        return head, items

    def _entity(self, entity_id: str, identifier: str) -> tuple[dict, list[dict]]:
        items = [_entry(t, "订阅该实体事件流的事件单", next_=[t]) for t in self.store.tickets_for_entity(identifier)]
        if self.entity_lookup is None:
            return {"summary": f"图谱未接入：实体 {identifier}，只列订阅它的事件单"}, items
        known = self.entity_lookup(identifier)
        if known is None:
            return {"summary": f"图谱里没有实体 {identifier}，只列订阅它的事件单"}, items
        head = {"summary": str(known.get("summary") or f"图谱实体 {identifier}")[:500]}
        items += [_entry(n, "一跳邻居") for n in known.get("next", [])]
        return head, items

    def _asset(self, asset_id: str, identifier: str) -> tuple[dict, list[dict]]:
        if self.asset_lookup is None:
            return {"summary": f"资料 {identifier}：资料目录未接入（B06 未启用存储），只记引用"}, []
        known = self.asset_lookup(identifier)
        if known is None:
            return {"summary": f"资料 {identifier}：资产目录里没有这条资料（B06），只记引用"}, []
        return {"summary": str(known.get("summary") or identifier)[:500]}, [_entry(n, "关联对象") for n in known.get("next", [])]
