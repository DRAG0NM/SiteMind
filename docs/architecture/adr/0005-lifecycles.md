# ADR-0005 状态机：版本清单、候选、待确认动作、后台任务

- 状态：已决；版本清单生命周期由 B01 写定（07 Q15，09 D16）
- 影响分支：B04、B15、B18、B24、B30、B42

迁移表在 `contracts.py`（`*_TRANSITIONS`）与 `contracts/enums.json`，实现方只能沿表迁移。

## 版本清单 `ManifestStatus`

```text
prepared ──检查完整性──▶ verified ──短事务激活──▶ active ──被替换──▶ retired
   │                       │                                    │
   └──────▶ discarded ◀────┘                      回滚：retired ─▶ active
```

- 发布顺序固定：写不可变产物 → `check_integrity` → 在 SQLite 短事务里激活。未激活产物不可见，被引用的旧版本不删除。
- 任一时刻恰好一份 `active`；`activate(expected_active, candidate)` 检查预期活动版本，不符返回 `PLANTKG_E_STATE_CONFLICT`。
- 回滚 = 重新激活一份 `retired`（曾经激活过，`ever_active=true`）的清单，不删产物；`discarded` 从未激活，不可回滚到。
- 每次运行显式绑定一份清单；发布与回滚只影响之后新建的运行，历史 run、执行与旧清单不可覆盖。

## 进化候选 `CandidateStatus`

`proposed → validated | rejected`；`validated → published | rejected`；`published → rolled_back`。
`validated/published` 必须有回测报告；`published` 必须关联复核员确认过的 `publish` 待确认动作（契约校验）。参数候选须更好且硬约束不退化；知识候选按正确性与审核准则，费用升高不等于失败（07 Q17）。

## 待确认动作 `PendingActionStatus`

`proposed → confirmed | rejected | expired`，终态不再变化。

- `kind`：`review_decision`、`plan_confirmation`、`publish`、`rollback`。
- 每次状态变化 `revision + 1`；确认/拒绝请求必须带 `expected_revision` 与 `idempotency_key`：修订不符 `PLANTKG_E_REVISION_CONFLICT`，同键不同内容 `PLANTKG_E_IDEMPOTENCY_MISMATCH`，已过期 `PLANTKG_E_ACTION_EXPIRED`，同键同内容返回原结果。
- 确认/拒绝必须记录确认人、时间与原因；只有 `review_decision` 允许 `edits`。
- 权限：确认端点要求 `reviewer` 角色，团队两人均配置为复核员；代码保留角色校验。

## 后台任务 `JobStatus`

`pending → running | cancelled`；`running → succeeded | failed | cancelled`。

- 不随 MCP 调用取消（平台 60 秒切断后求解继续）；`succeeded` 必须给 `result_ref`，`failed` 必须给 `error`，终态必须有 `finished_at`。
- 同一 `idempotency_key` 提交返回同一 Job。

## 计划 `PlanStatus`

`draft → auto_confirmed | review_required | rejected`；`review_required → confirmed | rejected`；`auto_confirmed/confirmed → executed`；未执行前新修订使旧修订 `superseded`（`PLAN_TRANSITIONS`）。门控转人工时 `GateDecision` 必须关联计划确认动作。
