"""B43 挖掘：处置执行记录 + B32 成功反馈 → 重复成功流程（Pattern）。

动作签名只保留每张工单按 action_index 排序后的 (entity_type, command) 序列，具体 entity_id
与数值不进签名；同组成功工单数达到阈值且窗口内没有系统失败才产出 pattern。数据性质标在
记录上：自带 fixtures/executions.json 是 synthetic（演示用合成处置记录）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUCCESS_OUTCOMES = frozenset({"approved", "accepted", "correct"})
FAILURE_OUTCOMES = frozenset({"rejected", "wrong", "corrected"})
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "executions.json"


@dataclass(frozen=True)
class Pattern:
    """一组动作签名完全相同的重复成功处置流程；证据只列成功工单。"""

    pattern_id: str
    event_type: str
    signature: tuple[tuple[str, str], ...]
    steps: tuple[dict[str, Any], ...]
    source_tickets: tuple[str, ...]
    success_count: int
    run_id: str | None
    evidence: tuple[dict[str, Any], ...]
    nature: str = "synthetic"
    coverage_gap: bool = False  # True：没有 B47 真实执行记录，只能用合成 fixtures 挖掘


def load_execution_records(path: Path | None = None) -> list[dict[str, Any]]:
    """读取处置执行记录；缺省读本包 fixtures/executions.json 的 records（合成数据）。"""
    source = FIXTURE_PATH if path is None else Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    records = data.get("records") if isinstance(data, Mapping) else data
    items = [dict(record) for record in (records or ())]
    if path is None:  # 自带的演示记录全部是合成数据
        for item in items:
            item["nature"] = "synthetic"
    return items


EXECUTION_RECORDS_HOOK = "b47.execution_records"


def execution_records(registry: Any, *, run_id: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    """挖掘输入：优先取 B47 登记的 b47.execution_records 钩子（真实执行记录）；
    钩子缺失或取不到记录时回退 fixtures，并返回 coverage_gap=True，挖出的 Pattern 标 synthetic。"""
    hooks = registry.hooks_for(EXECUTION_RECORDS_HOOK) if registry is not None else ()
    if hooks:
        try:
            raw = hooks[-1](run_id=run_id) or ()
        except Exception:  # noqa: BLE001 — 钩子故障按缺失处理，仍回退 fixtures
            raw = ()
        records = [_record_dict(item) for item in raw]
        if records:
            return records, False
    return load_execution_records(), True


def _record_dict(item: Any) -> dict[str, Any]:
    data = dict(item.model_dump(mode="json") if hasattr(item, "model_dump") else item)
    data.setdefault("nature", "derived")
    return data


def merge_feedback(records: Sequence[Mapping[str, Any]],
                   feedback_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 ticket_id 用 B32 反馈行的 outcome/system_error 覆盖记录值；不改入参。"""
    overrides: dict[str, Mapping[str, Any]] = {}
    for row in feedback_rows:
        ticket_id = str(row.get("ticket_id") or "").strip()
        if ticket_id and ticket_id not in overrides:
            overrides[ticket_id] = row
    merged: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        row = overrides.get(str(item.get("ticket_id") or "").strip())
        if row is not None:
            item["outcome"] = row.get("outcome", item.get("outcome"))
            item["system_error"] = row.get("system_error", item.get("system_error"))
        merged.append(item)
    return merged


def collect_tickets(records: Sequence[Mapping[str, Any]],
                    event_type: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """执行记录按 (event_type, ticket_id) 归并：丢弃缺字段的行，收集动作、结果、运行与数据性质。挖掘与 evidence 核对共用。"""
    tickets: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        ticket_id = str(record.get("ticket_id") or "").strip()
        record_event = str(record.get("event_type") or "").strip()
        entity_type = str(record.get("entity_type") or "").strip()
        command = str(record.get("command") or "").strip()
        if not ticket_id or not record_event or not entity_type or not command:
            continue
        if event_type is not None and record_event != event_type:
            continue
        ticket = tickets.setdefault((record_event, ticket_id),
                                    {"actions": [], "outcomes": [], "run_ids": [], "natures": set()})
        ticket["natures"].add(str(record.get("nature") or "synthetic"))
        ticket["actions"].append({"order": int(record.get("action_index") or 0),
                                  "entity_type": entity_type, "command": command,
                                  "entity_id": str(record.get("entity_id") or "")})
        ticket["outcomes"].append((str(record.get("outcome") or "").strip().lower(),
                                   bool(record.get("system_error"))))
        run_id = str(record.get("run_id") or "").strip()
        if run_id and run_id not in ticket["run_ids"]:
            ticket["run_ids"].append(run_id)
    return tickets


def ticket_signatures(records: Sequence[Mapping[str, Any]]) -> dict[str, tuple[tuple[str, str], ...]]:
    """ticket_id → 按 action_index 排序的 (entity_type, command) 序列，与挖掘用的动作签名同一口径。

    同一工单出现在多个 event_type 下时按 ticket_id 合并所有动作（不互相覆盖）。"""
    merged: dict[str, list[dict[str, Any]]] = {}
    for (_event, ticket_id), ticket in collect_tickets(records).items():
        merged.setdefault(ticket_id, []).extend(ticket["actions"])
    return {ticket_id: tuple((action["entity_type"], action["command"])
                             for action in sorted(actions, key=lambda action: action["order"]))
            for ticket_id, actions in merged.items()}


def mine_patterns(records: Sequence[Mapping[str, Any]], *, min_successes: int = 3,
                  event_type: str | None = None) -> list[Pattern]:
    """按 (event_type, 动作签名) 分组挖重复成功流程；同组有系统失败工单则整组不产出。

    成功行 outcome ∈ approved/accepted/correct；失败行 ∈ rejected/wrong/corrected 且
    system_error 为真。失败工单不只过滤掉它自己，还让同签名组整体不出（不做“部分成功”）。
    Pattern.run_id 只在整组成功工单同属一次运行时才有值，跨运行时留空。
    """
    tickets = collect_tickets(records, event_type)

    groups: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    for (record_event, ticket_id), ticket in tickets.items():
        actions = sorted(ticket["actions"], key=lambda action: action["order"])
        signature = tuple((action["entity_type"], action["command"]) for action in actions)
        group = groups.setdefault((record_event, signature), {"successes": [], "failed": False, "natures": set()})
        if any(outcome in FAILURE_OUTCOMES and error for outcome, error in ticket["outcomes"]):
            group["failed"] = True
        elif any(outcome in SUCCESS_OUTCOMES for outcome, _ in ticket["outcomes"]):
            group["successes"].append((ticket_id, actions, ticket["run_ids"]))
            group["natures"].update(ticket["natures"])

    patterns: list[Pattern] = []
    for (group_event, signature), group in sorted(groups.items()):
        successes = sorted(group["successes"], key=lambda item: item[0])
        if group["failed"] or len(successes) < min_successes:
            continue
        steps = tuple(_step(index + 1, entity_type, command, _example_entity(successes, index))
                      for index, (entity_type, command) in enumerate(signature))
        run_ids = sorted({run_id for _ticket_id, _actions, runs in successes for run_id in runs})
        patterns.append(Pattern(
            pattern_id="b43-pattern-" + hashlib.sha1(
                _signature_key(group_event, signature).encode("utf-8")).hexdigest()[:12],
            event_type=group_event,
            signature=signature,
            steps=steps,
            source_tickets=tuple(ticket_id for ticket_id, _actions, _runs in successes),
            success_count=len(successes),
            run_id=run_ids[0] if len(run_ids) == 1 else None,
            evidence=tuple({"ticket_id": ticket_id,
                            "actions": [[action["entity_type"], action["command"]] for action in actions]}
                           for ticket_id, actions, _runs in successes),
            nature="synthetic" if "synthetic" in group["natures"] else sorted(group["natures"])[0],
        ))
    return patterns


def _step(order: int, entity_type: str, command: str, example_entity_id: str) -> dict[str, Any]:
    text = f"对 {entity_type} 执行 {command}"
    if example_entity_id:
        text += f"（示例 {example_entity_id}）"
    return {"order": order, "entity_type": entity_type, "command": command,
            "example_entity_id": example_entity_id, "text": text}


def _example_entity(successes: Sequence[tuple[str, list[dict[str, Any]], list[str]]],
                    index: int) -> str:
    for _ticket_id, actions, _run_ids in successes:
        if index < len(actions) and actions[index]["entity_id"]:
            return actions[index]["entity_id"]
    return ""


def _signature_key(event_type: str, signature: Sequence[Sequence[str]]) -> str:
    return event_type + "|" + "|".join(f"{entity_type}/{command}" for entity_type, command in signature)
