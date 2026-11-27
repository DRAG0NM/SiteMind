# pv 包样例（B10）

| 文件 | 内容 |
| --- | --- |
| `pv.yaml` | 光伏阵列 PV1，装机 500 kWp（候选值，configured，未批准选型） |

测试用的光伏数据是仓库里的真实 PVGIS 摘录（`fixtures/timeseries/pvgis_2017_excerpt.json`），经 B05 `PvgisAdapter` 从 1 kWp 缩放到 500 kWp，
`nature=derived`。源 2017-07-01、07-02 映射到厂区 2026-06-29、06-30；规划日 07-01 的 `pv_available` 预测由测试替身按持久性方法得到
（取 24 h 前，as_of 时还不可见就取 48 h 前，只读已可见记录）。正式预测归 B14，本包不做预测。

## 用法

```python
from plantkg.packs.pv import PvPack, load_pv_config
pv = PvPack(load_pv_config(Path("fixtures/packs/pv/pv.yaml"), "fixtures/packs/pv/pv.yaml"))
planner = SharedPlanner({"pv": pv, ...}, ...)
```

- 模型：`使用 + 弃光 (+ 限发) = 可发`，非负；可发只取 B09 读好的 `pv_available` 预测，缺这条预测直接拒绝，不当 0。
- 装机边界：可发超过装机 kWp 即拒绝（PVGIS 被重复缩放或 W 当 kW）。B05 已做唯一一次装机缩放，本包不再缩放。
- 计划动作：每个非零时段的 `pv_use`、`pv_curtail`（kW，对象 `entity://…/pv/PV1`），依据为装机配置和相关规则。
- 限发规则 `pv_output_cap`：`quantity=pv_output`、`unit=kW`、只给 `bounds.upper`，按 `applies_to` 逐时段生效。超出上限的功率记为
  `pv_limit` 动作，**不算弃光**；B08/B17 组账本时光伏可发取 `预测 − pv_limit`，“弃光同时购电”的核对因此只针对经济性弃光。
- 执行账本取实际光伏：`pv_actual_kw(B07 取回的记录, 时段, as_of, 装机)`，不另做时间过滤，只拒绝 as_of 时还不可见、不是 derived kW、
  或超过装机的记录；缺测返回 `None`。
- 账本拆分：`pv_breakdown(ledger)` 把可发拆成直接利用、充电、外送、未归属、弃光。光伏先供本时段负荷；剩余部分只在本时段没有购电、
  没有放电时才算进充电和外送，否则记为未归属，不把储能电量说成全是光伏。

## 样例结果（2026-10-31 本机，HiGHS）

规划日 07-01（当地），B09 构造日的 base_load 与演示电价，光伏为上面的持久性预测（日可发 792.7 kWh，峰值 156.8 kW）：

| 场景 | 状态 | 日电费（元） | 弃光 kWh | 限发 kWh |
| --- | --- | --- | --- | --- |
| 光伏 + 电价 | optimal | 4483.79 | 0 | 0 |
| 光伏 + 储能 + 电价 | optimal | 3776.35 | 0 | 0 |
| 光伏 + 电价，限发 0 kW | optimal | 5109.82 | 0 | 792.7 |
| 光伏 ×3 + 电价 | optimal | 3271.29 | 39.6 | 0 |
| 光伏 ×3 + 储能 + 电价 | optimal | 2725.70 | 0 | 0 |

光伏 ×3（峰值 470 kW，仍在装机内）只用来制造午间富余；加入储能后富余全部充电，弃光为 0。计划经 B08 `compute_ledger` 复算判平，
费用与目标值的差在 `COST_TOLERANCE` 内。

```powershell
& "$env:USERPROFILE\venvs\plantkg\Scripts\python.exe" -m pytest -q tests/packs/pv
```

## 待交接

- `pv_limit` 是本包新增的动作量：B17/B16 组账本时要从光伏可发里扣掉它；M05 弃光量指标是否计入限发，由 B17 定。
- `pack.yaml` 已声明 provides，但保持 `status: placeholder`：注册表目前只登记清单，还没有登记 `DomainPack` 实例的入口，
  `PvPack` 由装配方构造后交给 `SharedPlanner`。入口确定后再改为 active 并填 entrypoint。
- 负电价：经济上“弃光再多购电”更省钱，但 B08/B09 把弃光同时购电判为不平，计划会被判 error。电价包有 `negative_price`
  待处理原因，负电价进规划前需要 B08/B12 先定口径；本包不加互斥二进制。
- 规则生效区间只覆盖两个时段起点之间（不含任何时段起点）时，所有时段都跳过，B09 `check_built` 会判 error。这属于 B09 规则筛选口径，
  本包不单独处理。
- `内部设计文档` 中“B10 负责装机缩放”的旧描述与 B05 口径不一致，按 `docs/data/timeseries.md` 以 B05 为准。
