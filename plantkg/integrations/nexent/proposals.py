"""专家提案的统一包装（03 §3.0 提案以落库为准）。

各专家分支登记 `<域>_propose` 的业务函数，签名 handler(context, binding, **params) -> ExpertProposal（proposal_id 为空的草稿）。
本模块把它包成注册表里的工具：按本次运行的绑定（事件单、轮次、触发时刻）调用业务函数 → 经 ProposalSink 落库分配 proposal_id
→ 对模型只返回 {proposal_id, summary}，数值字段不经过模型转述。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from typing import Any

from plantkg.core.contracts import (
    AgentTiming,
    BusinessStatus,
    Context,
    ErrorCode,
    ExpertProposal,
    ExpertReply,
    PlantkgError,
    ProposalCost,
    ProposalReceipt,
    RunBinding,
    TOOL_SPECS,
    ToolResult,
    tool_error,
)
from plantkg.core.registry import Registry, ToolBinding

EXPERT_DOMAINS = ("energy", "elevator", "hvac", "equipment")
PROPOSE_TOOLS = {f"{d}_propose" for d in EXPERT_DOMAINS}
# 业务函数登记处：专家分支调 registry.register_hook("expert_propose:<域>", handler)；没登记时用本卡的合成占位
HOOK_PREFIX = "expert_propose:"

BindingLookup = Callable[[Context], tuple[RunBinding, AgentTiming | None] | None]


class MemoryProposalSink:
    """ProposalSink 替身（dev/test/mock）：B27 实现登记后替换。production 不装，缺实现即启动失败。"""

    def __init__(self) -> None:
        self._items: dict[str, ExpertProposal] = {}
        self._lock = threading.Lock()

    def save(self, proposal: ExpertProposal, binding: RunBinding) -> str:
        proposal_id = "proposal://pp-" + secrets.token_hex(6)
        with self._lock:
            self._items[proposal_id] = proposal.model_copy(update={"proposal_id": proposal_id, "binding": binding})
        return proposal_id

    def attach_reply(self, proposal_id: str, reply: ExpertReply) -> ExpertProposal:
        with self._lock:
            stored = self.get(proposal_id)
            updated = stored.model_copy(update={"diagnosis": reply.diagnosis or stored.diagnosis,
                                                "evidence_refs": stored.evidence_refs + tuple(reply.evidence_refs)})
            self._items[proposal_id] = updated
        return updated

    def get(self, proposal_id: str) -> ExpertProposal:
        try:
            return self._items[proposal_id]
        except KeyError:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"提案 {proposal_id} 不存在") from None

    def list(self, ticket_id: str, round_: int | None = None) -> list[ExpertProposal]:
        with self._lock:
            items = list(self._items.values())
        return [p for p in items if p.ticket_id == ticket_id
                and (round_ is None or (p.binding is not None and p.binding.round == round_))]


def placeholder_propose(domain: str) -> Callable[..., ExpertProposal]:
    """合成占位：不给动作，只说明“占位专家已收到事件单”。数据性质 synthetic，B19～B22 交付后替换。"""
    def handler(context: Context, binding: RunBinding, focus: str | None = None) -> ExpertProposal:
        return ExpertProposal(expert=domain, ticket_id=binding.ticket_id, actions=(), cost=ProposalCost(cny=0.0), confidence=0.5,
                              diagnosis=f"[synthetic] {domain} 占位专家：已收到事件单{('，关注 ' + focus) if focus else ''}；真实提案由对应专家分支交付")
    return handler


def summarize(proposal_id: str, proposal: ExpertProposal) -> str:
    return (f"{proposal_id}：{len(proposal.actions)} 个动作，代价 {proposal.cost.cny:.1f} 元，"
            f"置信度 {proposal.confidence:.2f}")[:500]


def _existing(sink: Any, binding: RunBinding, domain: str) -> str | None:
    """本事件单本轮该专家已有的提案：预演已落库或模型重复调用时复用它，不再生成第二份。"""
    for proposal in sink.list(binding.ticket_id):
        if proposal.proposal_id and proposal.expert == domain and proposal.binding is not None \
                and proposal.binding.round == binding.round:
            return proposal.proposal_id
    return None


def wrap(domain: str, inner: Callable[..., ExpertProposal], sink_for: Callable[[], Any], binding_for: BindingLookup,
         on_saved: Callable[[Context, str], None]) -> Callable[..., ToolResult]:
    done: dict[str, ToolResult] = {}  # 一次运行只落一份提案，模型重复调用返回同一份

    def tool(context: Context, **params: Any) -> ToolResult:
        run_id = getattr(context, "agent_run_id", None)
        if run_id in done:
            return done[run_id]
        found = binding_for(context)
        if found is None:
            return tool_error(ErrorCode.PRECONDITION, "专家提案只能在事件单会诊里调用，这次是问答：不要重试，直接用知识库和其他工具回答")
        binding, timing = found
        sink = sink_for()
        existing = _existing(sink, binding, domain)
        if existing is not None:  # 同轮同专家只保留一份：预演缓存或重复调用都复用已有提案
            on_saved(context, existing)
            receipt = ProposalReceipt(proposal_id=existing, summary=summarize(existing, sink.get(existing)))
            result = ToolResult(ok=True, business_status=BusinessStatus.done, data=receipt.model_dump(mode="json"),
                                run_id=binding.run_id)
            if run_id is not None:
                done[run_id] = result
            return result
        draft = inner(context, binding, **params)
        if not isinstance(draft, ExpertProposal):
            raise PlantkgError(ErrorCode.INTERNAL, f"{domain}_propose 的业务函数必须返回 ExpertProposal 草稿")
        proposal = draft.model_copy(update={"proposal_id": None, "expert": domain, "ticket_id": binding.ticket_id,
                                            "binding": binding, "timing": timing})
        proposal_id = sink.save(ExpertProposal.model_validate(proposal.model_dump()), binding)
        on_saved(context, proposal_id)
        receipt = ProposalReceipt(proposal_id=proposal_id, summary=summarize(proposal_id, proposal))
        result = ToolResult(ok=True, business_status=BusinessStatus.done, data=receipt.model_dump(mode="json"), run_id=binding.run_id)
        if run_id is not None:
            done[run_id] = result
        return result
    return tool


def install(registry: Registry, sink_for: Callable[[], Any], binding_for: BindingLookup, on_saved: Callable[[Context, str], None],
            *, placeholders: bool) -> None:
    """在领域包加载之后调用：给四个专家的 propose 工具登记统一包装；业务函数取各专家分支登记的钩子，
    没有时 placeholders=True（mock/dev）用合成占位，否则不登记该工具（prod/evaluation 不装 Fake）。"""
    specs = {s.name for s in TOOL_SPECS}
    for domain in EXPERT_DOMAINS:
        name = f"{domain}_propose"
        assert name in specs, name
        existing = registry.tools.get(name)
        if existing is not None and name not in registry.fallbacks:
            # 专家分支直接登记了工具：它的处理函数就是业务函数，原地换成包装后的版本
            registry.tools[name] = ToolBinding(existing.spec, wrap(domain, existing.handler, sink_for, binding_for, on_saved))
            continue
        handlers = registry.hooks_for(HOOK_PREFIX + domain)
        if not handlers and not placeholders:
            continue
        inner = handlers[-1] if handlers else placeholder_propose(domain)
        registry.register_tool(name, wrap(domain, inner, sink_for, binding_for, on_saved), fallback=not handlers)
