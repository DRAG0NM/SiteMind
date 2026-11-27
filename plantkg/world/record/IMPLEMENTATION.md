# B16 回放记录器

`services.executor` 在 `storage.enabled=true` 时装配为 `RecordExecutor`，也注册到 `Executor` 端口。关闭存储时不创建该服务。它只记录历史回放，不连接设备、不发送控制指令；没有新增 MCP 或 HTTP 写入口。存储启用时，注册表还提供只读 `ledger` 工具和 `GET /api/v1/runs/{run_id}/ledger?kind=execution`，按已提交序号分页读取可见的执行账本。计划账本属于 B19（v0.8 编号），当前返回 `DEPENDENCY_UNAVAILABLE`。

```python
# context、plan 和 seq 必须来自服务器绑定的固定 run 与 B15 服务。
plan = services.gate.executable(plan_id, revision)
result = services.executor.execute(plan, context, expected_seq, idempotency_key)
historical = services.executor.read_execution(read_context, result.event_seq)
ledger = services.executor.read_ledger(read_context, result.event_seq)
```

执行前需要 B09 `solve_with_receipt` 的真实回执、经可信服务核验并持久化的 `SolveReceiptStore`、B15 根据同一回执及受信规则/配置形成的批准快照。`StoredExecutionBasisProvider` 仅接受由服务器装配的配置解析和来源核验函数。默认 B15 提供者缺少正式规则、费用及校准来源，不能产生可执行快照；测试中的回调和来源材料明确为 synthetic/test_only，不得配置到正式环境。老批准可以照常读取，但没有快照时 B16 拒绝执行。

每次执行一个 15 分钟槽。输入准备、规则解析和 B08 计算在写事务外完成；事务内重新检查计划、批准、B04 确认、快照和持久回执，再写入结果/账本/投影并用 B07 CAS 推进，异常回滚。B07 `event_seq` 独立于 B04 事件号。相同幂等键及完整请求返回持久化结果，变化的请求报冲突；单 run 的时钟版本、槽和提交结果均有唯一约束。`read_execution` / `read_ledger` 只读取已提交且不早于 `visible_after` 的结果；B07 历史快照按该时点和可见截止读取投影。

工序曲线至少需要一条已核验的 `config` 或 `asset_version` 引用；规则等其他引用可用于说明，但不能单独证明配置来源。所有工序配置／资产引用均须在已核验集合中，避免空集合检查误将缺少来源的曲线视作已核验。

固定 run 的 `execution_input_mode` 可为 `online_observed` 或 `offline_scenario`，并通过 `execution_quantities` 明确负荷和光伏量名。在线路径使用 B07 候选读取，只接受在槽结束前可见的合格 measured。事后场景路径仅允许可信 `offline` 上下文，使用固定的 `execution_truth_source_ids`、`execution_truth_record_hashes` 和 `execution_truth_as_of`，且与在线记录指纹不相交。HIPE 南京映射保持 configured，PVGIS 保持 derived/estimated 和原始小时可见时间；来源指纹、原始记录和转换信息存入内部投影。公开查询继续遵守原 B07 截止；场景结果在最晚来源可见时间之前不可通过历史读取或状态快照取得。

快照中的审核规则决定光伏上限：实际可发减去物理限发后交给 B08；额外供需过剩先经济弃光再削减放电；实际供给不足时外送只送现场余量，不从电网购电外送。B08 计算并复核能量、费用和 SOC；下一槽接续上槽实际能量。`operation_start` 和 `operation_power` 与冻结的工序曲线逐项核对，使用 B08 曲线替换核算当槽负荷，状态只追加投影；不修改原始观测。缺价、缺配置、缺测、超额功率、非法状态或不可验证的快照均拒绝。

当前范围：单光伏实体、单储能实体以及明确冻结的工序清单；未核验或未支持的动作拒绝。后验来源晚于运行终点时，公开 B07 游标不能越过运行终点，相关结果可能无法在该 run 的公开时间范围内读取。内部 `read_execution_offline(context, seq)` 仅允许可信 offline 上下文及固定的事后场景截止，读取已提交结果；不注册到公开工具或 HTTP。正式工厂规则、费用、参数来源和预测校准尚未验收；构造测试和德国/欧洲来源映射不是南京现场实测。

## 传输层验证

执行后账本经真实传输层验证在 `tests/execution/test_execution_ledger_transport.py`：用默认 `build_services` 装配（`profile=dev`、固定 sqlite/graph/series 路径）与 `synthetic/test_only` 材料执行 8 槽，再经 `TestClient(create_app(services))` 与 `Client(create_mcp_server(services))` 分别分页读取，校验顺序、`balanced`、`as_of`、知识截止、跨截止游标拒绝和未绑定运行的拒绝；`services.close()` 后重建服务继续执行第 9 槽并读回 9 条。另断言执行前后历史观测指纹不变、写入只发生在 `b16_executions` 与 B07 游标。

该文件不重复 96 槽全量覆盖：96 槽（含第 40 槽后独立进程续跑）由 `tests/execution/test_record_executor_integration.py` 自建装配覆盖，其分页读取只经注册的 REST provider。

## 尚未完成的交付项（非 B16 代码）

默认服务装配尚未设置 `gate.execution_basis_provider`，也未提供 `SolveReceiptStore` 的 `verifier`；因此默认装配不能自行产生可执行的批准快照，测试中以替身注入。按 `STATUS.md` §3 的裁定，这属于公共装配（`plantkg/bootstrap.py`）与 B09/B12/B25 的交接项，最小变更见 `STATUS.md` §4，不需要改动本模块代码。正式工厂规则、电价、参数与校准来源仍待业务验收，上述测试不得视为正式服务接通。
