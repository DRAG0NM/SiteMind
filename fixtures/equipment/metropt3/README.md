# MetroPT-3 小样本（B22）

两个 10 秒采样窗口，只用于设备监测专家的泄漏研判演示与冒烟测试：

| 窗口 | 文件 | 时段 | 用途 |
| --- | --- | --- | --- |
| 泄漏 | `leak_2020-05-29_2330_to_2020-05-30_0600.csv` | 2020-05-29 23:30～05-30 06:00 | 官方第 2 次空气泄漏 |
| 对照 | `baseline_2020-05-27_2330_to_2020-05-28_0600.csv` | 2020-05-27 23:30～05-28 06:00 | 前两晚同一时段，夜间对夜间 |

官方统计：运转占比（电机电流 > 2 A）约 23% → 100%，电机电流均值约 1.0 → 5.6 A，油温约 65 → 76 ℃。
白天正常运转占比约 50～70%，研判必须与同时段基线比，不能拿夜间基线判白天。07-15 泄漏前几小时
运转占比已近 100%，不作对照。

## 当前文件是仿写的

`manifest.json` 里 `nature: synthetic`，每个 CSV 首行也标了 `nature=synthetic`：按上述公开统计仿写，
**不是 MetroPT-3 原始读数**。跑下面的命令会下载原始包到仓库外、记 SHA-256，并用真实切片替换这两个文件
（`nature: measured`）：

```powershell
python scripts/data/fetch_metropt3.py --data-dir D:\data\metropt3
```

列：`timestamp`、`motor_current`（A）、`oil_temperature`（℃）、`reservoir_pressure_bar`（bar）、
`compressor_on`（电流 > 2 A 判为运转）。读取逻辑在 `plantkg/world/sim/faults/metropt3.py`，不区分
synthetic / measured。

## 署名

Davari, N., Veloso, B., Ribeiro, R., & Gama, J. (2021). *MetroPT-3 Dataset* [Dataset]. UCI Machine Learning
Repository. https://doi.org/10.24432/C5VW3R 。许可 CC BY 4.0。数据来自地铁空压机 APU，不是本厂设备。
