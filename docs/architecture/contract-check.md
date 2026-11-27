# 契约覆盖核对

B01 验收要求“契约涵盖全部 50 个执行分支（v0.8）的已知接口，未知平台事件用适配边界标注”。下表逐分支列出接口在契约中的落点。记号：**R** = `contracts.py` 记录/常量，**P** = `ports.py` 端口，**T** = MCP 工具（`TOOL_SPECS`），**A** = REST 路由（`API_ROUTES`），**内部** = 模块内部实现，不跨模块，不进契约。

分支编号与 `docs/development/branch-plan.json`（v0.8，50 个执行分支）一致；v0.7 旧编号的对照见 `docs/development/12-开发规则.md`「新旧编号对照」。

## 核验级别

| 级别 | 含义 | 范围 | 自动检查 |
| --- | --- | --- | --- |
| 已登记 | 每个执行分支的接口名都对应到一个契约落点 | 全部 50 个分支（下表每分支一行） | `test_coverage.py::test_every_branch_has_its_own_row` |
| 落点已核验 | 表中每个反引号落点（记录、字段、枚举、端口、工具、路由、文档）在契约中真实存在 | 全部 50 行；`replace_shifted_load` 等标“内部”的函数除外 | `test_landing_points_exist` |
| 字段已核验 | 04「关键记录与数据契约」点名的字段逐个存在，04 写法不同之处列为别名 | 04 契约表全部记录，及版本组件、UIEvent 类型等枚举 | `test_04_record_fields`、`test_04_enumerations` |
| 签名已核验 | 04「端口签名」与「规划模型契约」点名的参数按顺序存在 | 04 列出的全部端口方法 | `test_04_port_signatures` |
| **待核验** | 各任务卡“详细任务”中的业务语义（如 B12 电价规则的条件字段、B28 多工序约束的参数、B29 Skill 适配） | 04 没有点名、卡片只有自然语言描述的部分 | 无；由各分支开工时按卡核对，缺项走契约前置变更 |

**这些自动检查只证明“设计点名的名称在契约里存在且一致”，不等于 50 个分支逐接口、逐字段的完整核验。** 下表“契约落点”一列是人工对照登记；各卡的输入输出语义、详细任务中的业务约束、错误场景是否被契约表达，均属**待核验**，由各分支开工时按卡核对并在本表补记核验结论。

04 写法与契约不同之处：PendingAction 的 `expected_revision` 放在确认请求 `ActionDecisionRequest` 里，记录本身用 `revision`；Ledger 的“来源”为 `source_refs`；`RunSessionStore.register` 的 `plant`/`ttl` 为 `plant_id`/`ttl_s`；`Gateway.run` 的 `user` 为服务端认证的 `actor`。

运行 `python -m pytest tests/contracts` 执行以上检查。

## 基础与骨架

| 分支 | 接口 | 契约落点 |
| --- | --- | --- |
| B01 | 全部记录、端口、OpenAPI、ToolResult、UIEvent、RunSession/ctx、PendingAction、Job | 本次交付 |
| B02 | `python -m plantkg --profile mock`；`GET /api/v1/health`；注册表；`create_app/create_mcp_server`；`CtxResolver.resolve` 与失败关闭 | A `/health` → R `HealthResponse`；P `CtxResolver`；R `ContextChannel`、`ErrorCode.NO_CTX/UNKNOWN_CTX/CTX_EXPIRED`、`NOTICE_NO_CTX_PREFIX`；R `ToolSpec.accepts_ctx`（注入白名单）；注册表/入口函数 内部；HTTP 框架见 ADR-0006 |
| B03 | Repository、GraphStore、SeriesStore、MigrationRunner | P `Repository`、`GraphStore`、`SeriesStore`；MigrationRunner 内部（07 Q13） |
| B04 | 清单 prepare/check/activate；create_run/get_run；EvidenceSink；RunSessionStore；JobRunner；PendingActionStore；`/runs`、`/versions`、`/evidence`、`/actions`、`/jobs`、确认端点 | P `VersionStore`、`RunStore`、`EvidenceSink`、`RunSessionStore`、`JobRunner`、`PendingActionStore`、`ActionHandler`；R `VersionManifest`、`RunInfo`、`EvidenceEvent/Page`、`RunSession`、`Job`、`PendingAction`、`ActionDecisionRequest`、四个迁移表；T `get_job`；A 对应路由 |

## 世界、决策与四包

| 分支 | 接口 | 契约落点 |
| --- | --- | --- |
| B05 | `Adapter.normalize`；`SourceBundle.load`；`resample_with_provenance` | P `Adapter`；R `SourceRecord`、`SourceBundle`、`TimeMap`、`TransformStep`；重采样函数 内部（结果须写 `transform_steps`） |
| B06 | ingest/list_assets/get_asset；resolve_entity；record_activation；AssetRepository | P `AssetRepository`；R `AssetVersion`、`Entity`；T `ingest`、`list_assets`、`get_asset`；A `/assets*` |
| B07 | ReplayStateProvider.at/advance；VisibilityContext.filter；ts_query；fork_run_at | P `StateProvider`、`TimeSeriesQuery`；R `StateSnapshot`、`RunInfo.forked_from`；T `ts_query`；A `/runs/{id}/view`；可见性过滤按 ADR-0003 内部实现 |
| B08 | compute_ledger/validate_balance；replace_shifted_load；ledger provider | P `LedgerCalculator`；R `Ledger`、`LedgerEntry`、`LEDGER_TOLERANCE`；`replace_shifted_load` 内部 |
| B09 | `Planner.solve`；ModelBuilder；RuleCompilerRegistry；validate_solution | P `Planner`、`ModelBuilder`（v0.8 追加 pv_available、total_load、add_var 的 index/scalar/action 参数）、`RuleKind`；R `PlanningRequest`、`PlanResult`、`SolverBudget`、`SolverStatus`、`ContributionReceipt`；ADR-0001；注册表与校验 内部 |
| B10 | pv `DomainPack.contribute`；rule_kinds/state/ledger_terms；pack.yaml | P `DomainPack`、`RuleKind`；pack.yaml 格式由 B02 注册表定 |
| B11 | storage `contribute`；reserve_ratio 配置校验；rule_kinds/ledger_terms | 同上；配置校验失败用 R `PendingIssueReason.config_invalid` |
| B12 | tariff `RuleKind.compile`；`contribute`；trusted_yaml_rules loader | P `RuleKind`、`DomainPack`；R `Rule`（`review_status=trusted_config` + `config_ref`）、`PendingIssueReason.missing_price/negative_price` |
| B13 | production `contribute`；operation_window `RuleKind.compile`；load_profile_for_start | P `DomainPack`、`RuleKind`；R `ConstraintContribution`；曲线函数 内部 |
| B14 | `Forecaster.predict`；uncertainty_summary | P `Forecaster`；R `Forecast`（`params.pv_correction_k`）、`ForecastBundle.uncertainty_summary`、`InputSlice` |
| B15 | `Gate.assess`；propose_plan_confirmation；计划确认执行函数 | P `Gate`、`ActionHandler`；R `GateDecision`（v0.8 追加 `policy_outcome`）、`Approval`、`PLAN_TRANSITIONS`；T `propose_plan_confirmation` |
| B16 | RecordExecutor.execute；read_execution；execution ledger | P `Executor`；R `ExecutionResult`、`ExecutedAction`、`Ledger(kind=execution)`；T `ledger`；A `/runs/{id}/ledger` |
| B17 | 场景世界：事件流、仿真推进与极端场景剧本；`loop.py --scenario one_day --offline` | R `DayRecord`、`DayEvent`、`Context(channel=offline)`；脚本 内部 |

## 专家、知识、进化与评测

| 分支 | 接口 | 契约落点 |
| --- | --- | --- |
| B18 | 工厂场景装配：生产区、行政楼、设备拓扑与负荷合成 | R `Entity`、`AssetVersion`；场景装配 内部 |
| B19 | `run_day`；日前计划与实时削峰；照明回路；B0/B1/B2 | P `Planner`、`Evaluator`；R `EvaluationReport`、`PlanningRequest.policy`、`PlanResult.objective_terms`；T `run_day`、`get_plan`；A `/plans*` |
| B20 | 电梯专家：群控仿真与人梯/货梯调度 | P `DomainPack`、`RuleKind`；R `ConstraintContribution`；仿真 内部 |
| B21 | 暖通专家：区域温度模型、预冷与错峰 | P `DomainPack`、`RuleKind`；R `ConstraintContribution`、`Unit`；专家提案 内部 |
| B22 | 设备监测专家；`sensor_query`；维修工单 | P `DomainPack`；T `sensor_query`、`workorder_search`；工单 内部 |
| B23 | FastMCP 注册表服务；run_day/get_plan/explain；Nexent 北向；网关 `/agent/run`、`/agent/stop`；同步脚本 | P `Gateway`、`ToolHandler`、`Explainer`；R `UIEvent`、`AgentRunRequest`、`NexentEventRef`、`TerminalReason`；T `explain`；A `/agent/*`；**适配边界**：Nexent SSE 原事件只经 `contracts/events/nexent-sse-mapping.md` 映射，未列出的新事件类型记日志、不发 UIEvent；同步脚本 内部 |
| B24 | `Judge.decide`；Calibrator.fit/predict；judge.backend 开关 | P `Judge`、`Calibrator`；R `JudgeResult`、`JudgeBackend`；T `judge_candidates`；ADR-0007 |
| B25 | 全局图谱：设备拓扑、安全标记、优先级规则与应急预案 | P `GraphStore`、`ChangeImpact`；R `Entity`、`EvidenceRef`；T `trace_impact`、`find_sources`、`safety_tags`、`priority_rules`、`playbooks`；工具规格见 `contracts/mcp/tools.json` |
| B26 | 专家私有知识库：资料接入与按专家隔离检索 | P `DocumentParser`、`KnowledgeIndex`；R `AssetFragment`、`Locator`；A `/assets/{id}/fragments` |
| B27 | 主 Agent 值守层：事件准入、事件流订阅、Jev 研判与派发 | P `Gateway`、`ProposalSink`；R `UIEvent`、`EvidenceEvent`；T `recall`、`ask_operator`、`escalate`、`trial`；A `/agent/*` |
| B28 | 三层冲突裁决：安全否决、仿真打分与主 Agent 裁决 | P `Gate`、`Evaluator`；R `GateDecision`、`PolicyConfig`、`PlanResult`；裁决 内部 |
| B29 | 跨域会诊：专家并行研判、提案落库、预演与一个声音 | P `ProposalSink`；R `ExpertProposal`、`ExpertReply`；T 专家提案工具（`energy_propose` 等）；会诊 内部 |
| B30 | 四层本体半自动构建与规则抽取复核；复核队列与复核执行函数 | P `OntologyBuilder`、`GraphStore`、`RuleExtractor`、`RuleRepository`、`RuleKind`、`ActionHandler`；R `OntologyCandidate`、`OntologyLayer`、`RuleCandidate.alternatives`、`Rule`、`PendingIssue`、`PendingAction(kind=review_decision, edits)`；T `propose_candidates`、`link_entities`、`extract_rules`、`next_review_batch`、`propose_review_decision`、`prepare_tariff_review`、`investigate_onboarding`；A `/reviews` |
| B31 | 检索-推理双驱动与证据链 | P `Explainer`；R `EvidenceEvent.path`、`EvidenceRef`；T `lookup_rules`、`retrieve_evidence`、`explain`；A `/plans/{id}/explanation`、`/evidence` |
| B32 | 纠错进化：人工纠正 → 候选 → 回测 → 发布/回滚；变更影响 | P `CandidateGenerator`、`Backtester`、`ValidationPolicy`、`ActionHandler`、`KnowledgePublisher`、`ChangeImpact`；R `Candidate`、`BacktestReport`、`FrozenSplit`、`VersionDiff`（`unknown` 明确列出）；T `propose_candidates_evo`、`backtest`、`propose_publish`、`propose_rollback`、`impact_of_change`、`diff`；A `/versions/diff`、`/versions/impact` |
| B33 | 三组对照评测：有无 Agent 的数字对比 | R `EvaluationReport`、`UIEvent`；A `/baselines`；运行器 内部 |
| B34 | 2D 指挥工作台：事件流、专家状态、事件单与操作工单 | A 全部只读路由、确认端点、`/agent/*`；R `UIEvent`、`events/events.json`、`EvidenceEvent.model_params/params` |
| B35 | 证据抽屉、操作工单、纠错与抽查卡片与数字对比视图 | A `/evidence`、`/plans/{id}/explanation`、`/reviews`、`/review-metrics`、确认端点；R `JudgeResult.top_k` |
| B36 | 工厂四幕演示的集成验收、数据导出与录制 | 全部契约测试与 OpenAPI 回归；R `DayRecord` |
| B37 | 初赛提交包：截图、数字、Nexent 导出与在线演示 | R `ContentHash`（散列）；脚本 内部 |

## v0.8 与展示

| 分支 | 接口 | 契约落点 |
| --- | --- | --- |
| B38 | 回放展示页框架：标签页、播放条、简版 3D、数据加载与知识徽章 | A `/demo/day/{run_id}`、`/runs/{run_id}/view`、`/versions`；R `DayRecord` |
| B39 | 场景模板化：专家配置包、Skill 模板与场景装配 | R `Context.industry_pack`；场景模板 内部 |
| B40 | 医院场景：急救电梯预知与直达、四类温度分区 | P `DomainPack`；R `Entity`；场景剧本 内部 |
| B41 | 政府办公楼场景：日程驱动与能耗定额考核 | P `DomainPack`；R `Entity`、`PolicyConfig`；场景剧本 内部 |
| B42 | Watchdog：长期值守的进展与健康检查 | A `/watchdog/health`、`/watchdog/blocked`、`/watchdog/explain`；R `WatchdogHealth`、`WatchdogTicket`、`JobQueueSnapshot`；P `JobRunner` |
| B43 | 候选 Skill 生成、评估与回滚 | P `CandidateGenerator`、`ValidationPolicy`；R `CandidateKind.skill`、`ManifestComponent.skills`、`SkillSummary`、`SkillVersionRecord`；A `/evolution/skills`、`/evolution/skills/{name}/history` |
| B44 | 总决赛多场景验收与答辩材料 | 全部契约测试与 OpenAPI 回归；验收 内部 |
| B45 | 3D 精修 | web 内部（无契约改动） |
| B46 | v0.8 契约前置：专家报告、仿真动作、负荷分项、策略版本与“一天”数据格式 | R `ExpertProposal`、`ExpertReply`、`SimAction`、`DayRecord`、`DataSourceRef`、`PolicyConfig`；P `Executor`；A `/demo/day/{run_id}` |
| B47 | 分级执行、抽查、临时规则与协同处置 | P `Executor`、`ActionHandler`、`PolicyConfig`；R `ExecutedAction`、`PendingAction`、`EvidenceEvent`；T `escalate`、`trial` |
| B48 | 本体评测：真实点位数据集 F1、复核量-F1 曲线、版本迁移与构建效率 | R `FrozenSplit`、`MetricSeries`；A `/review-metrics`；评测 内部 |
| B49 | 回放展示页各页内容：主 Agent、四位专家、资料与图谱、效果对比、运行记录 | A `/evidence`、`/reviews`、`/demo/day/{run_id}`；R `UIEvent`、`DayRecord` |
| B50 | 初赛材料正文：Word 各节底稿 | 文档 内部（无契约改动） |

## 未知平台事件的适配边界

- Nexent 北向 SSE：只认 `nexent-sse-mapping.md` 表中的事件；新事件类型记日志，不进 UIEvent，不影响终态判定。
- Nexent 工具事件中的参数是模型填值，不作事实；工具调用事实只来自 EvidenceEvent。
- 平台改版（v2.8.2 以外）视为新适配器，B23 单列真实验收证据；契约不随平台字段改名。
