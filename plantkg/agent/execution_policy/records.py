"""B47 包内记录：操作工单状态机、执行幂等记录、抽查/纠错队列与临时规则。

这些是分级执行自己的账，只在执行表的 `record` 列与 trace 里流转；对外仍用 B46 的 WorkOrder / TempRule 契约
（操作工单的“处置中”在契约视图里记 confirmed，表内保留 in_progress）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from plantkg.core.contracts import ProposedAction, TempRule, WorkOrder

TASK_STATUS = Literal["proposed", "confirmed", "in_progress", "done", "rejected", "expired"]
EXEC_STATUS = Literal["executed", "skipped_duplicate", "infeasible", "queued", "proposed", "rejected"]


class WorkOrderTask(BaseModel):
    """操作工单 / 人工待办：待确认 → 已确认 → 处置中 → 完成（或拒绝、过期）。"""

    model_config = ConfigDict(extra="forbid")

    work_order_id: str
    ticket_id: str
    run_id: str | None = None
    round: int = 0
    created_at: datetime
    reason: str
    kind: Literal["work_order", "human_decision"] = "work_order"
    origin: Literal["d55", "safety_veto", "escalation", "manual"] = "d55"  # 开单来源：决定执行时是否整单等人工
    status: TASK_STATUS = "proposed"
    playbook_id: str | None = None
    actions: tuple[ProposedAction, ...] = ()
    pending_action_id: str | None = None
    confirmed_at: datetime | None = None
    done_at: datetime | None = None
    basis: str = ""  # 为什么开单：安全标记 / 重大损失 / 置信度 / 矩阵
    autonomy_paused: bool = False  # 暂停自动执行期间被压成“只提议”的积压项：一条一张，恢复后仍逐条批准（#245）

    def contract(self) -> WorkOrder:
        """给 B17 场景视图与 day.json 的契约视图；处置中在契约里记 confirmed。"""
        status = "confirmed" if self.status in ("confirmed", "in_progress") else self.status
        return WorkOrder(work_order_id=self.work_order_id, ticket_id=self.ticket_id, created_at=self.created_at,
                         reason=self.reason, actions=self.actions, status=status, pending_action_id=self.pending_action_id)


class ExecutionRecord(BaseModel):
    """一个动作的执行结果；主键在 SQLite 层是（plant_id, run_id, key）。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    run_id: str
    ticket_id: str
    proposal_id: str | None = None
    round: int = Field(ge=0)
    action_index: int = Field(ge=0)
    entity_id: str | None = None
    command: str | None = None
    status: EXEC_STATUS = "executed"
    at: datetime
    detail: str = ""


class QueueItem(BaseModel):
    """抽查 / 纠错 / 先做后审队列项；结果回写供 B32 进化。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["correction", "sampling", "review"]
    item_id: str
    ticket_id: str
    run_id: str | None = None
    at: datetime
    status: str = "pending"  # pending / correct / wrong / accepted / rejected
    record: dict[str, Any] = Field(default_factory=dict)


class StoredTempRule(BaseModel):
    """临时规则的存储视图：契约 TempRule + 所属运行与状态。"""

    model_config = ConfigDict(extra="forbid")

    rule: TempRule
    run_id: str | None = None
    status: Literal["active", "expired", "cancelled"] = "active"
    intervention: dict[str, Any] | None = None  # 人工撤销记录（#245）：重复撤销按它幂等返回
