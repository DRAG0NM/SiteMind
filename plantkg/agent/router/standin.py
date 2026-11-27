"""会诊（B29）的最简替身：同步跑各领域的确定性专家，数据 synthetic。

正式实现由 B29 经 registry.register_hook(EXPERTS_HOOK) 登记，本卡不 import 下游；
没有登记时用本替身把“事件单 → 专家提案 → 提案就绪”跑通，便于演示与冒烟测试。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from plantkg.core.contracts import DayEvent, ExpertProposal, ExpertReply, RunBinding, Ticket


class SyntheticConsilium:
    """experts：领域 → f(event, binding) -> ExpertProposal 草稿。提案经 ProposalSink 落库，回答经 attach_reply 挂上。"""

    def __init__(self, experts: Mapping[str, Callable[[DayEvent, RunBinding], ExpertProposal]]) -> None:
        self.experts = dict(experts)
        self.calls: list[tuple[str, RunBinding]] = []

    def __call__(self, router, ticket: Ticket, binding: RunBinding) -> None:
        self.calls.append((ticket.ticket_id, binding))
        event = router.store.event(ticket.event_ids[-1])
        if event is None:
            router.complete_round(ticket.ticket_id, binding.round)
            return
        for domain in ticket.experts:
            expert = self.experts.get(domain)
            if expert is None:
                continue
            draft = expert(event["event"], binding)
            proposal_id = router.sink.save(draft, binding)
            reply = ExpertReply(status="done", diagnosis=draft.diagnosis, evidence_refs=draft.evidence_refs)
            router.store.set_ticket_state(ticket.ticket_id, f"reply_status_{binding.round}_{proposal_id}", reply.status)
            router.store.set_ticket_state(ticket.ticket_id, f"reply_diagnosis_{binding.round}_{proposal_id}", reply.diagnosis or "")
            router.sink.attach_reply(proposal_id, reply)
        router.complete_round(ticket.ticket_id, binding.round)
