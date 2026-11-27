# B18 状态与完成标准

依据 `内部设计文档` 分支 `docs/development/branch-plan.json` 的 B18 任务卡（`feature/factory-site`）。`docs/B任务完成标准.md` 里的“B18”条目沿用旧编号（决策候选与回滚），与本卡不是同一任务，不适用。

## 怎么看效果

```powershell
python -m plantkg.world.site                 # 设备表、首日逐小时人流与分区负荷、变压器峰值
python -m pytest tests/world/site -q
```

换全年数据：先 `python scripts/data/fetch_d05.py --data-dir <仓库外目录>`，再 `python -m plantkg.world.site --d05 <目录>\d05_all.parquet`（可配合修改 `site.yaml` 的 `time_map`）。

## 任务卡要求与证据

| 要求 | 实现 | 证据（`tests/world/site/test_factory_site.py`） |
| --- | --- | --- |
| 中型工厂装配：车间 4 货梯 3 空压、照明、恒温区；12 层行政楼 6 人梯、办公暖通、照明；洁净车间、危化品仓库、数据机房；光伏 1 MWp、储能 2 MWh/500 kW、配电 | `site.yaml` 54 台设备 | `test_equipment_and_floors_match_config` |
| 配电拓扑：变压器 → 区配电柜 → 设备，含容量上限 | `distribution.nodes`，`SiteModel.path`/`transformer_limit_kw` | `test_every_device_reaches_grid_through_a_transformer`、`test_broken_topology_is_rejected`；变压器峰值 TX1 925/1800 kW、TX2 669/1125 kW |
| 人流与作息曲线、生产节拍与物料搬运任务，标 synthetic | `loads.py` | `test_people_flow_has_morning_and_evening_peaks` |
| 设备沿用 B06 entity:// | `SiteModel.entities()` 输出 B01 `Entity` | `test_entities_follow_b06_identity` |
| 光伏储能按新装配同步配置 | `configs/sites/factory/pv.yaml`、`storage.yaml` | `test_pv_and_storage_configs_load_in_b10_b11_format` |
| D05 下载到仓库外并记 SHA-256，仓库只放一周样本与署名 | `scripts/data/fetch_d05.py`；`fixtures/sites/factory/` | 原包 MD5 与 Zenodo 一致；散列见 `fixtures/sites/factory/README.md` |
| D05 输出 B01 契约，原始 measured 并注明外部工厂，UTC、available_at = 时段结束 + 延迟 | `sources/d05.py` `measured_bundle` | `test_raw_d05_records_are_measured_and_marked_external` |
| 时间按 B05 time_map，DST 重复取第一次、缺失标 missing | `source_slot` | `test_dublin_wall_clock_maps_to_same_shanghai_wall_clock`、`test_dst_repeat_keeps_first_and_missing_hour_is_missing` |
| 表计取用：可靠窗口、剔除 24 条故障区间、暖通只用 ex_a/ex_b/ahu_c、不用热泵与光伏表 | `exclusion`、`category_of` | `test_faulty_and_unused_meters_are_excluded` |
| 余项 ≥ 0，分项加余项 = 底座 × 倍数；叶子超总进线的处理写入 transform_steps | `decompose`（改为取较大值，见 IMPLEMENTATION 第 2 条） | `test_residual_nonnegative_and_items_sum_to_base_times_multiplier`、`test_leaf_sum_above_net_import_becomes_base_with_zero_residual` |
| 小样本只含完整日 | `fetch_d05.py sample` 校验 | `test_sample_has_only_complete_dublin_days_after_mains_start` |
| 性质：倍数 configured、缩放后 derived；可追到原表计与转换参数；早班落在厂区早上 | `base_steps`、`to_bundle` | `test_scaled_records_are_derived_and_trace_back_to_d05`、`test_weekday_morning_shift_lands_in_plant_morning` |
| 合成负荷总和与分项一致 | 汇总 | `test_site_total_equals_sum_of_items_zones_and_transformers`、`test_d05_category_split_by_rated_power` |
| HIPE 保留为回退底座 | `sources/hipe.py`，`synthesize(site, hipe_bundle, days)` | `test_hipe_fallback_base_splits_by_configured_shares`（B05 `HipeAdapter` 实际输出） |
| B03 存储兼容：身份一致、同参幂等、不同参数可共存、延迟一致（PR #23 审查） | `to_bundle` 统一身份、`d05.fingerprint` | `test_scene_bundles_write_to_b03_and_read_back`、`test_bundle_ids_change_with_every_effective_parameter`、`test_configured_delay_applies_to_base_devices_and_totals` |
| 验收：一张表列出所有设备、区域和配电路径 | `SiteModel.table()`，`python -m plantkg.world.site` 首段 | 运行输出 |
| 验收：人流曲线能看出早晚高峰 | 同上第二段 | 运行输出：行政楼 07 时 12 人 → 09 时 570 人，18 时 362 → 19 时 70；人梯次数 8 时、12～13 时、18 时三处高峰 |

## 与任务卡的差异（已在 PR 说明）

1. 底座总量取 max(mi_a, 叶子合计)，不是“叶子超出时按比例缩到 mi_a”（发起人 2026-11-04 确认）。
2. D05 功率口径用电能 ×4，总进线均值约 232 kW；卡里的 78/197 kW 是三相平均字段。倍数取 2（发起人确认）。
3. B10/B11 的样例配置和 `configs/plant.yaml` 未改，新规模放 `configs/sites/factory/`；下游切换由 B10/B11/B17 决定。

## 下一步与待业务验收

- 设备额定功率、变压器容量、作息参数都是按常理编写的 configured/synthetic 值，待业务确认。
- 下游（B19～B22、B25）从 `SiteModel` 与 `SiteLoads.to_bundle()` 只读取用；字段增改在本分支。
