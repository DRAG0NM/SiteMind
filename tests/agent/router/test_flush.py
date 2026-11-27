"""B27 合批（flush）：live 主 Agent 汇报失败时降级确定性研判、pending 取走、记录实际终态。"""

from __future__ import annotations

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.core.ports import AgentResultSource
from tests.agent.router.helpers import AIR, T0, make_event


class FailedTriage:
    """live 研判返回失败终态（不是抛异常）。"""

    def get(self, kind, binding, payload):
        return {"status": "failed", "terminal_reason": "error", "final_answer": None, "latency_ms": 42}


class RaisingTriage:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get(self, kind, binding, payload):
        raise self.exc


def test_flush_failed_live_triage_degrades_and_takes_pending(env):
    env.router.mode = "live"
    env.registry.mode_ports[(AgentResultSource, "live", "triage")] = FailedTriage()
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    env.store.add_pending("pend-flush", at=T0, kind="report", ticket_id=ticket_id, record={"summary": "压力异常", "round": 1})

    batch = env.router.flush()

    assert batch is not None and batch.coverage_gap is True and batch.source == "deterministic"
    assert batch.failure and "failed" in batch.failure
    assert "压力异常" in batch.final_answer
    assert env.store.pending() == []  # 裁决：失败时 pending 取走
    record = env.router.runs[-1]
    assert record.source == "deterministic" and record.coverage_gap is True
    assert record.run_mode == "deterministic"  # 不再出现 live+deterministic 的自相矛盾
    assert record.status == "failed" and record.terminal_reason == "error"


def test_flush_precondition_does_not_break_handle_event_or_step(env):
    env.router.mode = "live"
    env.registry.mode_ports[(AgentResultSource, "live", "triage")] = RaisingTriage(
        PlantkgError(ErrorCode.PRECONDITION, "运行存储未就绪"))
    env.store.add_pending("pend-event", at=T0, kind="report", ticket_id="ticket://TK-0001",
                          record={"summary": "首次汇报", "round": 1})

    handled = env.router.handle_event(make_event("EV-I", 0, "operator_instruction", AIR))
    assert handled.batch is not None and handled.batch.coverage_gap is True
    assert handled.batch.source == "deterministic" and "运行存储未就绪" in (handled.batch.failure or "")

    env.store.add_pending("pend-step", at=T0, kind="report", ticket_id="ticket://TK-0001",
                          record={"summary": "推进步汇报", "round": 1})
    env.router.step(T0)  # 不抛：值守循环继续
    assert env.store.pending() == [] and "推进步汇报" in (env.router.runs[-1].final_answer or "")


def test_pending_order_is_reproducible_and_safety_first_within_minute(env):
    """#262：同一分钟的待汇报事项按（安全类优先、开单时刻、事件单号、种类）排序，编号由内容派生。"""
    from plantkg.agent.router.router import Router
    from plantkg.agent.router.severity import SeverityEstimator
    from tests.agent.router.helpers import SEVERITY
    env.router.severity = SeverityEstimator(dict(SEVERITY) | {"safety_event_types": ["elevator_trapped"]})
    first = env.router.handle_event(make_event("EV-U", 0, "load_spike", AIR), flush=False).ticket_id
    trapped = env.router.handle_event(make_event("EV-T", 0, "elevator_trapped", "LIFT-1"), flush=False).ticket_id
    second = env.router.handle_event(make_event("EV-V", 0, "load_spike", "P-HVAC-P"), flush=False).ticket_id
    for ticket_id in (second, trapped, first):  # 故意乱序写入
        record = {"summary": ticket_id, "round": 1}
        env.store.add_pending(Router._pending_id(ticket_id, "report", T0, record), at=T0, kind="report",
                              ticket_id=ticket_id, record=record)
    order = list(dict.fromkeys(item["ticket_id"] for item in env.router._ordered_pending()))
    assert order == [trapped, first, second]  # 困人在前；其余同开单时刻，按事件单号
    assert Router._pending_id(first, "report", T0, {"round": 1, "summary": "x"}) == \
        Router._pending_id(first, "report", T0, {"round": 1, "summary": "x"})


def test_same_ticket_urgent_and_report_are_both_kept_in_stable_order(env):
    """#262 复审：同一事件单同一分钟的 urgent 与 report 两条不互相覆盖，顺序固定；混合时区不抛错。"""
    from datetime import datetime
    from plantkg.agent.router.router import Router
    ticket_id = env.router.handle_event(make_event("EV-U", 0, "load_spike", AIR), flush=False).ticket_id
    env.store.take_pending([i["item_id"] for i in env.store.pending()])
    urgent = {"summary": "urgent：停产", "round": 1, "urgent": True}
    # 轮次、摘要都相同，只有提案不同：编号仍不碰撞
    reports = [{"summary": "压力异常", "round": 1, "proposals": [p]} for p in ("pr-2", "pr-1")]
    for record in (*reports, urgent):
        env.store.add_pending(Router._pending_id(ticket_id, "report", T0, record), at=T0, kind="report",
                              ticket_id=ticket_id, record=record)
    assert len(env.store.pending()) == 3  # 编号不碰撞
    first = [i["record"]["summary"] for i in env.router._ordered_pending()]
    env.store.add_pending("pend-naive", at=datetime(2026, 7, 1, 2, 0), kind="report", ticket_id=ticket_id,
                          record={"summary": "无时区", "round": 1})
    assert len(env.router._ordered_pending()) == 4  # 无时区与带时区混排不抛 TypeError
    assert first[0] == "urgent：停产"  # urgent 在本轮汇报前，不靠哈希
    assert first == [i["record"]["summary"] for i in env.router._ordered_pending() if i["record"]["summary"] != "无时区"]


def test_report_with_urgent_looking_summary_is_not_treated_as_urgent(env):
    """排序看显式 urgent 字段：摘要以 urgent 前缀开头的普通汇报不会被当 urgent。"""
    from plantkg.agent.router.router import Router
    ticket_id = env.router.handle_event(make_event("EV-U", 0, "load_spike", AIR), flush=False).ticket_id
    env.store.take_pending([i["item_id"] for i in env.store.pending()])
    fake = {"summary": "urgent：只是摘要这样写", "round": 1, "proposals": ["pr-1"]}
    real = {"summary": "停产", "round": 1, "urgent": True}
    for record in (fake, real):
        env.store.add_pending(Router._pending_id(ticket_id, "report", T0, record), at=T0, kind="report",
                              ticket_id=ticket_id, record=record)
    assert [i["record"] for i in env.router._ordered_pending()] == [real, fake]
