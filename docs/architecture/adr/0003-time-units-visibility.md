# ADR-0003 时间、单位、数据性质与可见性

- 状态：已决（03 §2.2/§2.3，09 D28）；B01 冻结
- 影响分支：全部；重点 B05、B07、B12、B14、B21、B22、B25、B26

## 决定

**时间**

- 内部时间一律 UTC：契约类型 `UtcDatetime` 拒绝无时区或非零偏移的时间（`+08:00` 也拒绝），原时区另存 `source_tz`。
- 区间左闭右开 `[start, end)`（`Interval`），`end > start`。日前网格 15 分钟，厂区日 96 个时段。
- `time_map` 按当地钟面时间对齐再转 UTC，不按 UTC 平移（否则德国早 8 点落到上海 14 点）。夏令时重复的一小时只保留第一次（`dst_repeat_policy=keep_first`），丢弃的电量写进 `transform_steps`。

**单位与空值**

- 功率 kW、能量 kWh、SOC 0～1（`Unit.fraction` 记为 `"1"`）、费用 CNY、电价 CNY/kWh。`Unit` 是封闭枚举，新增单位是契约变更；原始单位存 `source_unit`，换算步骤存 `transform_steps`。
- `value=None` 表示缺测，必须标 `quality.flag=missing/suspect`；空值不能当 0。缺价不当 0，进待处理清单（`PendingIssueReason.missing_price`）。

**数据性质** `DataNature`：`measured`、`derived`、`configured`、`synthetic`、`simulated`。预测单独用 `Forecast` 表示，性质固定为 `derived`，不能当 `measured`。负荷放大倍数、政策生效日映射等标 `configured`。

**两条时间轴**

- 可见性：`available_at ≤ as_of`（规划时是否已知）。实测数据 `available_at = 时段结束 + 采集延迟`，契约校验 `measured` 记录不早于时段结束。规则的 `available_at` 取来源资料的 `available_at`；复核、发布发生在现实时间，只入审计，不参与可见性。
- 适用性：`effective_from ≤ 时段开始 < effective_to`。已发布但未生效的新政策在规划时可见，只作用于生效后的时段（`Rule.is_visible` / `Rule.applies_to`）。
- 预测输入只用 `available_at ≤ issued_at` 的数据，`issued_at ≤ target.start`（契约校验）；持久性预测取 24 小时前，D 日 18:00～24:00 尚不可见的时段取 48 小时前。

**标识**

- 资料版本 `asset://{行业}/{内容哈希前8位}/{类型}/{定位}`，契约校验前 8 位与完整 `content_hash` 一致；去重与完整性用完整 sha256。
- 稳定实体 `entity://{厂区}/{类型}/{编号}`，不随资料修改而变。
- 配置引用 `config://{文件}#{键路径}`。

## 后果

所有查询（时序、图谱、知识库、规则）都按 Context 的 `as_of` 过滤；只读结果与缓存键包含清单与 as_of。
