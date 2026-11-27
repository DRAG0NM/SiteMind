# 工厂装配小样本（B18）

## d05_week.parquet

D05 工厂设备级用电的一周切片：爱尔兰当地 2025-06-04（周三）至 2025-06-10，共 7 个完整日，总进线 `mi_a` 每天 96 个 15 分钟时段，36 块表、24192 行。**这是爱尔兰一家约 150 人连续生产工厂的实测，不是本厂数据。**

| 列 | 含义 |
| --- | --- |
| `asset_id`、`asset_type` | 原包 `metadata/AssetList.csv` |
| `time` | 窗口起点 UTC（原包 `window_start_utc`） |
| `energy_kwh` | `Energy_kWh_15m`，ALL 相（三相合计）；×4 即平均 kW |
| `is_reliable_window` | `IsReliableWindow` |

schema 元数据 `d05` 带 43 块表的资产类型、24 条计量故障记录（原包 `metadata/meter_data_quality_log.csv`，按行序编号，时间按 UTC 解析）、引用与许可。

- 本文件 SHA-256：`6a6d485707a99dff9fa0f70baa4e49b74cf9d05956970d660cae1c8b103355fa`
- 由仓库外全年窄表 `d05_all.parquet`（SHA-256 `9279dbf350f4decdd355ac8cd4fecca82246c9753fbbb7d984f02875b27ae419`，1,039,873 行）切出
- 全年窄表由 Zenodo 原包转换：`d05_zenodo.zip`，218,425,056 字节，MD5 `acc03fa5cb4b9c54a426440fffe49e74`（与 Zenodo 公布值一致），SHA-256 `710f09609955f6037c2784d18640201bd47669db06d556f5ef23403e0d56fd2a`

重新生成：

```powershell
python scripts/data/fetch_d05.py --data-dir D:\data\d05 --sample-out fixtures/sites/factory/d05_week.parquet
```

## 署名

Flynn et al. (2026). *Industrial Asset-Level Electrical Energy Dataset from a Manufacturing Facility (15-Minute Aggregates)*, v1.0. Zenodo. https://doi.org/10.5281/zenodo.19180972 ；论文：*Data* 2026, 11, 101, https://doi.org/10.3390/data11050101 。许可 CC BY 4.0（https://creativecommons.org/licenses/by/4.0/）。本仓库只做了格式转换与切片，未改动数值。
