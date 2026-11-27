# B25 状态与完成标准

依据 `内部设计文档` 分支 `docs/development/branch-plan.json` 的 B25 任务卡（`feature/global-graph`）。`docs/architecture/contract-check.md` 等处的“B25 extract_rules”是 v0.7 旧编号（规则抽取，现归 B30），不适用。

## 怎么看效果

```powershell
python -m plantkg.knowledge.graph            # 空压机供气、管网压降影响区域、突增来源、安全判断、优先级、预案
python -m pytest tests/knowledge/graph -q
```

## 任务卡要求与证据

| 要求 | 实现 | 证据（`tests/knowledge/graph/test_global_graph.py`） |
| --- | --- | --- |
| 工厂拓扑写入 Oxigraph：空压机 → 管网 → 设备，哪些设备服务哪些楼层 | `GlobalGraph.load`：B18 配电树 + `graph.yaml` 的冷却水、压缩空气、冷冻水网络与服务关系，写入命名图 `urn:plantkg:graph:global:base`，Brick 类对齐 | `test_compressor_reaches_the_lines_it_feeds`、`test_cooling_water_failure_propagates_across_media` |
| 安全标记：轿厢有人、消防与应急照明回路、生命保障、危化品仓库、数据机房；洁净车间、数据机房为硬约束；查不清按涉及 | `safety_tags`：直接命中，或断电、停机、检修经电、水、气、冷冻水网络间接命中（停供类动作互相等价）；目标可以是区域；未知目标、未知动作、未给 occupied 都转工单 | `test_occupied_elevator_action_is_tagged`、`test_cutting_power_upstream_hits_tagged_areas`、`test_maintenance_counts_as_cutting_supply`、`test_unclear_cases_count_as_involved`、`test_unknown_action_always_needs_work_order` |
| 优先级配置：安全固定第一；生产时段生产优先、上班高峰舒适优先、夜间成本优先；带版本 | `priority_rules`，窗口与 `knowledge_version` 存在图里 | `test_priority_safety_first_then_by_zone_and_time` |
| 应急预案作为带版本的规则 | `playbooks`：触发、牵头专家、分工步骤、是否工单、条款原文 | `test_playbooks_filter_and_cite_clauses` |
| 图谱查询注册为 MCP 工具 | `packs/global_graph` 登记 `trace_impact`、`find_sources`、`safety_tags`、`priority_rules`、`playbooks` | `test_tools_are_exposed_over_mcp` |
| 测试：一台空压机查到它供气的生产线；对电梯有人的动作返回安全标记 | 同上 | 同上 |
| 验收：“空压站压力突降会影响哪些区域”并给出路径 | `trace_impact("AIR-HDR")` | `test_air_header_pressure_drop_names_zones_with_paths`：洁净车间 `AIR-HDR → AIR-BR-CR → CR-PNEU`，生产车间 `AIR-HDR → AIR-BR-P → LINE-1` |

## 数据性质

- 介质网络、服务关系、安全标记、优先级、预案：按常理编写，`configured`；图谱专有节点（管网、应急照明回路、消防泵等）标 `synthetic`。
- 条款原文 `configs/sites/factory/graph/regulations.yaml`：虚拟厂内规程，`synthetic`，不是国家标准原文。B26 资料入库后可替换。

## 约定与补的东西

- B30 等写入方用 `GlobalGraph.add_subgraph(<名>, turtle)` 写 `urn:plantkg:graph:global:<名>`，不改 base；查询取所有子图并集（`test_subgraph_is_queried_together_with_base`）。
- 知识平面不能导入 world 平面，图谱直接读 B18 的 `site.yaml`（逐层展开规则与 `SiteModel.load` 一致），拓扑校验仍由 B18 负责。
- `find_sources` 候选角色：`load`（负荷类症状的用电设备，区域查询路径从供电源头写起）、`supply`（上游供给方）、`leak`（压降时本区气网入口往下的支管与用气点）、`serving`（区域查询时服务该区、本身可能就是来源的设备，如温度偏离时的空调箱）；区域失电时服务该区的设备是受影响负荷，不算候选。
- MCP 输出（`trace_impact` 的安全标记、区域、楼层、受影响设备，`find_sources` 的候选，`safety_tags` 的标记）按 8KB 字节预算逐条装入，常驻 `*_total` 与 `truncated`；`safety_tags` 的判定按全部命中算，不受截断影响；`add_subgraph` 后 `knowledge_version` 随子图内容变化。
- 默认配置路径沿用 `core/config.py` 的仓库根 `configs/` 约定，没有打进 wheel；打包配置属 B01 范围，这里不单独处理。以后要用 wheel 或镜像部署到仓库外时，需另开打包任务，把 `configs/` 打进包或改成可配置路径（全项目共用，不只图谱）。
- `pyoxigraph` 加入基础依赖：图谱包随默认装配加载。storage extra 里保留同版本一项，和 solver extra 一样兼容现有安装命令。
- 多上游（多电源、多气源）时，区域 `load_surge` 的供电链取编号最小的上游；B30 接入多电源后再按实际运行方式选。
- `configs/defaults.yaml` 启用 `global_graph` 包；`tests/skeleton` 两处断言随之更新（mock 健康里多一项真实实现，dry-run 包列表多一项）。

## 待业务验收（不阻塞）

- 管网走向、服务楼层、安全标记范围、优先级时段、预案步骤都待业务确认。
