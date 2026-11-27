# day.json 数据口径（B17 导出）

`python -m plantkg.world.scenario export-day` 把一次真实场景运行按 `contracts/day.schema.json` 写成 day.json；`GET /api/v1/demo/day/{run_id}` 只读 `runtime/days/` 下已导出的文件，不另算一份。下面这些口径供开发工作台实时流与回放展示页共同使用（#238）。

## 时间

- `local_date` 是厂区当地日期，`timezone` 是 IANA 时区，一天取 `[当地 00:00, 次日 00:00)`。`runs` 是运行接续链，`run_id` 是链末主运行。
- 提案按 `binding.trigger_at`、决策按 `at`、事件单按 `opened_at`、工单按 `created_at`、Agent 运行按 `binding.trigger_at` 落进当天；跨日记录见下一节。
- **`timing.*` 是墙钟**（协调器记录的真实请求与返回时刻），只作耗时证据，不是场景时间。场景时刻看 `binding.trigger_at` 以及各小节自己的 `at`/`opened_at`/`created_at`。证据抽屉与控制台运行席按“触发 08:05 · 耗时 47.7 秒”显示，不要把 `timing` 的绝对时刻当成回放日时刻。

## 跨日引用 context

- `context.proposals` / `context.events` / `context.tickets` 携带“被当日记录引用、但发生在当日以外”的记录，原样携带，不进当日时间轴和计数。
- 当日 decisions 的 `conflict.proposal_ids` 指向的提案、当日 tickets 的 `event_ids` 指向的事件、当日 work_orders 的 `ticket_id` 指向的事件单，不在当日小节时到 context 里找。
- 展示页按 ID 查找先查当日小节、再查 context；时间轴和计数只用当日小节。

## 事件单与工单状态

- 记录带可选 `status_history: [{status, at}]`，只在状态变化时追加，不按分钟写。
- 导出时 `status` 取 `status_history` 中 `at < 当日结束` 的最后一条；`status_history` 也截到当日结束，只带 `at < 当日结束` 的条目（运行时存储不变），7-01 的导出不会出现 7-02 才发生的迁移；`context` 里的记录同样截断。没有 history 时回退记录里的当前 status。
- 写入侧要把初始状态也写进 history（开单时的 open/waiting、建单时的 proposed 等），否则早于第一次状态变化的导出日无法还原。

## 指标与限值

- `metrics` 是 `[DayMetric]`。B33 的组是 baseline / independent / coordinated / knowledge；`group="config"` 是配置限值，条目带 `nature=configured`，不参与改善率（`plantkg/evaluation/report.py` 计算比较时排除这一组）。可选 `variant` 表示版本/批次维度（基线、修复后、批次1 等），`(group, metric, variant)` 在一份导出里唯一；指标名不再带 `@` 后缀。指标清单见 `docs/architecture/metrics.md` 的“B33 产物指标名”表。
- 工厂包目前 6 项限值：`transformer_limit_pct`、`contract_capacity_kw`、`battery_soc_floor_pct`（能源包，从 site.yaml 与 storage.yaml 读）、`hvac_supply_setpoint_c`（暖通包，从 thermal.yaml 读）、`compressed_air_floor_mpa`、`compressed_air_leak_threshold`（设备包）。数值都在各包配置里，导出不写死；读不到就不输出该项。
- 显示用上限与实际硬约束分开：`transformer_limit_pct`（configured，百分比）只是展示刻度的上限；实际容量检查按有效限额：剧本 `params.capacity_kw` 配了该节点时以它为准（演示日 TX1 = 1150 kW），否则按 `capacity_kva × power_factor`（site.yaml 的 power_factor 0.9，即 1800 kW）。`load_ratio` 也除以同一个有效限额，所以 08:05 峰值 95%、08:06 储能放电后降到 59%。
- `export-day --metrics <file>` 可重复，接受 B33 `metrics.json`（顶层 dict 的 `metrics` 列表）或裸列表；未传时 metrics 只剩 config 组。

## 证据与资产

- `evidence` 是运行链里当天发生、当日结束前可见的 B26 证据事件，从同库 `SqliteEvidenceSink` 按运行链与当日读取。
- `assets` 只放被 `evidence[].sources[].asset_version_id` 引用到的资料版本，未被引用的不导出。
- CLI 打印每类数据的条数；某类为 0 时同时打印原因，例如“metrics 0 条（未传 --metrics）”“evidence 0 条（库内无证据）”。

## 校验

- 单文件：`python -m plantkg.contracts.validate runtime/day.json`。
- 链模式：`python -m plantkg.contracts.validate --chain runtime/scenario.db --run RUN_ID`。它逐日导出该运行链覆盖的所有日期，检查 tickets / proposals / decisions / work_orders 的链内并集，检查每份导出的引用（提案、事件、事件单、证据资产）能在当日小节或 context 找到，并用 `DayRecord` 再校验一次序列化结果。问题清单为空就是通过。


## 全厂资产版本快照（#238）

`assets` 是当日证据所引用的资料版本子集，不是厂区总量。`knowledge` 指标 `asset_total` 为同库同厂区截至 `as_of` 可见的所有资产版本（含历史版本），`asset_activated` 为其中最高阶段为 `referenced` 的数量；`asset_state_counts.counts` 包含 `dormant`、`identified`、`in_graph`、`referenced` 四个互斥非负整数，`value` 为 null 表示结构化数值。`asset_activation_rate` 是 referenced/total 的 0–1 ratio。统计本身 `nature=derived`，note 标出来源资料性质；引用不代表业务收益。

截止时间是当地日半开窗口的末尾前一微秒与持久化分钟账本所能证明的运行推进时间的较早者（槽内未落盘的分钟不会被猜测）；未完成日有明确标记。目录可用但为空时总量和四态均为 0，比例为 null；目录未装配或不可读时均为 null 并附原因。前端必须在回放游标到达 `as_of` 后才显示快照作为全量数据，较早时刻不能展示未来统计。旧导出没有这些可选字段时按原有方式处理。
