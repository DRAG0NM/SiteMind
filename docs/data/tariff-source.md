# 分时电价来源与口径（B12）

## 选定的政策

| 版本 | 文件 | 状态 | 依据 |
| --- | --- | --- | --- |
| 新版 | 江苏省发改委《关于优化工商业分时电价结构促进新能源消纳降低企业用电成本支持经济社会发展的通知》（苏发改价格发〔2025〕426号），2025-04-25 落款、04-28 印发，2025-06-01 起执行 | `approved`：对照原件 PDF（`fixtures/packs/tariff/sources/js-2025-426.pdf`）逐条核对一致，2026-10-31 由 DRAG0NM 复核确认 | `fixtures/packs/tariff/jiangsu_2025_426.yaml` |
| 旧版 | 426 号之前执行的江苏工商业分时电价 | `pending`：只有媒体转述，文号、生效日、尖峰未找到原文 | `fixtures/packs/tariff/jiangsu_pre2025.yaml` |

两版都登记成规则，供 B27/B30 做新旧对照；`pending` 的规则规划器以 `not_reviewed` 拒绝编译。复核人对照原件逐条核对后，填
`reviewer`、`review_note` 并改为 `approved`，才能用于正式闭环验收。

新版要点（表1、表3，两部制 315 千伏安及以上工业）：

| 季节 | 峰 | 平 | 谷 | 尖峰 |
| --- | --- | --- | --- | --- |
| 夏冬（6-8、12-2 月） | 14:00-22:00 | 6:00-11:00、13:00-14:00、22:00-24:00 | 0:00-6:00、11:00-13:00 | 7-8 月 14:00-15:00、19:30-21:30；12-1 月 18:00-20:00 |
| 春秋（3-5、9-11 月） | 15:00-22:00 | 6:00-10:00、14:00-15:00、22:00-次日 2:00 | 2:00-6:00、10:00-14:00 | — |

平段 = 用户购电价格；峰段上浮 80%，谷段下浮 65%；尖峰在峰段基础上再上浮 20%。深谷（春节、五一、国庆 10:00-14:00，在谷段基础上下浮 20%）
按国务院办公厅放假通知登记节日日期（`dates`），登记到 `through: 2026-12-31`；晚于 `through` 的时段拒绝定价，新一年放假通知发布后补登。
深谷价 = 谷段价 × 0.8，不论该时刻原属哪个时段（春节在冬季，10-11、13-14 原属平段，也按谷段价 × 0.8）。

旧版（媒体转述）：峰 8:00-11:00、17:00-22:00，平 11:00-17:00、22:00-24:00，谷 0:00-8:00；两部制峰段上浮 72%、谷段下浮 58%，
计价基础为到户电价（新版改为用户购电价格，两版的基础不能混用）。

## 口径

- 价格 = 计价基础（元/kWh，按当地年月）× 规则系数。规则 `tariff_tou` 是一份完整时段表：`value` 为平段系数，`condition.periods`
  每项有月份（深谷为节日日期 `dates` 与登记截止 `through`）、当地时间窗口（起点晚于终点即跨午夜）、浮动比例和原文定位。计价基础不在政策原文里（按月公布），作为厂区受信配置
  `fixtures/packs/tariff/tariff.yaml#tariff.base_prices` 单独登记。
- 编译时整表校验：每个月 24 小时被峰/平/谷恰好覆盖一次，尖峰落在峰段内（深谷不要求落在谷段内），系数非负；不合格分别返回 `missing_price`、`conflict`、
  `negative_price`。对象不是本厂区接入点、单位不是 `1`、用电类别与本厂区不同，分别返回 `subject_unknown`、`unit_unknown`、`conflict`。
- 时段按开始时刻决定生效版本；时段内的当地分钟必须同属一个电价时段，否则判时段网格与电价表不对齐。任何时段无规则覆盖、两版同时
  覆盖、所在月份未定义、缺当月计价基础，都拒绝建模（不当 0 元，不任取一版）。
- 模型：每时段 `tariff__energy_cost[t] = 价格 × 购电 × Δt`，约束 `tariff__price_{规则ID}` 带规则与配置 Ref，目标项 `tariff.energy_cost`。
- 账本：`TariffPack.prices(rules, time_set)` 给出逐时段价格与对应规则 ID，填进 B08 `SlotInput.price_cny_per_kwh`；与模型同一个函数。
- 只核算分时电度电费。`cost_report` 按时段名汇总电量与电费，峰值购电功率单列为指标，不当作需量/容量电费。

## 演示配置

`fixtures/packs/tariff/demo_tou.yaml` 顶层 `demo: true`，两条规则分别照搬旧版、新版时段结构，生效日映射进回放窗口（03 §2.3）：
`demo-tou-old` 到当地 2026-07-02 00:00 为止，`demo-tou-new` 自 07-02 起生效、06-25 起可见。计价基础 0.62 元/kWh 是演示值
（`demo_base_price: true`）。`acceptance_blockers(rules, demo_base_price)` 对演示规则、非 `approved` 规则和演示计价基础都给出拦截项，
正式闭环验收前必须为空。

```python
from plantkg.packs.tariff import load_tariff_config, load_tariff_rules
tariff, demo_base = load_tariff_config(Path("fixtures/packs/tariff/tariff.yaml"), "fixtures/packs/tariff/tariff.yaml")
rules = load_tariff_rules(Path("fixtures/packs/tariff/demo_tou.yaml"), "fixtures/packs/tariff/demo_tou.yaml")
planner = SharedPlanner({"tariff": tariff, ...}, ..., rules_of=...)
```

## 待交接

- 正式验收前：补旧版文号与生效日、登记当月代理购电价格公告里的用户购电价格（按电压等级、两部制）。
- `configs/plant.yaml` 的 `tariff: {source: null}` 不归本分支，选定来源后由 B02 维护人改为指向本文件。
- B09 `check_built`：规则生效区间与规划区间相交、却不含任何时段起点时（如生效时刻 10:50 落在 15 分钟网格中间），本包对它的约束全部跳过，
  B09 判 error。电价版本通常在当地零点或月初切换，与网格对齐；需要支持非对齐生效时刻时由 B09 改筛选口径（B10 已记同一问题）。
- 负电价：系数校验为非负，负电价口径仍待 B08/B12 共同确定。
- B25 抽取的电价规则也走 `tariff_tou` 与同一编译器，不另建定价路径；抽取结果须给出完整时段表，单个时段的规则无法单独编译。
