# B17 场景世界：事件流、仿真推进与极端场景剧本

## 怎么看效果

```powershell
python -m plantkg.world.scenario run configs/scenarios/factory_demo_day.yaml      # 两天演示剧本，约 5 秒
python -m plantkg.world.scenario run configs/scenarios/extreme_heat_wave.yaml     # 8 个极端场景之一
python -m plantkg.world.scenario export-day --date 2026-07-01 --out runtime/day.json
python -m plantkg.world.scenario export-day --date 2026-07-01 --metrics runtime/b33-results/metrics.json --out runtime/day.json
python -m plantkg.contracts.validate --chain runtime/scenario.db --run RUN_ID      # 逐日导出并查链内并集与引用
python -m pytest tests/world/scenario -q
```

`run` 在终端打印：运行接续链（每次知识发布分叉一次）、事件、apply 到仿真的动作、事件单与操作工单、临时规则（含试算代价）、逐小时状态（SOC、行政楼与洁净车间温度、人梯排队、空压管网压力、购电、光伏）和账本摘要；同样的内容以 JSON 写到 `runtime/<scenario_id>.json`（`--json` 改为在终端打印 JSON）。数据库默认 `runtime/scenario.db`。

演示日一次运行的结果：4 段运行（v1 → 07:30 开场 v2 → 09:30 插曲 v3 → 16:10 纠错 v4），2880 条分钟账本全部平衡，192 个槽 `minutes_balanced`；08:05 拥堵与突增后 08:06 电梯切分区、储能放电 400 kW 到下限、行政楼推迟预冷 15 分钟；10:30 用插曲学到的规则判出泄漏、切换照明回路；14:20 困人只生成工单，14:30 确认后 14:31 救援、货梯接管、井道通风；16:00 未见过的组合异常置信度 0.1 转人工，次日同类事件自动处理。

换剧本只改 YAML（`configs/scenarios/*.yaml`），不改代码。

## 任务卡对照

| 任务卡要求 | 实现 | 冒烟测试（`tests/world/scenario/test_scenario_world.py`） |
| --- | --- | --- |
| `ScenarioRunner.run(scenario, until) → RunTrace` | `runner.py` | 全部用例 |
| 独立仿真时钟、1 分钟子步；Simulator 端口 `step(state, sim_actions, dt) → state` | `plantkg/world/sim/base.py`、`ScenarioWorld.advance` | `test_minimal_script_runs_a_day_and_balances`（假仿真器） |
| `apply(SimAction)` 不经 Plan 与 B15 | `ScenarioRunner.apply` | `test_closed_loop_action_changes_later_state` |
| `ScenarioExecutor` 只收 `kind=scenario`；分钟账本走 B08 `compute_ledger`；槽末求和成 `ScenarioSlotSummary`；分表存；逐分钟容量检查；需量 = 槽购电 × 4 | `runner.ScenarioExecutor`、`ledger.py`、`store.py`、迁移 `B17_001_scenario.sql` | `test_storage_action_minute_ledger_slot_summary_and_fork`、反例一、反例二、`test_capacity_violation_recorded_and_trial_infeasible`、`test_executor_only_accepts_scenario_runs` |
| `ScenarioInputProvider`：1 分钟保持取值（性质不变），光伏与气温按小时中点线性插值（derived），原记录不改 | `inputs.py`（`minute_records` 展开 1 分钟记录） | 演示日运行 |
| 分钟级分叉：`forked_from`、`forked_at`，复制快照与分叉前账本，`ForkHook` 交接，父运行在途结果作废，未关闭事件单按新知识重派、`trigger_at = forked_at`、保留 `origin_event_at` | `ScenarioRunner.fork`、`TicketBook`/`TempRuleBook.on_fork`、`Registry.hooks_for("scenario_fork")` | `test_fork_hands_over_temp_rule_and_open_ticket`、`test_fork_redispatch_uses_forked_at_and_keeps_origin` |
| 时间协调器：live 停钟等待、墙钟 120 秒 + 重试一次，失败标受阻且确定性动作照常生效；trial/evaluation 不等墙钟；生效 = `trigger_at + response_budget`；迟到结果只入历史 | `coordinator.py`、`DirectDispatcher.deliver` | `test_delayed_live_result_takes_effect_at_budget_and_matches_deterministic`、`test_late_result_goes_to_history_only` |
| `TrialRunner`：深拷贝状态与随机数、输入冻结，先查硬约束（容量、硬性区域、安全标记）再算软代价，不碰正式时钟与账本 | `runner.TrialRunner` | `test_trial_is_isolated_and_order_independent`、容量用例 |
| 直派器：事件类型 → 专家 → apply | `dispatch.DirectDispatcher`、`experts.DEFAULT_ROUTES` | 演示日与极端场景 |
| 协同组：`--group coordinated` 走值守层，事件单以 RouterStore 为准 | `agent/router/scenario.CoordinatedDispatcher`（经 `DISPATCHER_HOOK` 登记） | `tests/agent/router/test_scenario_wiring.py` |
| 剧本 YAML schema（events、operator_actions 四类、装配引用 B18） | `script.py` | `test_every_script_loads_without_code_changes` |
| 演示日剧本（含次日）+ 极端场景库 8 个，标 synthetic | `configs/scenarios/` | 同上 |
| RunTrace 每分钟状态；`state_series` = 15 分钟全量帧 + 分钟变化；分叉链拼出完整一天 | `build_frame`、`state_from_frames` | `test_state_series_rebuilds_trace_and_exports_day` |
| 一条命令跑完一天；`export-day` 按 `day.schema.json` 写出，提案、Agent 运行与问答沿分叉链合并去重；证据与资产由装配层传入，只导出被引用的资产 | `__main__.py`、`export.py` | `test_day_export_dataplane.py` |
| 同一剧本同一种子两次一致 | 随机数随快照保存 | `test_same_script_same_seed_twice_is_identical` |

## 数据与性质

| 数据 | 来源 | 性质 |
| --- | --- | --- |
| 设备负荷、人流、物料任务 | B18 `synthesize`（D05 一周样本 × 倍数 + 作息合成） | 原记录的性质（derived / synthetic / simulated），到 1 分钟保持取值不改性质 |
| 光伏出力、室外气温 | `fixtures/timeseries/pvgis_2017_excerpt.json`（1 kWp → 1 MWp，三天循环） | derived（小时中点线性插值） |
| 电价 | 江苏 2025 年 426 号文分时时段 + `tariff.yaml` 计价基础（演示值） | configured |
| 剧本事件、极端参数、容量运行限额 | `configs/scenarios/*.yaml` | synthetic / configured |
| 温度、排队、管网压力、储能 SOC | 内置仿真器 | simulated |

## 场景包约定：限值与表计（#238）

B40 医院、B41 政务楼这类场景包接入时，必须提供下面两样东西，展示页和导出才不用按工厂硬编码：

- **限值 `export.config_metrics`**：在包的 `install` 里经 `SCENARIO_HOOK` 往场景注册表登记 `fn(site, policy) -> list[DayMetric]`，数值从本包配置读，读不到就不输出该项。条目固定 `group="config"`、`nature=configured`，B33 计算改善率时排除这一组。工厂包的 6 项：`transformer_limit_pct`、`contract_capacity_kw`、`battery_soc_floor_pct`（能源包）、`hvac_supply_setpoint_c`（暖通包）、`compressed_air_floor_mpa`、`compressed_air_leak_threshold`（设备包）。前四项由能源/暖通包登记，后两项随设备限值一侧登记。
- **关键设备表计清单**：列出展示页“此刻”要读的测点（如 `TX1.P`、`TX1.load_ratio`），写清取数口径与数据性质。`sensor_query`、状态帧与 day.json 都按这份清单提供，不按工厂名写死。

导出侧的数据口径见 [docs/data/day-export.md](../../../docs/data/day-export.md)。`docs/development/12-开发规则.md` 的 DomainPack 一节在 内部设计文档，合并后需要把这套约定同步过去。

## 补过的最简实现（以后由谁加粗）

- **内置仿真器** `sims.py`：设备启停与故障、区域温度一阶模型、电梯排队、空压管网。B20（电梯群控）、B21（温度 RC 模型）、B22（故障注入）实现同名 `Simulator` 后替换 `ScenarioWorld.simulators`。
- **确定性专家** `experts.py`：登记为 `AgentResultSource` 的 deterministic 实现（键为专家名）。B19～B22 各自登记后覆盖。
- **事件单、工单、临时规则** `dispatch.TicketBook`/`TempRuleBook`：B27、B47 的替身，实现 `ForkHook`。B27/B47 用 `registry.register_hook("scenario_fork", hook)` 接入。
- **`SiteInputProvider`**：默认实现读 B18 装配；演示用的完整实现由 B19 提供。
- **改了别人的文件**：`plantkg/core/runs.py`（B04）的 `create_run` 增加 `forked_at`、`energy_end_target_kwh`、`data_sources` 三个可选参数（v0.8 契约字段，原调用不受影响）；`scripts/check_boundaries.py`（B02）把各模块的 `__main__` 视为装配层，场景命令行在这里接电价包与 SQLite，平面内代码只收注入。

## 与任务卡的差异与已知问题

1. 分叉只在当前仿真分钟发生（知识发布总在“现在”），不支持回到过去任意分钟重开。
2. 储能放电按“先弃光再削减放电”裁剪到不超过（负荷 − 光伏），不外送；SOC 裁剪到上下限内，所以分钟账本不会越界。
3. 容量检查覆盖 B18 的两台变压器和剧本 `params.capacity_kw` 里写的节点；B18 的配电柜没有容量字段，配上后自动参与检查。储能与光伏接在 LV1/TX1 下，削不了 TX2 的负载，能源专家会如实说明。
4. 负载超过 `capacity_warn_ratio` 时产生 `sim_limit` 事件（每节点 30 分钟一次；储能已在放电时不重复）。
5. `configs/scenarios/` 在 B02 骨架里也是 `load_settings(scenario=…)` 的配置覆盖层（`python -m plantkg --scenario <名>`）。v0.8 任务卡把这个目录给了剧本；实测 `python -m plantkg --profile mock --scenario factory_demo_day --dry-run` 照常启动（剧本键被配置忽略），但两种用途共用一个目录，是否拆开待 B02 维护人决定。
6. 待业务验收（不阻塞）：仿真参数、容量运行限额与专家规则都是按常理编写的 configured/synthetic 值。
7. 权威入口：`python -m plantkg.world.scenario run` 与 `scripts/nexent_live_scenario.py` 都装 B19 分钟层（`scenario.minute_layer` 钩子），目标线取“能源事件发生前的实际购电”（configured），不跑 15 分钟滚动重解；B19 `run_day` 那条入口用日前计划目标线并在整格边界重解。两条入口共用同一个 `RealtimeController`，演示/评测/录制的能源动作与账本口径以本 CLI/live 入口为准（#188 裁决）。
