# 冻结来源与参数的运行装配

B04 的 `VersionManifest`、B03 的 `SeriesStore` 和 B07 的 `TimeSeriesQuery` 接口不变。启用存储的 `build_services` 在 `services.version_run.integration_artifacts` 提供不可变产物仓储。文件位于 SQLite 数据库旁的 `integration_artifacts/`，相对部署数据目录可迁移；每个文件以规范 JSON 的 SHA-256 命名，经临时文件、flush/fsync 和不覆盖硬链接发布，再登记 B03 的 `storage_artifacts`。部署文件系统必须支持硬链接。读取重查厂区目录登记、版本、内容哈希；来源映射还检查所引用的 B03 Parquet、来源身份与量。应一同备份 SQLite、Parquet 和此目录。

可信装配代码在创建运行前取得已归一化的总表、设备表、PVGIS bundle 和经批准的设备选择。下面的选择函数只是说明调用方式：实际调用方必须依据业务批准记录选择已知历史，不能让模型提供批准名单；输入原始表计只供可信预处理，不加入在线输入。

```python
from plantkg.adapters.baseload import selected_device_baseload
from plantkg.world.replay.input_assembly import prepare_online_inputs

artifacts = services.version_run.integration_artifacts
source_map, inputs, baseline = prepare_online_inputs(
    services.storage.series, artifacts, total_bundle, selected_device_bundles,
    frozen_meter_names, pv_bundle, selected_device_baseload,
    approved_baseline=lambda bundle: approved_baseline_records,
    approved_pv=lambda bundle: approved_pv_records,
    as_of=ctx.as_of, basis="approved meter selection and PV input revision",
)
parameters = artifacts.prepare_parameters(
    pv_capacity_kw=100.0, pv_correction_k=1.0,
    model_version="persistence-1", parameter_version="parameters-1",
    configuration_ref="plant-configuration-1",
)
manifest = services.version_run.versions.prepare(
    {"data_sources": source_map, "parameters": parameters}, "prepared inputs")
services.version_run.versions.check_integrity(manifest.version_manifest_id)
run_ctx = ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id})
run = services.version_run.runs.create_run(run_ctx, "replay_day", clock, inputs=inputs)
```

批准函数仅返回对应 bundle 中明确批准、在 `ctx.as_of` 已可见且时段已结束的记录。装配会验证选择、来源身份，调用 B05 扣减一次，保存两个独立分区及量到来源的映射。`clock.start` 不能早于批准截止。缺测保持缺测；PVGIS 的装机缩放已由 B05 完成，工序负荷由 B13 工序包贡献，不能再把总表或已扣表计加回基线。

在线读取使用注册的 `TimeSeriesQuery`，带运行和固定清单的可信 context 分别查询 `baseline_load` 和 `pv_ac_power`。B07 仍负责唯一时钟、分页、可见时间和记录哈希，未批准记录不会出现。后续 B14 可调用 `artifacts.parameters_for_run(run_context, versions, runs)`，将结果映射到其参数快照；此函数不访问当前活动配置。更改 k 应准备新的参数产物和清单，旧运行仍指向旧产物。`k` 不修改历史观测或账本。

B04 校验及激活时验证新产物及映射的底层分区；查询和参数解析再次检查文件。旧 `series:v1:` 清单无需迁移。缺少组件、跨厂区、未验证清单、未登记或损坏文件均不回退最新版本。

## 公共前置提交的历史验证

- `python -m pytest tests/replay/test_integration_artifacts.py -q`：12 passed，0 failed；其中 `test_real_b05_hipe_and_pvgis_to_b07` 使用 B05 真实适配器、SQLite、Parquet、B04 和 B07，`test_real_dual_source_replay_and_frozen_parameters_restart` 验证分页、隔离、版本切换和重启。
- `python -m pytest tests/storage tests/versions tests/replay tests/adapters -q`：132 passed，0 failed。
- `python -m pytest -q`：708 passed，0 failed。
- `python contracts/build.py --check`、`python scripts/check_boundaries.py` 和新增 Python 文件的 `pyflakes` 检查通过。

以上为公共前置提交当时的独立检查；B14 集成验证见 `fixtures/forecasting/README.md` 与集成测试。
