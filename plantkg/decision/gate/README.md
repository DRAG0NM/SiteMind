# B15 计划门控与人工确认

（编号按 v0.8：计划生成与计划账本属 B19，Nexent 网关与 MCP 服务属 B23；对照见 内部设计文档 docs/development/12-开发规则.md。）

本模块为 B15 的持久化计划、固定政策和 B04 统一人工确认提供接缝。启用正式存储时，`build_services` 自动注册真实 `Gate`、`plan_confirmation` handler 和 `propose_plan_confirmation` 工具，并以 `services.gate` 暴露服务。未启用存储时不创建此服务。**默认提供者只能核验 B14 预测；尚无完整的正式规则和费用材料提供者，不能将本模块视为完整 B15 验收已完成。**

## 接口与责任

- B19：用 `services.gate.register(plan, context, metadata)` 登记真实 Planner 的 `draft` 计划和材料定位信息；`load(plan_id, revision=None)` 返回计划，调用 `assess(plan_id, expected_revision, context)` 进行门控。调用者必须提供可信、固定到计划所属 run 和 manifest 的 `Context`。同计划同修订内容或元数据冲突会拒绝，登记时保存深拷贝。
- 冻结的 `Gate.assess(plan, forecast_quality, rule_status, baseline)` 通过 `services.gate.bind(context).assess(...)` 使用；注册表中的未绑定适配器直接拒绝。四参数中的简化材料不能证明来源：**材料以服务端注入的 provider 为唯一权威**，不能把它们当成外部授权自动放行的方式。
- B16：只从 `services.gate.executable(plan_id, revision=None)` 读取可执行计划；它校验当前修订、有效期、持久化 Approval，以及人工批准对应的 B04 动作状态、目标和确认人。不能把调用者构造的 `Plan(status='confirmed')` 当批准依据。
- B23：已有 `plantkg_propose_plan_confirmation` 工具只创建或复用人工动作；`expected_revision` 是**计划修订**。人工通过既有 `POST /api/v1/actions/{id}/confirm` 或 `python -m plantkg actions ...` 操作，使用配置的复核员；统一入口的 `expected_revision` 是**动作修订**。提议不写批准。失败以既有 `PlantkgError` 代码表述，例如 `NO_CTX`、`CROSS_PLANT`、`REVISION_CONFLICT`、`PRECONDITION`、`STATE_CONFLICT` 和 `DEPENDENCY_UNAVAILABLE`。

  如果政策拒绝计划，提议工具保存 `rejected` 计划和评估决定，返回 `status=rejected`、计划 `revision` 和 `reasons`，不创建确认动作。已转人工的计划只能使用原提议理由复用动作；理由不同返回 `IDEMPOTENCY_MISMATCH`。

`GateService` 可通过构造参数 `materials_provider(plan, metadata) -> PolicyMaterials` 和 `clock() -> datetime` 注入受信任的材料提供者与业务时钟。提供者必须从持久化原始规划请求及固定 run 对应的预测、规则、来源和明确 CNY 成本构建材料，并核对身份；工具参数和模型自报不构成材料。`metadata` 仅是定位辅助，不自动验证其中断言。默认 B14 提供者使用 `metadata` 中的 `planning_request`（原始请求的 JSON 对象）与 `forecast_key`（B14 产物键），验证并载入固定 run 的预测；由于尚未提供已核验的依赖、价格、基线和校准材料，人工确认仍会被阻止。直接构造服务且不注入材料提供者时返回 `DEPENDENCY_UNAVAILABLE`。默认装配按计划所属 run 读取 B07 持久化回放游标，门控有效期与 B04 计划确认动作的 TTL 均使用该游标；不同 run 的时间彼此独立，重启后继续使用已提交游标。直接构造服务时应注入适当的业务时钟。其他类型的 B04 动作仍使用原时钟。

## 政策与修订

`gate-policy-v1` 固定阈值为覆盖率 1.0、可溯源校准可靠度至少 0.95、可比基线成本大于 0.01 CNY、相对成本差不超过 0.10。未知校准、无可比基线、feasible 等触发人工审核；身份冲突拒绝；缺必要预测、价格、关键规则审核等阻止人工确认。策略决定及阈值快照保存在数据库中。B14 当前预测未校准，也没有可追溯校准可靠度，完整真实 B14 预测应转人工。具有完整校准证据的构造样例只能验证自动路径，不能证明真实预测已经校准。

例如，计划 `p@1` 是 draft；提议后计划变为 `p@2`、动作目标是 `p@2`，新动作自己的 revision 为 0。复核入口提交动作 `expected_revision=0`；动作确认后变为 revision 1。读取计划时核实 B04 已确认，然后保存 manual Approval 并产生最终计划 `p@3`。B16 应读取 `p@3`，不能用旧修订。每个计划状态变迁递增计划修订，每个动作状态变迁递增动作修订，两者互不替代。

`repository.decision(plan_id, revision)` 和 `policy_snapshot(plan_id, revision)` 按指定的计划修订精确查找策略评估记录；省略 `revision` 时返回该计划最新的一条评估记录，而不是保证对应当前计划修订。人工确认或 B04 动作拒绝、过期产生的后续计划修订不新增策略评估记录，因此精确查询该修订返回 `None`，省略修订则仍可读到之前的 `review_required` 决定。B16 应以当前计划状态、当前修订的 Approval 和动作状态判断执行资格；B23 展示人工处理结果时应读取当前计划和关联动作，不应把最新策略决定当成当前状态。

## 故障恢复与审计

B16 内部事务接缝：先在事务外调用 `executable(plan_id, revision)` 完成可能的人工批准懒最终化；再在与 B07 共用的 SQLite 单写者事务中调用 `require_executable_in_transaction(con, plan_id, revision, run_id, manifest_id, instant)`。该方法只读同一连接，复核最新计划、批准、人工动作和已提交回放游标，并要求完整候选槽在有效期内。它返回持久计划，不执行、不写审批、不替代 B16 对调用者计划内容的比较。事务还须包含 B16 执行事实及 B07 单槽 CAS，任何异常全部回滚。

B04 先调用 handler，再单独提交动作状态。B15 的 handler 先按 action ID 持久暂存结果，不在该步骤暴露执行资格；动作提交失败时，重试重新核验条件并复用暂存结果。只有确认动作实际持久化为 `confirmed` 且目标和确认人匹配时，后续 `load` / `executable` 才最终写入计划确认与批准。B04 reject / expire 不调用 handler；读取计划时同步为不可执行的 rejected 状态。决定和状态在 B15 事务中保存，`gate_decided` 事件是在提交后的尽力证据投影；B04 自己产生动作提议和决定事件。

门控转人工跨 B15 与 B04 两次事务，B15 先持久化提议意图，再以确定性键创建动作，最后原子保存计划状态与决定；中途故障后可用同一请求恢复。旧 run 固定的 manifest 不因新 manifest 成为活动版本而替换。

## 当前限制

- 默认材料提供者读取固定 run 的 B14 产物，但不会把请求内的规则 ID、目标值或配置路径当作已验证的依赖和 CNY 成本。因此，在缺少这些来源时计划会转人工，确认和执行资格均被拒绝。B14 的区间仍未校准，真实自动放行未验收。
- B09 在求解时核验规则编译和包贡献，但 `PlanResult` 未暴露可持久重建的完整贡献回执、依赖时段、配置版本和成本口径。建议由 B09/B19 提供绑定请求、结果与固定 run/manifest 的只读产物接口；B15 消费该产物而不重新实现求解器。
- B12 提供 `load_tariff_config`、`load_tariff_rules`、`acceptance_blockers` 和逐时段计价，但当前 B15 缺少能够从固定 manifest 找到本次实际电价配置、规则版本与成本的公共来源。建议 B12/B19 对外提供可追溯的固定版本引用和独立 CNY 费用；正式数据须通过 B12 的 `acceptance_blockers`。完整知识规则的来源与复核仍归 B25。
- `tests/gate/test_gate_full_day.py` 使用真实 B12 加载器、规则编译、电价包、B14 和 B09 求解器，输入和电价为仓库演示数据；B12 明确拦截其正式验收。测试先验证默认材料不足无法确认，再注入明确标注的测试材料，验证 B04 HTTP 确认和重启读取。该注入只证明 B15 接缝，不证明正式材料完整。
- 没有新增计划 HTTP 路由、前端或登录；B19、B16 尚需在各自实现中调用本服务。

## 验证样例

在 Windows / Python 3.11 上，`python -m pytest tests/gate tests/versions tests/forecasting tests/planner -q` 为 130 项通过，`python -m pytest tests -q` 为 808 项通过。`python contracts/build.py --check`、`python scripts/check_boundaries.py` 及新增代码的 pyflakes 检查通过。完整 96 槽测试使用生成的 HIPE/PVGIS 输入、B12 演示规则和演示计价基础；未将它们作为正式数据。另用构造的校准材料验证自动批准与策略快照，用 SQLite 故障注入验证 B04 提交失败时不可执行、重试仅生成一次批准。全天测试使用真实装配的回放时钟，并验证独立运行、动作过期与重启后的计划有效期。
