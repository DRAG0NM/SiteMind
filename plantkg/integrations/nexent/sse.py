"""Nexent 北向 SSE（v2.8.2 实测形态）的解析与终态判定（contracts/events/nexent-sse-mapping.md）。

只从 SSE 取回答文本、子 Agent 节点与“正在调用”提示；工具调用事实以 plantkg 自己的调用记录为准。
子 Agent（depth>=1）的思考、工具返回与回答都在同一条流里（P21e）：记进节点，不发给用户，只让主 Agent 对人说话。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TIMEOUT_MARK = "MCP 工具调用超时"
ERROR_PREFIX = "Run Agent Error:"


def parse_lines(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """把 SSE 文本行解析成事件字典；只认 data: 行，非 JSON 的 data 跳过。"""
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@dataclass
class Node:
    node_id: str
    agent_name: str
    depth: int
    parent_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    answer: list[str] = field(default_factory=list)
    complete: str | None = None
    step: tuple[Any, Any] | None = None  # 当前步的 (step_id, attempt_id)；平台可能把这一步的输出改判成正文
    step_text: list[str] = field(default_factory=list)

    @property
    def final_answer(self) -> str | None:
        text = self.complete if self.complete else "".join(self.answer)
        return text or None


@dataclass
class RunState:
    """一次北向运行的累积状态。feed 返回要发给用户的回答增量（只来自 depth 0）。"""

    root_agent: str
    conversation_id: str | None = None
    nodes: dict[str, Node] = field(default_factory=dict)
    finished: bool = False  # 收到 depth 0 的 complete
    complete_failed: bool = False  # complete 的正文本身是错误或超时文本
    error: str | None = None
    timed_out: bool = False

    def __post_init__(self) -> None:
        self.nodes["root"] = Node("root", self.root_agent, 0)

    @property
    def root(self) -> Node:
        return self.nodes["root"]

    def feed(self, event: dict[str, Any], now: datetime) -> str | None:
        kind = event.get("type")
        content = event.get("content")
        depth = event.get("depth") or 0
        if kind == "conversation_created" and isinstance(content, dict):
            if content.get("conversation_id") is not None:
                self.conversation_id = str(content["conversation_id"])
            return None
        if self.root.started_at is None:
            self.root.started_at = now
        if kind in ("subagent_start", "subagent_end"):
            info = _json(content)
            node_id = str(event.get("invocation_id") or info.get("invocation_id") or f"sub-{len(self.nodes)}")
            node = self.nodes.get(node_id)
            if node is None:
                name = info.get("invocation_name") or event.get("agent_name") or "unknown"
                node = self.nodes[node_id] = Node(node_id, str(name), depth or 1, parent_id="root")
            if kind == "subagent_start":
                node.started_at = now
            else:
                node.finished_at = now
            return None
        if kind == "error":
            self.error = str(content or "Nexent 返回 error 事件")
            return None
        if kind == "model_output_thinking":
            node = self.nodes.get(str(event.get("invocation_id"))) if depth else self.root
            if node is not None:
                step = (event.get("step_id"), event.get("attempt_id"))
                if step != node.step:
                    node.step, node.step_text = step, []
                node.step_text.append("" if content is None else str(content))
            return None
        if kind != "final_answer":
            return None
        text = "" if content is None else str(content)
        node = self.nodes.get(str(event.get("invocation_id"))) if depth else self.root
        if node is None:  # 没见到 subagent_start 的子 Agent 回答：按 invocation_id 补一个节点
            node = self.nodes[str(event.get("invocation_id"))] = Node(str(event.get("invocation_id")), str(event.get("agent_name") or "unknown"),
                                                                       depth, parent_id="root", started_at=now)
        phase = event.get("answer_phase")
        if phase == "complete":
            reclassified = (not text and event.get("reclassify_step") and not node.answer
                            and node.step is not None and node.step[0] == event.get("step_id"))
            if reclassified:  # 最后一步被改判：正文走了思考流，complete 是空的
                text = "".join(node.step_text)
                if "<completed_action_history" in text:  # 模型照抄平台上下文后停住，这不是回答
                    text, reclassified = "", False
            node.complete = text or None
            if depth == 0:
                self.finished = True
                self.root.finished_at = now
                self._classify(node.final_answer or "")
            return text if reclassified and not depth else None
        node.answer.append(text)
        if depth:
            return None
        return text

    def _classify(self, answer: str) -> None:
        if answer.strip().startswith(ERROR_PREFIX):
            self.error = answer.strip()
            self.complete_failed = True
        elif TIMEOUT_MARK in answer and len(answer) < 200:
            self.timed_out = True
            self.complete_failed = True


def _json(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    try:
        value = json.loads(content) if isinstance(content, str) else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def terminal(state: RunState, *, cancelled: bool, deadline_hit: bool, recovered: str | None) -> tuple[str, str, str | None]:
    """返回 (UIEvent 类型, terminal_reason, 失败消息)。recovered 是断流后从会话历史补回的回答。

    depth 0 的 complete 已到即信任该回答：之后补发的 error 事件、断流与截止都不再改写终态。
    只有 complete 正文本身判为错误或超时（complete_failed）时仍按失败返回。
    """
    if state.finished and not state.complete_failed:
        return "run_finished", "completed" if state.root.complete else "completed_from_deltas", None
    if cancelled:
        return "run_cancelled", "cancelled", None
    if deadline_hit or state.timed_out:
        return "run_failed", "timeout", "运行超过截止时间" if deadline_hit else (state.root.final_answer or "MCP 工具调用超时")
    if state.error:
        return "run_failed", "error", state.error[:500]
    if state.finished:
        return "run_finished", "completed" if state.root.complete else "completed_from_deltas", None
    if recovered:
        return "run_finished", "stream_lost_recovered", None
    return "run_failed", "stream_lost", "连接断开且会话历史里没有结局"
