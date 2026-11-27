"""专家最终回答的解析与过期判定（B29）。

专家回答只用于补诊断与依据：`{status, proposal_id, diagnosis, evidence_refs, question}`。解析失败只少诊断，
提案照常进入裁决；绑定与当前轮不符的回答标过期，只入历史，不回写提案。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from plantkg.core.contracts import ExpertProposal, ExpertReply, RunBinding

REPLY_STATUSES = frozenset({"done", "waiting", "blocked", "needs_clarification"})


def last_reply_object(text: str) -> dict | None:
    """取回答里最后一个带 status 的 JSON 对象：容忍代码块围栏、前后说明与重复作答，附带的小对象不顶替结论。"""
    decoder, found, at = json.JSONDecoder(), None, text.find("{")
    while at >= 0:
        try:
            item, end = decoder.raw_decode(text, at)
        except json.JSONDecodeError:
            end = at + 1
        else:
            if isinstance(item, dict) and "status" in item:
                found = item
        at = text.find("{", end)
    return found


def parse_reply(result: Mapping[str, object]) -> ExpertReply:
    """live 来源的返回 → ExpertReply。运行未完成或回答不符合契约时返回 parse_failed，不抛异常。"""
    if result.get("status") != "finished":
        return ExpertReply(status="parse_failed")
    item = last_reply_object(str(result.get("final_answer") or ""))
    if item is None:
        return ExpertReply(status="parse_failed")
    status = str(item.get("status") or "")
    if status not in REPLY_STATUSES:
        return ExpertReply(status="parse_failed")
    proposal_id = item.get("proposal_id")
    diagnosis = item.get("diagnosis")
    question = item.get("question")
    refs = item.get("evidence_refs")
    try:
        return ExpertReply(
            status=status,
            proposal_id=str(proposal_id) if proposal_id else None,
            diagnosis=str(diagnosis) if isinstance(diagnosis, str) and diagnosis.strip() else None,
            evidence_refs=tuple(str(r) for r in refs) if isinstance(refs, list) else (),
            question=str(question) if isinstance(question, str) and question.strip() else None,
        )
    except ValueError:  # needs_clarification 却没给问题
        return ExpertReply(status="parse_failed")


def stale_reason(proposal: ExpertProposal, binding: RunBinding) -> str | None:
    """提案绑定的运行、轮次或知识版本与当前回答不一致时返回原因；一致返回 None。"""
    bound = proposal.binding
    if proposal.ticket_id != binding.ticket_id:
        return f"提案属于事件单 {proposal.ticket_id}"
    if bound is None:
        return "提案没有运行绑定"
    if bound.run_id != binding.run_id:
        return f"提案来自运行 {bound.run_id}，当前运行是 {binding.run_id}"
    if bound.round != binding.round:
        return f"提案是第 {bound.round} 轮，当前是第 {binding.round} 轮"
    if bound.knowledge_version != binding.knowledge_version:
        return f"提案的知识版本 v{bound.knowledge_version}，当前是 v{binding.knowledge_version}"
    return None
