# B05 时序来源、适配器与验收说明

本文件记录来源核对、实现设计与真实数据验收；验收细节以本文末尾及机器可读报告为准。原始文件保存在仓库外，仓库只保留可追溯的小切片及清单。不得将两个不同年份和地区的数据称为同一厂区同时实测。

## 仓库与边界

基线 main 和目标分支均为 `5c465c09f5a5fa5e90faa407b165f02a00e43444`；B01 合并提交 `1f34a1c`、B02 合并提交 `ab2465f` 均为基线祖先。内部设计文档 位于 `82a885da661e5e378606632c3c176bd400431848`，仅作规划参照。实际 Adapter 接口见 `plantkg/core/ports.py`。B05 拥有 adapters、tests/adapters、fixtures/timeseries 与本文件；共享契约及配置需协调。

用户已确认厂区时区 Asia/Shanghai、负荷因子默认 25，1 表示原始量级。负荷放大视图标 configured，原始德国 HIPE 标 measured，不称南京实测。光伏和储能容量、设备参数均为配置候选；现有 plant.yaml 是示意，不代表最终设备选型。旧规划 B10 描述装机缩放与本次手册 B05 缩放一次存在差异，后续对接需同步；本轮不修改共享文件。

## PVGIS 来源

PVGIS 5.3 seriescalc 请求南京代表点 32.06, 118.80，PVGIS-ERA5，2017，固定式倾角 30°、朝南、损耗 14%、峰值功率 1 kWp。它们是场景假设，不是实际厂址或设备配置。原始 JSON 与完整参数、响应元数据保存在仓库外，文件信息见 `fixtures/timeseries/source_manifest.json`。该 JSON 含 8760 条小时记录，`P` 的元数据单位为 W，源装机 1 kWp，小时记录时间从 20170101:0030 至 20171231:2330，输出元数据标示 hourly averages。

JRC 手册说明 ERA5 的辐照度是小时平均且在 HH:30 标记；对于功率的区间对齐仍应在设计阶段结合接口和边界用例明确并验证。不能将小时展开称为新的 15 分钟实测。原始小切片 `fixtures/timeseries/pvgis_2017_excerpt.json` 保存 JSON 的零基数组索引及源文件 SHA256。

来源及条款：https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/pvgis-5-user-manual_en ；https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/general-information/usage-conditions-data-protection_en 。

## HIPE 来源

KIT HIPE v1.0.1 cleaned 一周包，2017-10-23 至 2017-10-30（截止日不含），CC BY 4.0，引用 Bischof 等人 HIPE、DOI 10.1145/3208903.3210278。官方记录 https://zenodo.org/records/14054902 。仓库内的既有 D1 探针是历史记录，并非本轮实源完整验收；该探针按采样起点直接归桶，跨 15 分钟边界时未拆分，不能作为严格能量守恒的最终算法。

## 待完成

以下保留第二轮设计供追溯，当前实现与验收结果见文末；数据来源与样例状态以清单为准。

现行契约允许 `value=null` 并要求 quality 为 missing 或 suspect；其 `available_at >= interval.end` 校验仅针对 measured。适配器为 derived 和 configured 的历史观测主动计算并检查可见时间。

本轮下载的一周 ZIP 为 109297827 字节，SHA256 `a04344f7edf0593d9c35db68e606babfd77d28522ec0dc37ee081516836bbe2b`，与仓库历史探针记录相同；MD5 `535683e5c98f534e5b6d6f286dbe6993` 与 Zenodo 公布值相同。ZIP 中有 11 个 CSV，清单记录每个成员的 SHA256。直接查看文件确认 `SensorDateTime`、`Machine`、`P_kW`，且两相和三相的列结构不同。总表及 VacuumPump1 均实际发现 2017-10-29 02 时的 +02、+01 数据。`hipe_excerpt.json` 只抽取这三个字段，保存原 CSV 文件名及一基行号；`synthetic_edge_cases.json` 是明确标注的人工样例。原 ZIP 保留在清单标注的仓库外路径，无需为读取小切片解压全量文件。


## 第二轮设计结论

### 职责和模块

以现有 `Adapter.normalize(source_ref, mapping, visibility) -> SourceBundle` 为唯一公开入口；只产生 B01 允许的 `SourceRecord`，不扩展字段、枚举或修改 B01。以下是第二轮的历史拟定模块，供追溯，并非实际文件清单：

- `plantkg/adapters/hipe.py`：按列名读总表及设备表，校验 Machine、原始偏移和 P_kW，识别重复与缺失，保留原始 1 倍 measured 路径。
- `plantkg/adapters/pvgis.py`：读取原始 seriescalc JSON 和响应元数据，校验 W、源装机、输入参数和时间约定；以 B05 执行 W→kW 和目标装机比例，生成目标装机的 derived kW。
- `plantkg/adapters/resample.py`：源区间裁切、跨桶拆分、时间加权、覆盖与缺失规则；不负责再次缩放。
- `plantkg/adapters/time_mapping.py`：各源各自的源起始日、本地钟面到南京的映射、DST 及来源区间到回放区间的映射；不将源 UTC 直接当南京当地时间。
- `plantkg/adapters/provenance.py` 与 `plantkg/adapters/local.py`：原文件定位、哈希、转换版本和仓库外路径加载；固定数据与清单分离。`source_ref` 指向本地文件或本地 manifest 条目；不自动联网，路径可配置。
- `tests/adapters/`：下文设计的契约、边界、守恒、可见性、溯源和重复缩放测试；`fixtures/timeseries/` 放小型样本。`docs/data/timeseries.md` 为说明和交接。

输入的 `mapping` 应显式包含 source_id、source_version、mapping_version、source_start、plant_start、source_tz、plant_tz、粒度和使用的策略版本；PVGIS 另包含 source_peak_kwp 与 target_peak_kwp，HIPE 另包含 load_scale.factor。`visibility` 提供非负采集延迟及必要的可见时间语义。拒绝缺失、非法、NaN/无限值及不能解释的输入；默认仅在本分支的适配器配置层提供，不擅改 `configs/plant.yaml` 或共享配置模型。后续实现时确认配置归属及路径，与维护者协作；本设计不声称已获得该协调。

实际实现文件为 `plantkg/adapters/__init__.py`、`common.py`、`hipe.py`、`pvgis.py` 和 `baseload.py`；时间映射、溯源和重采样逻辑分别包含在现有模块中。

### PVGIS 功率区间核验与假设

证据：原始 JSON 的 `meta.outputs.hourly.timestamp` 为 `hourly averages`，`P` 字段描述为 `PV system power`、单位 W，但没有独立给出 P 的区间起止。JRC 的小时数据页面将 `P` 描述为光伏输出功率、时间为 UTC；其用户手册将 ERA5 辐照度说明为对应一小时的平均值，标记在 HH:30。官方材料没有在已核对文字中单独明确计算出的 P 是该小时的严格积分均值，也未给出单独的 P 区间定义。来源：

- https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/pvgis-5-tools/hourly-radiation_en
- https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/pvgis-5-user-manual_en

**实现假设 `pvgis-era5-p-centered-hour-v1`**：仅针对已核验 PVGIS 5.3 / ERA5 / HH:30 输出，将时间戳 `t` 视为 P 的代表性中心时刻，定义源区间 `[t - 30 min, t + 30 min)`，按该值为小时平均功率近似处理，均匀分配到四个 15 分钟子区间。这个假设受到输出级 hourly averages 提示和 ERA5 辐照度时间约定支持，但不等于官方已单独证明 P 的积分均值；需在转换步骤、文档和结果中标明。任何其他数据库、非 HH:30 时间或元数据不匹配时拒绝套用，等待显式支持；不直接向下取整。保留 source_resolution=1h 与假设版本，不称为真实 15 分钟观测。若正式用途需要严格物理平均功率，先进一步核实或替换来源。

真实响应跨午夜核对（原 JSON 数组索引从 0 开始；例子中的区间是上述假设，不是官方逐条给出的边界）：

| 索引 | 源 UTC 时间 `t` | P W | 假设源 UTC 区间 | 源南京本地对应区间 |
| --- | --- | --- | --- | --- |
| 4335 | 2017-06-30 15:30 | 0 | [15:00, 16:00) | 2017-06-30 [23:00, 24:00) |
| 4336 | 2017-06-30 16:30 | 0 | [16:00, 17:00) | 2017-07-01 [00:00, 01:00) |
| 4343 | 2017-06-30 23:30 | 40.22 | [23:00, 24:00) | 2017-07-01 [07:00, 08:00) |
| 4344 | 2017-07-01 00:30 | 48.21 | [00:00, 01:00) | 2017-07-01 [08:00, 09:00) |

索引 4335 与 4336 在南京本地午夜相接；4343 与 4344 在 UTC 午夜相接，但均在南京 7 月 1 日。该检查证明日期换算和半开边界可一致划分，**不能证明源 P 的真实积分窗口**。例如 4344 的 48.21 W、源 1 kWp、目标 500 kWp：B05 输出每个 15 分钟段 24.105 kW，四段能量合计 24.105 kWh；这些数字只在上述假设和候选装机下成立。真实记录见 `pvgis_2017_excerpt.json`。

### 时区、DST 与重采样

- 原始时间先解析为真实带偏移的时刻；HIPE 偏移需符合 Europe/Berlin 的当地时区规则，源 UTC、原字符串和偏移均记录。处理源上的间隔、重复及缺测，再按各自 source_start 的当地日期与钟面位置映射到 plant_start 和 Asia/Shanghai，最后转换到 UTC。PVGIS 首先将其 UTC 区间转南京当地时间，再按自己的 source_start 映射；不能沿用 HIPE 的源日期。
- HIPE 2017-10-29 02:00–03:00 有两个不同 UTC 区间。按时间顺序与原偏移识别，保留 +02 的第一次，丢弃 +01 的第二次；先在源时轴按偏移和转换边界切段，不让保持区间跨入被舍弃的重复小时。丢弃范围和来源写入 provenance，不把重复样本误作普通去重。转换参数里三个字段口径不同：`dropped_repeat_segments` 是丢弃的采样段数，`dropped_repeat_intervals` 是合并相邻段后的源 UTC 区间与一基行号范围，`dropped_energy_kwh` 是这些段按源功率 × 有效秒数算出的电量。超出数据覆盖的边缘标缺失。映射后的每个完整南京日期生成 96 个左闭右开 15 分钟槽，缺失槽值为 null；不补造或插值。
- 每个 HIPE 有效采样区间为 `[t_i, min(t_{i+1}, t_i+60s))`，末点无后继默认不延伸；有效值跨 15 分钟边界按交集分割再计算 `kWh = Σ(P_kW × 有效秒数 / 3600)`。已知有效段平均功率为 `Σ(P × 有效秒数) / Σ有效秒数`，同时记录覆盖秒数；不把缺失秒数除入有效均值。默认不填补，只有完整覆盖的槽输出正常平均功率；覆盖不足输出 null/missing 并记录部分覆盖和可计算的已知能量。允许阈值或填补须显式配置与版本化，不能伪装为实测。
- 同一设备同一原始时刻的完全相同行可去重并记录；冲突功率或设备标识不一致则报错并定位来源，禁止叠加。不同设备不因时间相同去重。真实零值与缺失分开处理。按共同有效覆盖区间核验重采样前后的能量守恒。

### 可见性、缩放与计量

- 原始瞬时 HIPE 输入的已知时间以实际采集语义为准；经聚合形成回放观测后，其映射时段的 `available_at` 至少为映射时段结束加非负延迟，并不早于参与输入中最晚的映射可见时间。源实际时间及源可见时间和映射规则分别保留；对重映射数据不能直接拿 2017 年绝对时间当 2026 年可见性。延迟默认为 0，但允许明确配置；不允许负值。
- PVGIS P 作为回放观测：先计算源完整小时结束后的可见时间，将其与源区间一起映射到目标回放时钟；四个 15 分钟子段全部继承同一完整小时的目标 `available_at`，而非各自子段结束。记录源真实 UTC 与映射 UTC 供追踪。若一个输出依赖多个输入，其可见时间为所有依赖的映射可见时间最大值，并不得早于输出相关完整源时段结束。`measured`、回放观察的 `derived`、从观察缩放得到的 `configured` 一视同仁；把因果依赖视作上界，缩放不重置、不提前。B01 只自动校验 measured；第三轮由适配器主动校验其余性质，不改 B01。
- HIPE 每次从原始 1 倍数据计算负荷配置视图：默认 factor=25，必须为有限正数；1 倍保持 measured，25 倍标 configured，并记录因子及版本。切换 25→1→25 应从同一原始输入重算，回到同样结果，不能产生 625 倍。总表和候选分表使用一致因子；设备额定功率不自动再乘 25。
- **PVGIS 装机缩放只由 B05 执行**：`P_target_kW = P_source_W / 1000 × target_peak_kwp / source_peak_kwp`，源与目标装机必须显式、有限且大于 0，缺失拒绝；目标输出为 derived kW，转换记录含源 1 kWp、目标装机、比例、版本、单位和输入指纹。B10 只消费目标装机的 kW，禁止再次缩放。旧规划中 B10 执行缩放的文字登记为待同步事项，不改 内部设计文档。
- 优先保留总表和分表原曲线；仅对明确选作候选的原设备扣除一次，替换后再加入对应曲线。其他设备不扣；未知、缺测、不一致和负余量均显式报告，不能据此编造实际工单。Q25 最终业务口径由 B13/B14 确认，B08 统一替换和账本验证。

### 契约和溯源

每条 `SourceRecord` 的 `content_hash` 使用对应原文件的完整 sha256（HIPE 为 ZIP 成员的解压后文件 hash，PVGIS 为原 JSON hash），`locator` 使用源 CSV 一基行号或原始时间区间及表计；JSON 数组索引、ZIP 成员名、多个贡献来源及其行范围/哈希放 `TransformStep.params`。聚合有多个来源时，逐条列明全部输入指纹、版本、定位和所占有效时间，不能只保留首个来源；若超出实践可承载规模，先登记契约/存储前置问题，不能偷偷截断。`event_time` 设计为目标时段起点，`interval` 为目标 UTC 左闭右开区间，`source_tz`/`source_unit` 为原始语义，`unit` 为标准 kW；可见时间、性质、质量按上文填写。映射前后时间、原始分辨率、覆盖、假设、参数、因子和转换版本放转换步骤；任何空值必须标 missing 或 suspect。

### 容量候选与配置归属

南京坐标、时区、光伏 500 kWp、储能 1000 kWh/250 kW 作为 configured 候选进行量级核验；最终容量未批准。HIPE factor=25 已确定，不再作为待确认项。第三轮提供参数校验和转换，并准备不重复缩放的核验样例；不得将候选容量写成已批准设备或凭未经计算的原始探针量级推导结论。共享 `configs/plant.yaml` 及公共配置模型由维护者协调，不因第二轮设计直接修改。

### 第三轮验证方案与实施清单

1. 用真实切片核验 UTC/南京午夜的四个源索引、映射后各完整天 96 槽及不同 source_start；验证端点恰好相接且不重复。PVGIS 假设需要有版本断言，非 ERA5/非 HH:30 拒绝。
2. DST 用两种偏移验证 keep_first，检查丢弃区间、交界处切分、源定位及 96 槽；不是将两个 UTC 时刻按字符串简单去重。普通重复同值去重、冲突值报错。
3. 测试跨 15 分钟的 2 kW 已知段逐桶积分、60 秒上限、缺口、末样本不延伸、真实零与空值、部分覆盖和完整覆盖的守恒；合成样例明确标 synthetic。
4. 真实 PVGIS 样例 48.21 W 按 1→500 kWp 输出 24.105 kW，只缩放一次；检查四段继承完整小时可见时刻，`as_of` 在该时刻前全部不可读，之后可读。对 measured、derived、configured 分别覆盖正向和拒绝提前可见；25→1→25 和总分表一致性回归。
5. 逐条验证 B01 SourceBundle/SourceRecord、单位、性质、空值、完整来源哈希与行范围、多输入溯源及可重现性；验证源装机缺失/零/负、factor 无效、负延迟等拒绝路径。检查无大文件进 Git。
6. 实施文件：上述六个 adapter 模块、`tests/adapters/` 对应测试、需要补充的最小 fixtures 和本说明；共享配置变更如有需要先协调。无预报、优化、领域包或前端代码。真实一周验收、容量敏感性和下游交接留到第四轮，不能把本轮设计当作已通过。

### 候选容量量级核验

以下仅用于候选量级检查，不构成设备选型、同时观测、可行性或经济性结论：

- 仓库历史 D1 探针报告 HIPE 总表 15 分钟均值约 16.03 kW、峰值约 42.33 kW；按已确定的 25 倍算术放大分别约 400.75 kW、1058.25 kW。探针数据和实现的已知边界见上文，其值是既有报告而非本轮完整重算。
- 本轮实取 PVGIS 响应在中心小时假设下选择南京当地 2017-07-01 00:00 至 2017-07-08 00:00 的 168 小时，按源 1 kWp 到候选 500 kWp 缩放，逐小时值之和对应约 9212.31 kWh，均值约 54.835 kW，最大小时值约 276.78 kW，零输出 70 小时。这是模型输出在明确假设下的统计，不是南京厂区实测。
- 候选储能 1000 kWh ÷ 250 kW = 4 小时，仅为名义能量与功率之比；未考虑 SOC 范围、效率、退化、功率约束和调度。

上述 HIPE 周为 2017 年 10 月、PVGIS 周为 2017 年 7 月，分别映射到演示时段；不能把它们当成同一地点、同一周的同步观测，也不能据两组峰值简单认定供需匹配。第三、四轮还需按已确认的转换与覆盖规则重算曲线，并由业务方确认最终装机和储能容量。

## 使用与运行

安装仓库 `pyproject.toml` 指定依赖和 dev 依赖。Windows 的 `zoneinfo.ZoneInfo` 使用 IANA 时区数据库，项目运行依赖声明 `tzdata==2025.3`，无需依赖宿主时区数据库；不要替换为固定偏移。官方说明：https://docs.python.org/3.11/library/zoneinfo.html 。原始数据保留在仓库外；清单中的 `external_path` 是本机记录，换机器后应自行指定对应路径，不能依赖该路径存在。HIPE 使用 `zip绝对路径!CSV成员名` 或解压后单个 CSV 路径；PVGIS 使用原始 seriescalc JSON 路径。各自显式提供 source_start、plant_start 和天数。以下 Python 示例为单次本地调用：

```python
from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.adapters.baseload import selected_device_baseload

hipe = HipeAdapter().normalize(
    r"C:\data\hipe_week.zip!MainTerminal_PhaseCount_3_geq_2017-10-23_lt_2017-10-30.csv",
    {"source_start": "2017-10-23", "plant_start": "2026-07-01", "days": 7,
     "load_scale": {"factor": 25}}, {"delay_seconds": 0})
pv = PvgisAdapter().normalize(
    r"C:\data\pvgis_2017.json",
    {"source_start": "2017-07-01", "plant_start": "2026-07-01", "days": 7,
     "pvgis_version": "5.3", "source_peak_kwp": 1, "target_peak_kwp": 500},
    {"delay_seconds": 0})
# 对每个设备分别 normalize；仅对明确选择的设备调用 selected_device_baseload(hipe, [device])。
```

HIPE 返回德国 UTC 时间轴的 measured 原始量级记录和南京映射的 configured 记录。二者可共享 source_id 和 quantity；下游应先按 nature、电表、目标时间窗及版本筛选，不能把两个视图相加。默认厂区业务使用 configured，原始 measured 用于追溯和核验；指定 factor=1 时目标视图仍是 configured，因为时区与日期已映射。PVGIS 返回已缩放的目标 kW，`nature=derived`；有值记录因中心小时假设标 `estimated`，缺测记录标 `missing`，负功率会报错。后续 B10 禁止再按装机缩放。`selected_device_baseload` 仅扣一次明确选择的配置分表，保留各输入溯源及最晚可见时间，缺测传递，绝对余量不超过 B01 `LEDGER_TOLERANCE["power_zero_kw"]` 时归零，低于负容差时报错；它提供候选基线，不创建工序或调度。对于各槽的 `known_energy_kwh`，不完整覆盖的 `value` 为 null，不可将部分能量按整个槽当作完整负荷。

本地验收命令（替换绝对路径）：

```powershell
python -m pytest tests/adapters tests/contracts -q
python -m pytest tests/skeleton -q
python -m tests.adapters.real_week C:\data\hipe_week.zip C:\data\pvgis_2017.json fixtures/timeseries/real_week_report.json
```

验收脚本只读原始文件并生成小型统计报告；不会把 ZIP 或整年 JSON 写入 Git。`fixtures/timeseries/source_manifest.json` 中保存原始来源、哈希与切片定位，`real_week_report.json` 记录此次真实数据统计。严格可重复运行时应先验证原文件 SHA256 与清单一致。

消费示例（每表、每个完整南京日应有 96 个配置槽）：

```python
from datetime import datetime, timedelta, timezone
start = datetime(2026, 6, 30, 16, tzinfo=timezone.utc)  # 南京 2026-07-01 00:00
configured = [r for r in hipe.records if r.nature == "configured"
              and r.locator.meter == "MainTerminal"
              and start <= r.event_time < start + timedelta(days=1)]
assert len(configured) == 96  # 缺失槽仍存在且 value 为 None
```

默认 bundle_id 由有效参数和源指纹确定；显式提供 bundle_id 时，调用方负责确保唯一性。真实 excerpt 在 `tests/adapters/test_repair.py` 中还原为临时 CSV / 最小 PVGIS 输入并调用 normalize。临时文件的哈希和行号/数组索引是实际适配器输入，原始大文件的散列及原行号/数组索引保留在原切片中，不能混用。脱网命令：`python -m pytest tests/adapters/test_repair.py -q`。

## 已知范围和下游交接

- `PvgisAdapter` 仅接受明确配置的 PVGIS 5.3、响应元数据 ERA5/小时平均/W 和 HH:30；它不能凭响应自身独立验证服务端版本。PVGIS P 的中心小时假设仍待更权威的字段级依据，不能将该假设写成已实证的真实积分窗口。
- HIPE 当前接受有序单表 CSV 或 ZIP 成员，不读取已规范化记录，不联网；缺字段、不同设备混入、时区不符、乱序或冲突重复会报错。最后一条观测无后继则不向后延伸。目标区间有空洞时严格标 missing；不自动修补。
- 每个有效 HIPE 槽记录源成员、文件 SHA256、覆盖、源时间范围、首末关联原始时间文字和一基行号范围；DST 另记丢弃段数、丢弃区间的源 UTC 起止与一基行号范围、丢弃电量。聚合不是逐行嵌入整个原文件。可通过该范围与源文件重现。PVGIS 保留源 JSON 数组下标、源小时 UTC 范围和比例。
- B07 负责唯一回放时钟和按 `available_at <= as_of` 过滤；B10 消费 B05 已按目标装机缩放的 kW；B13/B14 需确认 Q25 工序和预测口径，B08 负责替换及账本口径。VacuumPump1 阈值结果只是可追溯候选，设备额定功率、工单和可平移窗口未确认。
- 500 kWp、1000 kWh／250 kW 仍为候选；不同来源不是同地点同一时间实测。`内部设计文档` 原 B10 缩放说明仍待维护者同步；本分支没有更改共享配置或冻结契约。


## 本次真实一周验收结果

HIPE：2017-10-23 至 2017-10-30 德国当地时间；PVGIS：南京当地 2017-07-01 至 2017-07-08；均映射至南京 2026-07-01 至 2026-07-08。HIPE 每表德国原始 676 槽、南京目标 672 槽，重复小时保留第一次。下表能量是有覆盖的已知能量，不把缺失填零；目标已按 25 倍缩放。丢弃数量一列按采样段数计，不是小时数；同一信息另有两个口径：`dropped_repeat_intervals` 给合并后的丢弃区间（源 UTC 起止 + 一基行号范围），`dropped_energy_kwh` 给这些段按源功率 × 有效秒数算出的电量（1 倍源口径，乘 `factor` 才是配置视图电量）。重复数是相同原始时刻的重复行数，冲突遇到即停止。

| 电表 | 原始缺测槽 | 目标缺测槽 | 丢弃的重复小时采样段数 | 重复行 | 原始已知 kWh | 保留部分 1 倍 kWh | 目标已知 kWh | 能量及覆盖守恒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ChipPress | 3 | 3 | 656 | 0 | 0.000 | 0.000 | 0.000 | passed |
| ChipSaw | 3 | 3 | 656 | 0 | 7.168 | 7.168 | 179.206 | passed |
| HighTemperatureOven | 3 | 3 | 656 | 0 | 12.395 | 12.395 | 309.887 | passed |
| MainTerminal | 3 | 3 | 656 | 0 | 2713.269 | 2705.302 | 67632.562 | passed |
| PickAndPlaceUnit | 3 | 3 | 729 | 0 | 5.193 | 5.203 | 130.079 | passed |
| ScreenPrinter | 3 | 3 | 729 | 0 | 4.251 | 4.251 | 106.264 | passed |
| SolderingOven | 3 | 3 | 656 | 0 | 32.435 | 32.435 | 810.867 | passed |
| VacuumOven | 3 | 3 | 656 | 0 | 0.182 | 0.182 | 4.554 | passed |
| VacuumPump1 | 3 | 3 | 656 | 0 | 29.096 | 29.096 | 727.402 | passed |
| VacuumPump2 | 3 | 3 | 656 | 0 | 11.455 | 11.455 | 286.386 | passed |
| WashingMachine | 3 | 3 | 656 | 0 | 0.103 | 0.103 | 2.563 | passed |

原始总表完整槽均值 16.059 kW、峰值 42.439 kW；目标 1 倍完整槽均值 16.107 kW、峰值 42.439 kW；目标 25 倍完整槽均值 402.674 kW、峰值 1060.979 kW。完整总表按源数据重新计算 25→1→25，一致；各表所有有覆盖目标能量除以 25 与源中保留部分能量、覆盖秒数分别一致。全部文件中未发现冲突行；各表逐槽对原始和目标完整记录进行了倍率比较。

全部设备与总表共同完整的 669 槽中，总表减全部设备的最小值 127.621 kW，负槽 0；此诊断不是选择工序的业务扣除规则。只扣除 VacuumPump1 的候选基线共 672 槽，其中 3 槽缺失，记录了总表与被选分表两条来源。阈值 12.5 kW 得到最长连续候选 2026-07-02T23:30:00+00:00 至 2026-07-03T09:45:00+00:00，共 41 槽，均值 21.547 kW；该阈值段不是已确认工单。

PVGIS 在明确的中心小时假设下输出 672 槽，缺失 0 槽，均值 54.835 kW、峰值 276.780 kW，总电量 9212.310 kWh；全部小时的四个子槽继承同一完整小时可见时间。以上容量均为候选，最终容量、工序业务口径和 PVGIS P 严格物理区间尚未确认。

首次实现时的历史验证：`python -m pytest -q` 通过 382 项；`python -m tests.adapters.real_week ...` 成功生成机器可读结果。实际运行环境的骨架 TestClient 在沙箱中卡住，完整回归在沙箱外执行通过。真实源文件及计算报告的位置见来源清单。

### 获取原始文件示例

在仓库外选择有足够空间的目录；例如将下面的 `$DataDir` 改为本机数据目录：

```powershell
$DataDir = 'C:\data\plantkg'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
curl.exe -L --fail -o "$DataDir\hipe_week.zip" 'https://zenodo.org/records/14054902/files/hipe_cleaned_v1.0.1_geq_2017-10-23_lt_2017-10-30.zip?download=1'
curl.exe -L --fail -o "$DataDir\pvgis_2017.json" 'https://re.jrc.ec.europa.eu/api/v5_3/seriescalc?lat=32.06&lon=118.80&startyear=2017&endyear=2017&raddatabase=PVGIS-ERA5&pvcalculation=1&peakpower=1&loss=14&angle=30&aspect=0&outputformat=json'
Get-FileHash -Algorithm SHA256 "$DataDir\hipe_week.zip", "$DataDir\pvgis_2017.json"
```

PVGIS 后续重新请求可能随服务数据或版本变化而产生不同内容；有差异时保存新文件与新元数据、重新计算哈希并重做验收，不能沿用旧文件指纹。HIPE 官方 Zenodo 页面公布的是 MD5；本次 SHA256 是本地计算并与历史探针比较的值。

## PR 5 修复后复验 2026年10月30日

在新的 Windows Python 3.11.5 虚拟环境中，仅以 `python -m pip install -e '.[dev]'` 安装项目和开发依赖；安装得到 `tzdata==2025.3`，`pip check` 通过。独立子进程清空系统 TZPATH 后成功加载 Europe/Berlin、Asia/Shanghai，验证柏林 2017-10-29 的两次 02:30 分别为 +02 和 +01。无 `tzdata` 的隔离解释器按相同方式加载柏林时复现 `ZoneInfoNotFoundError`。

修复后在最新 main 上执行：`pytest tests/adapters -q` 33 项通过，`pytest tests/contracts tests/skeleton -q` 375 项通过，`pytest -q` 461 项通过；完整回归在沙箱外运行，以避免本机沙箱中 TestClient 挂起。契约生成物一致性与模块边界分别由 `tests/contracts/test_boundaries.py` 和 `tests/skeleton/test_layout.py` 覆盖。无跳过或失败项。

修复后重新核对两个仓库外原始文件 SHA256，并在同一新环境执行 `python -m tests.adapters.real_week <HIPE ZIP> <PVGIS JSON> <输出路径>`。11 个 HIPE 电表、PVGIS、基线与全部统计检查通过；与修复前历史报告相比，HIPE 数值、PVGIS 数值和检查结果均未变化，仅报告状态表明本次复验。现 `fixtures/timeseries/real_week_report.json` 和 `source_manifest.json` 均记录修复后验收状态及新的报告散列。

复审修正：R1 加入运行依赖与无系统时区库子进程测试；R2 使用冻结账本功率容差并记录归零前值；R3 将两个真实切片恢复为临时输入并测试适配器，同时区分原文件与临时文件定位；R4 缺必填键及 PVGIS 结构返回带路径的 ValueError；R5 默认 bundle ID 对源内容、有效参数和选择的分表集合做确定性区分；R6 支持包含感叹号的普通路径及 ZIP 路径；R7 更新当前模块、采样段单位和清单状态。S1 为有效 PVGIS 输出标 estimated、拒绝负值；S2 明确按性质过滤视图。

PVGIS P 中心小时处理仍是记载的假设；候选装机容量和 Q25 工序口径仍待确认。旧规划中的 B10 缩放描述待同步，B05 输出已经完成一次装机缩放。

样例和报告 JSON 使用 `.gitattributes` 固定 LF 换行，避免 Windows 和其他系统的检出换行差异造成文件 SHA256 不一致。本次重新计算了切片及报告的字节指纹；原始 HIPE ZIP 和 PVGIS JSON 的指纹未变。

本次复验依据 DRAG0NM 对 PR 5 提交 `33ae2aa` 的[审查意见](https://github.com/DRAG0NM/SiteMind/pull/5#pullrequestreview-5713435743)，重点核对 Windows 时区依赖、基础负荷浮点容差及真实切片的脱网复现。对应修复位于提交 `8a20db6`；合并前仍需按仓库流程完成复审。
