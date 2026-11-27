"""会诊的确定性结果来源（B29）。

`expert`：只用确定性提案、不产生回答。先找该专家自己的确定性结果源（场景注册表里按专家名登记，能带运行简报），
没有再调专家包登记的 `expert_propose:<域>` 业务函数；两种来源都只返回草稿，由会诊统一落库。
`follow_up`：没有模型可问时跳过追问，返回明确的空结果。

trial/evaluation 模式经注册表回退到这里，提案、裁决、分级执行与账本逻辑不变。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plantkg.core.contracts import (
    Actor,
    AgentResultKind,
    Context,
    ErrorCode,
    ExpertProposal,
    PlantkgError,
    RunBinding,
)
from plantkg.core.ports import AgentResultSource

EXPERT_KEY = "expert"  # 与 AgentResultKind.expert、bootstrap.MODE_REQUIREMENTS 一致
FOLLOW_UP_KEY = "follow_up"


class DeterministicExpertSource:
    """AgentResultSource（deterministic，键 expert）：payload["expert"] 选专家，返回 {"proposal": 草稿}。"""

    name = "b29-expert"
    source_kind = "deterministic"

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if AgentResultKind(kind) != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"会诊的确定性专家只实现 expert，不实现 {kind}")
        expert = str(payload.get("expert") or "")
        if not expert:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "会诊的确定性专家需要 payload.expert")
        delegated = self.registry.mode_ports.get((AgentResultSource, "deterministic", expert))
        if delegated is not None and delegated is not self:
            result = dict(delegated.get(AgentResultKind.expert, binding, payload))
            if result.get("proposal") is None:
                raise PlantkgError(ErrorCode.INTERNAL, f"专家 {expert} 的确定性结果源没有给出提案")
            return {"status": "finished", "expert": expert, "source": "deterministic", "proposal": result["proposal"]}
        handlers = self.registry.hooks_for(f"expert_propose:{expert}")
        if not handlers:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"专家 {expert} 没有确定性提案实现，也没有可回退的结果源")
        context = payload.get("context") or _context(binding, payload)
        draft = handlers[-1](context, binding, focus=payload.get("focus"))
        if not isinstance(draft, ExpertProposal):
            raise PlantkgError(ErrorCode.INTERNAL, f"expert_propose:{expert} 必须返回 ExpertProposal 草稿")
        return {"status": "finished", "expert": expert, "source": "deterministic", "proposal": draft.model_dump(mode="json")}


class SkipFollowUp:
    """AgentResultSource（deterministic，键 follow_up）：没有模型时跳过追问，不伪造回答。"""

    name = "b29-follow-up"
    source_kind = "deterministic"

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if AgentResultKind(kind) != AgentResultKind.follow_up:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"会诊的确定性追问只实现 follow_up，不实现 {kind}")
        return {"status": "finished", "skipped": True, "final_answer": ""}


def install_sources(registry: Any) -> None:
    """按（端口, deterministic, 键）登记 expert 与 follow_up；已登记的不覆盖（B33 缓存等可先占位）。"""
    for key, impl in ((EXPERT_KEY, DeterministicExpertSource(registry)), (FOLLOW_UP_KEY, SkipFollowUp())):
        if (AgentResultSource, "deterministic", key) not in registry.mode_ports:
            registry.register_mode(AgentResultSource, "deterministic", impl, key=key)


def _context(binding: RunBinding, payload: Mapping[str, Any]) -> Context:
    """没接入网关时给 propose 业务函数的离线上下文：身份是值守服务，只能提议。"""
    return Context(plant_id=str(payload.get("plant_id") or "plantkg"), industry_pack="manufacturing",
                   actor=Actor(actor_id="plantkg-consultation", roles=("viewer",)), channel="offline",
                   run_id=binding.run_id, trace_id=f"consult-{binding.ticket_id.rsplit('/', 1)[-1]}",
                   as_of=binding.trigger_at)
