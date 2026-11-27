"""#123 B04 确认事务两阶段：副作用已生效时动作不可拒绝；落库失败重启后动作停在 needs_repair。"""

from __future__ import annotations

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import ErrorCode, PlantkgError, ToolResult
from tests.versions.conftest import PLANT, REVIEWER, context


class Handler:
    kind = "publish"

    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, action, actor):
        self.applied.append(action.action_id)
        return ToolResult(ok=True, business_status="done", data={"published": action.target_ref})


def test_failed_confirm_persist_blocks_reject_and_restart_marks_needs_repair(writer):
    """修前：handler 已执行、决定没落库，动作仍是 proposed，复核员随后 reject 成功（#123）。"""
    handler = Handler()
    store = SqlitePendingActionStore(writer, PLANT, {"publish": handler})
    normal = store.propose("publish", {"target_ref": "cand-ok"}, context(idempotency_key="ok-1"))
    assert store.confirm(normal.action_id, REVIEWER, "正常确认", 0, "d-ok").status == "confirmed"

    pending = store.propose("publish", {"target_ref": "cand-fail"}, context(idempotency_key="fail-1"))
    writer.submit(lambda con: con.execute(
        "CREATE TRIGGER fail_confirm_persist BEFORE UPDATE ON pending_actions WHEN NEW.status='confirmed' "
        "BEGIN SELECT RAISE(ABORT,'injected persistence failure'); END"))
    with pytest.raises(PlantkgError) as failed:
        store.confirm(pending.action_id, REVIEWER, "确认", 0, "d-fail")
    assert failed.value.code == ErrorCode.INTERNAL
    assert handler.applied == [normal.action_id, pending.action_id]  # 副作用已生效
    assert store.get(pending.action_id).status == "proposed"  # 决定没落下来

    with pytest.raises(PlantkgError) as conflict:  # 修前：reject 成功，出现“动作 rejected、副作用仍在”
        store.reject(pending.action_id, REVIEWER, "想驳回", 0, "d-reject")
    assert conflict.value.code == ErrorCode.STATE_CONFLICT

    restarted = SqlitePendingActionStore(writer, PLANT, {"publish": handler})
    assert restarted.get(pending.action_id).status == "proposed"
    for decide in (
        lambda: restarted.confirm(pending.action_id, REVIEWER, "重启后再确认", 0, "d-again"),
        lambda: restarted.reject(pending.action_id, REVIEWER, "重启后再驳回", 0, "d-reject-2"),
    ):
        with pytest.raises(PlantkgError) as blocked:
            decide()
        assert blocked.value.code == ErrorCode.STATE_CONFLICT  # 修前：两个都成功

    writer.submit(lambda con: con.execute("DROP TRIGGER fail_confirm_persist"))
    later = restarted.propose("publish", {"target_ref": "cand-after"}, context(idempotency_key="after-1"))
    assert restarted.confirm(later.action_id, REVIEWER, "修复后照常确认", 0, "d-after").status == "confirmed"
    assert restarted.reject(restarted.propose("publish", {"target_ref": "cand-after-2"},
                                              context(idempotency_key="after-2")).action_id,
                            REVIEWER, "正常拒绝", 0, "d-after-reject").status == "rejected"


def test_cancel_only_closes_proposed_action(writer):
    store = SqlitePendingActionStore(writer, PLANT, {"publish": Handler()})
    action = store.propose("publish", {"target_ref": "cand-cancel"}, context(idempotency_key="cancel-1"))
    cancelled = store.cancel(action.action_id, "对应工单已完成")
    assert cancelled.status == "expired" and cancelled.revision == 1
    assert cancelled.payload["cancel_reason"] == "对应工单已完成"
    assert store.cancel(action.action_id, "重复取消").status == "expired"  # 幂等
    confirmed = store.propose("publish", {"target_ref": "cand-confirmed"}, context(idempotency_key="cancel-2"))
    store.confirm(confirmed.action_id, REVIEWER, "确认", 0, "d-cancel")
    with pytest.raises(PlantkgError) as conflict:
        store.cancel(confirmed.action_id, "已确认不能再取消")
    assert conflict.value.code == ErrorCode.STATE_CONFLICT
