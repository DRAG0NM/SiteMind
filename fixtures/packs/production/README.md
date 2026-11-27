# production 包样例（B13）

| 文件 | 内容 |
| --- | --- |
| `operations.yaml` | 工序配置：JOB1 额定 150 kW（configured），未优化开始 当地 09:45 |
| `vacuum_pump1_run_20260701.json` | HIPE VacuumPump1 一次完整运行（12 个 15 分钟时段），峰值归一的形状与来源哈希、行号 |
| `hipe_day_20260701.json` | 当日 MainTerminal 与 VacuumPump1 的 B05 configured 视图（×25），供平移替换与 Q25 口径测试 |

数据为 HIPE v1.0.1（CC BY 4.0，德国 2017-10-23）经 B05 `HipeAdapter` 按当地钟面映射到 2026-07-01。工序与设备的对应、额定功率是配置，
HIPE 只给形状，不是本厂同年实测；这句说明以 `config://…#provenance_note` 挂在每个工序动作的 refs 上。

## 用法

```python
from plantkg.packs.production import ProductionPack, load_operations
specs = load_operations(Path("fixtures/packs/production/operations.yaml"), "fixtures/packs/production/operations.yaml")
planner = SharedPlanner({"production": ProductionPack(specs), ...}, ...)
```

- 窗口规则 `operation_window`：`quantity=operation_start`、`unit=h`、`bounds={lower, upper}` 为当地钟面小时（可带 `condition.tz`，默认 Asia/Shanghai）。
  没有窗口规则的工序固定在原开始时段，即 B0/B1 口径。
- 计划动作：`operation_start`（count，开始时段）与逐时段 `operation_power`（kW），两者与 `load_profile_for_start` 一致。
- 执行记账：`shifted_total(总表, 设备表, spec, 区间, 新开始)` 先查对齐、倍数、缺测、设备不超总表，再调 B08 `replace_shifted_load`。
- 预测口径（07 Q25）：base_load = 总进线 − 被选作形状的设备表（B05 `selected_device_baseload`），工序负荷只由本包贡献。
- 多工序（B28）：实现 `OperationExtension`，放在 `plantkg/packs/production/multi/`，经 `ProductionPack(extensions=...)` 接入。

## 样例结果（2026-10-31 本机）

真实当日 base_load（总进线 − VacuumPump1，均值约 475 kW，峰值约 1048 kW；首段缺测按相邻时段保持），B09 演示电价与光伏、储能，窗口 08:00～18:00：

| 光伏 | 储能 | 不平移（元） | 窗口平移（元） | 节省 | 新开始 |
| --- | --- | --- | --- | --- | --- |
| 否 | 否 | 8419.25 | 8273.48 | 145.77 | 14:00 |
| 否 | 是 | 7711.81 | 7566.05 | 145.77 | 14:00 |
| 是 | 否 | 4736.22 | 4590.45 | 145.77 | 14:00 |
| 是 | 是 | 4035.22 | 3940.12 | 95.10 | 14:00 |

窗口 08:00～10:00 放不下 3 小时工序：返回 infeasible，冲突线索指向 `production__window_job1` 与 `rule-job1-window`。
窗口恰为原运行 09:45～12:45：optimal，开始时段不变，费用与不平移相同。
