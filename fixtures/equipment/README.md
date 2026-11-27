# 设备监测小样本（B22）

| 文件 | 内容 | 性质 |
| --- | --- | --- |
| `metropt3/` | MetroPT-3 泄漏与对照两个 10 秒采样窗口、manifest、署名 | 当前 synthetic（按公开统计仿写），跑 `scripts/data/fetch_metropt3.py` 后替换为 measured |
| `d05_metering_anomalies.yaml` | D05 原包 `meter_data_quality_log.csv` 的 24 条计量故障，按行序编号、时间原样 | derived（来自 D05 measured） |

MetroPT-3 是地铁空压机 APU 的实测数据（UCI 791，CC BY 4.0），不是本厂设备；D05 是爱尔兰一家工厂的
设备级用电（Zenodo 19180972，CC BY 4.0），也不是本厂。两者只提供信号形状、事件素材与统计口径，
本厂设备的数值由装配与仿真给出（configured / simulated）。

署名与许可见各子目录的 README / manifest。
