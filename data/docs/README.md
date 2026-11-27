# 专家资料（B26）

每个专家一个子目录，文件名与 `nexent_assets/knowledge/catalog.yaml` 里的 `path` 对应（分享已有原件的除外，
例如电价政策 PDF 直接引用 `fixtures/` 下的原件）。资料分三类：
文本（`.md`、`.pdf`）、表格（`.csv`）、图片（`.jpg`/`.png` 加一份同名的 `*.md` 结构化描述）。

数据性质逐份写在清单的 `nature` 字段里，这里只列来源与处理：

| 资料 | 来源 | 性质 |
| --- | --- | --- |
| `energy/js-2025-426.pdf` | 仓库既有 `fixtures/packs/tariff/sources/js-2025-426.pdf`（江苏省发改委 2025 年 426 号文原件），清单直接引用原路径，不复制 | real |
| `energy/pv-output-curve.png` | `nexent_assets/probes/b_bailian/samples/p9_pv_chart.png`，由探针按 PVGIS 摘录数据绘制 | synthetic（数据）/ 图片本身是实测渲染 |
| `equipment/compressor-fan-nameplate.jpg` | `nexent_assets/probes/b_bailian/samples/p9_nameplate.jpg`（Wikimedia Commons，CC0） | real（真实铭牌照片） |
| `equipment/compressor-station-procedure-2024.md` | 按常理编写的《空压站运行规程（旧版，2024）》，泄漏判别阈值与 2025 版手册不同，留给 B30 插曲制造冲突 | synthetic |
| `equipment/compressor-work-orders.csv` | 按常理编写的 11 张历史工单（AC-2 同一现象重复 6 次，另有 AC-1、AIR-BR-P 的重复记录），供工单挖掘 | synthetic |
| 其余规程、手册、点检表、工单、维保记录 | 按常理编写，用于演示检索、引用与资产激活 | synthetic |

图片的 `.md` 是结构化描述：YAML front matter 存字段，正文是中文描述，由 deepseek-flash（`deepseek-v4.1-flash-1017`）
识读图片生成，命令见 `plantkg/knowledge/documents` 的 `--refresh-images`。描述文件与图片一起入库，图片因此可被文本检索到。
