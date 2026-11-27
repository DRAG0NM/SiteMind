# B14 冻结预测装配

B14 使用 B07 的 `TimeSeriesQuery` 读取批准的 `baseline_load` 和 B05 缩放一次的 `pv_ac_power`。以 UTC 15 分钟槽预测，优先使用 24 小时前、其次 48 小时前的可见历史；缺测、suspect 不补零，estimated 保留标识。D-1 18:00 发布 D 日预测时，后六小时通常回退 48 小时。长于可用历史的周预测会报告缺测和覆盖率，不伪造数据。`uncertainty_summary` 包含各量 coverage、missing/fallback 数和 `interval_calibrated=0.0`，仅供 B15 未来保守判断。

## 公共前置及装配

本分支包含 `IntegrationArtifacts`、`prepare_online_inputs` 和 B07 装配所需的公共前置修复。可信预处理按 `docs/architecture/forecasting-prerequisites.md` 写入 B05 扣除明确选定设备表的单来源基线和独立 PVGIS 分区，建立映射及冻结在线记录白名单，并用该映射和参数产物创建 B04 manifest/run。B13 工序只由工序包贡献，不将已扣设备再并入基线。

在启用存储的 `build_services` 之后，可信服务端调用：

```python
from plantkg.decision.forecasting import ForecastService

forecast_service = ForecastService.from_services(services)
# ctx 必须绑定可信 run、该 run 的固定 manifest 和当前 B07 回放时点。
key, bundle = forecast_service.predict_and_save(ctx, horizon)
forecast_ids = tuple(item.forecast_id for item in bundle.forecasts)
# 重新打开服务后可复现；B17 应同时固定 key、两个 ID、run、manifest 和发行时刻。
bundle = forecast_service.load(ctx, key)
# 在 B09 SharedPlanner 构造时注入，request.context 必须使用同一可信 ctx：
forecasts_of = forecast_service.for_planner(ctx, key)
```

`FrozenParameters` 通过 `parameters_for_run` 读取固定 manifest 的参数，并传递版本、配置引用、模型、容量和 k；缺少或损坏参数不回退活动配置。k 仅用于光伏预测，不改写观测或账本；变更需新参数产物和 manifest。参数变化会改变预测 ID 和仓储 key。现有 `FixedParameters` 仅供单元测试。

`ForecastService` 从正式注册表取得 B07 时钟及查询端口；独立仓储默认位于 SQLite 旁 `forecast_artifacts/`。该目录应与 SQLite、时序及公共产物目录一起备份，必须支持硬链接。写入临时文件并独占发布，同一 key 同内容幂等、不同内容冲突，读取校验完整内容摘要。按 key 读取前会验证运行、厂区、固定清单、参数快照、B07 回放时点及发布时间；B09 回调要求两个持久化 forecast ID 与 key 对应。预测不写入 SourceRecord 或 B08 账本。`ForecastStore` 低层 `get` 不应直接暴露给在线调用方。

本地可重运行入口：`python -m pytest tests/forecasting/test_real_integration.py -q`。其中 `test_full_day_real_adapter_history_and_b09` 生成小型、明确标注的合成 HIPE/PVGIS 原始输入，经真实 B05、B03、B04、B07、B14、B09 路径完成 96 槽规划；测试电价来自测试用 tariff 包（当前 main 的 B12 包为占位），不是对真实电价、真实工厂数据或 B15/B17 的验收。另一个集成测试验证不同 k 的预测和规划、重启、备份迁移恢复、读取绑定和损坏拒绝。B17 需使用固定的预测 key/IDs 而非未来真值；B18 可根据偏差准备新候选参数，B14 不负责审批或激活。
