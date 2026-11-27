# B16 状态与完成标准

依据 `内部设计文档` 分支 `docs/development/01-完整分支清单.md` 的 B16 任务卡及其「15 完成验收」，本文件记录完成标准、证据、下一步和业务验收状态。

## 结论（2026-11-01）

B16 原始任务卡要求的功能已全部实现并有测试证据；对外接口符合 `plantkg/core/ports.py` 的 `Executor` 与注册表契约。原先列为 B16 缺口的「可信求解回执签发与核验」「默认服务跑通 B09→B15→B16 闭环」「计划账本」不属于 B16 职责，裁定见 §3。默认装配仍缺执行依据提供者，属公共装配与 B09/B12/B25 的交接项，精确要求见 §4。

## 1 原始任务卡要求与证据

| 任务卡要求 | 实现 | 证据 |
| --- | --- | --- |
| 只在历史回放中记录动作并调用独立账本，不发真实或仿真控制 | `RecordExecutor` 只追加 `b16_executions` 与运行投影；无新增写工具或写 HTTP 路由 | `test_execution_ledger_transport.py`：执行后 `b16_executions` 仅 8 行、`replay_clock_history` 末序号 8、历史观测指纹逐条不变 |
| 一个状态提交者串行提交充放/工序结果和事件序号 | 单实例 `RecordExecutor` + 表唯一约束 `(plant,run,idempotency_key)`/`seq_before`/`seq_after`/`slot_start` + 事务内 B07 `advance_in_transaction` | `test_record_executor_integration.py`：双线程同槽一胜一 `STATE_CONFLICT`；`event_seq` 严格 1..96；`test_execution_ledger_transport.py`：默认装配下 `event_seq` 1..8 |
| 重复执行不重复充电/平移 | 幂等键 + 完整请求摘要 `request_digest`；同键同请求返回持久结果，同键异请求 `IDEMPOTENCY_MISMATCH` | 同上两文件；重启后同键仍返回同一 `ExecutionResult` |
| 过期/错误 run/未确认计划拒绝 | `gate.executable`（缺批准 `PRECONDITION`）、`require_executable_in_transaction`（修订 `REVISION_CONFLICT`、游标变化 `STATE_CONFLICT`）、`require_execution_snapshot_in_transaction`（依据变化） | `test_record_executor_integration.py` 的 5 类坏 plan/ctx；`tests/gate/test_execution_snapshots.py` |
| 保存预测计划与回放实际差异，失败不中止覆盖历史 | `forecast_error`（逐量实际-预测）、`soc_deviation`、plan 轨迹重算 | `test_record_executor_integration.py` 槽 1 夜间削减 `soc_deviation > 0` |
| 失败时中止或申请重新规划而不覆盖历史 | 事务回滚；三种 SQLite 触发器注入后 clock 与账本均无残留（含提交期失败） | `test_record_executor_integration.py`（含 DEFERRABLE 外键 AFTER 提交期失败） |
| 削减规则（03 §2.4）：先弃光、再削减放电，记录削减量与 SOC 偏差 | `apply_pv_cap` → `excess` → `curtailed = min(excess, usable)` → `actual_discharge = discharge - 剩余不足` | `test_record_executor_integration.py`：cap 8 限发 1 kW、cap 10 限发 0 kW，`pv_curtailed_kwh == cap/4`；`test_pv_cap_prerequisite.py` |
| 储能 SOC 接续、守恒、边界 | 上槽实际 `energy_kwh` 接续；充放双向钳位到 `[reserve, upper]`；`validate_balance` | 96 槽全部 `balanced`；末槽 SOC `50 - .5/.9 + .225`；历史 `clock.at` 不泄漏未来投影 |
| 工序投影 | `operation_start`/`operation_power` 与冻结曲线逐项核对，负荷由 B08 曲线替换核算 | 槽 4-7 负荷 [8,6,12,14] kW 合计 10 kWh（平移生效） |
| 接口 `execute` / `read_execution` / 账本 provider | `Executor.execute(plan, context, expected_seq, idempotency_key)`；`read_execution`、`read_ledger`；注册 `ledger` 工具与 `GET /runs/{run_id}/ledger` | `plantkg/core/ports.py`、`plantkg/world/record/providers.py` |
| 运行类结果带 run_id、version_manifest_id、时间区间、来源引用 | `ExecutionResult` 身份四元组与 `event_seq`；`LedgerMeta.sources` 记 `bundle_id:online_record_hash` | `test_execution_ledger_transport.py` 逐条断言 run_id、manifest、`as_of`、时段起点 |
| 「不对温湿度历史观测伪造动作响应」「无业务事实覆盖更新」 | 只追加执行记录与投影，不改写时序观测 | `test_execution_ledger_transport.py`：执行前后 `load` 记录 `online_record_hash` 逐条相等 |

## 2 测试要求（任务卡第 14 项）与验收（第 15 项）

| 要求 | 证据 |
| --- | --- |
| 同请求执行两次账本不变 | `test_record_executor_integration.py`、`test_execution_ledger_transport.py` |
| 并发/乱序动作、过期清单引用、未确认计划拒绝 | `test_record_executor_integration.py`、`tests/gate/test_execution_snapshots.py`、`tests/replay/test_execution_bridge.py` |
| 改变动作后购电曲线改变且守恒成立 | `test_record_executor_integration.py`（cap 8 与 cap 10 两组 grid_import 233.25 / 233.0，均 balanced） |
| 负荷预测偏高的夜间时段触发削减，SOC 偏差被记录且账本仍平衡 | `test_record_executor_integration.py` 槽 1 |
| 外部 API 不进默认测试；平台集成单列真实验收证据 | 默认测试全为本地 SQLite/Parquet；真实 HTTP/MCP 见 §5 |

96 槽连续执行（含第 40 槽后由独立进程续跑）由 `tests/execution/test_record_executor_integration.py` 覆盖（自建装配）；默认服务装配（`build_services`）路径由 `tests/execution/test_execution_ledger_transport.py` 用 8 槽覆盖，包括真实 HTTP 与真实 MCP 传输、分页、知识截止、跨截止游标隔离、服务重建后读取。两处合计覆盖任务卡全部行为要求。

## 3 缺口裁定（A/B/C/D）

**A. B16 必须实现但尚未实现：无。** 原始任务卡 5 项详细任务、3 个接口、14 项测试要求与 15 完成验收均落在 §1/§2 的实现与证据中。

**B. B16 已实现但尚未验证：无（本轮已补）。** 原「未通过真实 HTTP/MCP 请求验证非空执行账本」即属此项，现已由 `test_execution_ledger_transport.py` 补齐。

**C. 属其他模块或公共装配，非 B16 责任：**

1. **B09 求解回执的来源核验**。B09 的正式验收只有「单一模型与目标汇总器、B08 口径校验、整数/互斥能力与超时规则」，没有「回执签发与核验」条目。B16 侧只要求「执行依据可核验且与批准计划一致」，已由 `StoredExecutionBasisProvider` 与 B15 批准快照实现；核验回调的实现权在 B09 维护人。
2. **执行配置及价格的正式来源**。`config_resolver` 需给出 pv 上限、储能参数、外送上限、电价与工序曲线，并与 run 的固定清单绑定；正式电价溯源属 B12/B25，设备与工序参数属资产配置。
3. **默认装配设置 `gate.execution_basis_provider`**。B16 已实现并提供该提供者，`bootstrap` 只缺一行装配（§4）。
4. **B09 求解 → B15 批准 → B16 执行的默认入口**。生产代码中没有任何计划创建或求解调用点；按 B17 任务卡「通过端口装配预测→共享规划→门控→回放→账本评估」，该入口属 B17 的 `run_day`。
5. **计划账本 provider**。`providers.py` 对 `kind=plan` 按契约返回 `DEPENDENCY_UNAVAILABLE`；B17 卡明确由 B17 提供。

**D. 正式业务材料验收（非软件阻塞）：** 正式工厂规则、电价、设备参数与预测校准来源，见 §6。

## 4 移交要求（最小变更）

默认装配若要自行产生可执行批准快照，只需在 `plantkg/bootstrap.py` 的 `_finish_services` 之前设置（无需改动 B16 代码）：

```python
version_run.gate.execution_basis_provider = StoredExecutionBasisProvider(
    SolveReceiptStore(storage.writer, version_run.runs, version_run.versions,
                      verifier=<C1 可信核验器>),
    <C2 config_resolver>,
    <C2 source_verifier>,
)
```

- **C1**：`verifier(context, receipt) -> bool`，由 B09 维护人在调用 `planner.solve_with_receipt` 的同一服务内提供；缺省为 `None` 时 `SolveReceiptStore.save` 拒绝持久化（`PRECONDITION`）。
- **C2**：`config_resolver(context, plan, receipt) -> (config, source_digest)`，`config` 必须恰好包含 `pv`/`storage`/`export`/`tariff`/`operations`，且与回执 `pack_evidence` 逐项一致（校验规则见 `plantkg/decision/gate/execution_basis.py`）；`source_verifier(context, plan, receipt, config, source_digest) -> bool`。
- **C4**：`run_day` 内的调用顺序为 `solve_with_receipt` → `SolveReceiptStore.save` → `gate.register/propose/confirm` → `gate.executable` → `executor.execute`。

这些设置不改变公共契约（`Executor.execute`、`execution ledger provider` 签名不变）。

## 5 验证证据

本机 Windows + Python 3.11.5，无虚拟环境：

- `python -m pytest -q` → `845 passed in 122.22s`
- `python -m pytest tests/execution/test_execution_ledger_transport.py -q` → `1 passed`
- `python -m pyflakes tests/execution/test_execution_ledger_transport.py` → 无输出

`test_execution_ledger_transport.py` 走默认 `build_services` 装配（`profile=dev`、固定 sqlite/graph/series 路径），用标记 `synthetic/test_only` 的合成材料构造 96 槽输入与求解回执，执行 8 槽后：

1. 真实 HTTP（`TestClient(create_app(services))`）分页读回 8 条 `balanced` 执行账本，`kind=plan` 返回 `DEPENDENCY_UNAVAILABLE`；
2. 真实 MCP（`Client(create_mcp_server(services))`）会话读到同一账本并翻页，早截止会话只见 2 条，跨截止游标被拒（`INVALID_REQUEST`），未绑定运行的会话被拒（`PRECONDITION`）；
3. `services.close()` 后用同一配置重建，继续执行第 9 槽并经 HTTP 读回 9 条。

注：测试内注入的 `StoredExecutionBasisProvider` 与回执核验回调是 §3-C1/C2 未就绪时的替身，明确标注 synthetic/test_only，不代表正式来源验收。

## 6 业务验收

待业务验收（不阻塞）。
