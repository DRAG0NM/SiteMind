# B18 工厂场景装配：实现说明

## 结构

| 文件 | 内容 |
| --- | --- |
| `configs/sites/factory/site.yaml` | 区域、楼层、配电拓扑、54 台设备（行政楼每层空调箱与照明自动展开）、生产区底座参数、时间映射、作息与人流参数 |
| `configs/sites/factory/pv.yaml`、`storage.yaml` | 1 MWp 光伏、2 MWh / 500 kW 储能，B10/B11 加载格式 |
| `model.py` | `SiteModel.load(site_yaml)`：展开设备、校验拓扑（断开、成环、接错节点、变压器缺容量都拒绝）、配电路径、B01 `Entity` |
| `sources/d05.py` | D05 读取、表计取用、底座分解、时间映射、`measured_bundle`（原始）与 `base_bundle`（厂区底座） |
| `loads.py` | `synthesize(site, d05_data, days)`：设备级负荷、人流、物料任务、分区/变压器/全厂汇总，`to_bundle()` 输出 B01 记录 |
| `__main__.py` | `python -m plantkg.world.site` 打印设备表、首日逐小时人流与负荷、变压器峰值 |
| `scripts/data/fetch_d05.py` | 下载原包到仓库外、校验 MD5、记 SHA-256、转全年窄表、切一周小样本 |

## 取舍

1. **功率口径用电能 ×4。** D05 的 `avg_power_kw_15m` ALL 相是三相平均（mi_a 约 78 kW），`energy_kwh_15m` 与 `demand_kw` 的 ALL 相是三相合计（约 232 kW，与原包年用电 1,228,639 kWh 吻合）。任务卡里的“均值约 78 kW、峰值约 197 kW”来自前者，偏小约 3 倍；倍数按 232 kW 口径取 2。
2. **底座总量 = max(mi_a, 叶子合计)。** 原包 `EdgeList.csv` 与光伏校验说明该厂有几百 kW 自有光伏，mi_a 是净购电；Gold 层光伏表 `mi_b` 读数接近 0，无法直接加回。样本周爱尔兰当地 10～17 点 mi_a 约 130～190 kW，叶子合计稳定在约 250 kW。任务卡原规则（叶子超出时按比例缩到 mi_a）会把对方光伏的午间凹陷带进本厂负荷，与本厂 1 MWp 光伏重复扣减。2026-11-04 经发起人确认改为取较大值：叶子未超时与原规则一致，超出时总量取叶子合计、余项为 0；`d05_gross_base` 步骤记净购电、叶子合计和取用依据。样本周 672 个时段中 249 个取叶子合计。
3. **pr_f 不计入叶子。** `EdgeList` 声明 `p_f → pr_f`（压机单元含机器人），两块都算会重复；`exclude_meters` 加 `pr_f`。ahu_a、ahu_b 按任务卡不用；ex_b、ahu_c 只用故障修复之后。
4. **分项归类按资产类型。** 压缩空气、暖通、工艺（压机、混料、机器人、搬运）、公用工程四类，配电柜、热泵、光伏表、总进线不是叶子。各类 × 倍数后按同类设备额定功率占比分到各台；未计量余项挂在 P-PROD，不分给设备；某叶子表计在该时段被剔除（故障、不可靠、缺读数）时，它的用电仍在总进线里，归入余项而不是丢掉，`d05_select_meters.excluded` 记录被剔除的表计和原因。设备超过额定功率时记录标 `suspect`（当前配置没有超出）。
5. **时间映射反向求源时段。** 对厂区每个 15 分钟，按 Asia/Shanghai 钟面减去日期偏移，在 Europe/Dublin 找同一钟面：春季跳过的一小时找不到源，厂区该时段 `missing`；秋季重复的一小时取第一次（`keep_first`）。与 B05 规则一致，但不依赖 `plantkg.adapters`（world 平面不能跨平面导入）。源与厂区起始日都是周三，工作日对齐。
6. **D05 没有的分项按常理编写。** 行政楼人流（早高峰 8:00～8:45、午间、晚高峰 17:30～18:30、夜间值班、周末 8%）、车间三班交接、照明、办公暖通、洁净车间、危化品仓库排风、数据机房标 `synthetic`；人梯负荷由人流推出运行次数、货梯负荷由工艺负荷率推出物料任务，标 `simulated`。物料任务按累计余数确定性生成，不用随机数。
7. **数据性质。** 原始 D05 记录 `measured`，`note` 写“爱尔兰工厂 D05，非本厂”；倍数写在 `scale_by_declared_multiplier` 步骤，`multiplier_nature=configured`；厂区底座、D05 来源设备与汇总标 `derived`。场景输出不含 `measured`。
8. **可见时间。** 所有记录 `available_at = 时段结束 + delay_seconds`（默认 0），与 B05 一致。厂区映射后的记录按厂区时段结束计。
9. **光伏/储能新规模单独成文件。** B10/B11 的 `fixtures/packs/*` 有测试绑定 500 kWp、1000 kWh；本分支不改它们，另在 `configs/sites/factory/` 给出同格式的新规模，测试验证能被 B10/B11 加载。`configs/plant.yaml` 是共享文件，也未改。
10. **场景包来源身份统一。** `SiteLoads.to_bundle()` 的包和全部记录都用 `b18-factory-site / b18-site-1`，满足 B03 `write_partition` 的“记录身份 = 包身份”约束；D05（或 HIPE）的来源 ID、版本、文件散列、表计和转换参数写在 `d05_select_meters` / `hipe_fallback_select` 步骤里，派生记录的 `content_hash` 仍指向原始数据文件。
11. **bundle_id 用有效参数指纹。** `d05.fingerprint` 对影响输出的全部参数（数据散列、表计、窗口、倍数、time_map、天数、延迟、版本）做稳定散列：同参重试同 ID，参数不同 ID 不同，B03 不会报 `IDEMPOTENCY_MISMATCH`。
12. **采集延迟统一。** `to_bundle()` 默认继承 `production_base.delay_seconds`，与 `base_bundle()` 一致；显式传参只用于覆盖。HIPE 回退时派生记录的可见时间还不早于所依赖 HIPE 记录的 `available_at`。
13. **HIPE 回退底座。** `synthesize(site, hipe_bundle, days)` 传入 B05 `HipeAdapter` 输出时，取 `MainTerminal` 的 configured 记录（B05 已映射厂区时间并 × 25）作生产区总量，× `hipe_fallback.multiplier`（1.25）；HIPE 没有分项表计，分项按 `hipe_fallback.category_shares`（configured，取 D05 2025-05-23 起全年按本规则的平均占比）拆，余项为剩下的约 27.7%。世界平面只读 SourceBundle 契约，不导入 `plantkg.adapters`。
