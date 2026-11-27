# storage 包样例（B11）

| 文件 | 内容 |
| --- | --- |
| `storage.yaml` | 储能 BAT1：1000 kWh、充放各 250 kW、效率 0.95/0.95、SOC 0.1～0.9（候选值，configured，未批准选型） |
| `parameters.yaml` | 按版本清单给的储能参数：基准 `vm-0003` 备用比例 0，候选示例 `vm-0004` 备用比例 0.3 |

## 用法

```python
from plantkg.packs.storage import StoragePack, load_storage_config
cfg = load_storage_config(Path("fixtures/packs/storage/storage.yaml"), "fixtures/packs/storage/storage.yaml")
params = yaml.safe_load(Path("fixtures/packs/storage/parameters.yaml").read_text(encoding="utf-8"))
storage = StoragePack(cfg, params, "fixtures/packs/storage/parameters.yaml")
planner = SharedPlanner({"storage": storage, ...}, ...)
```

- 配置校验：容量、充放功率为正有限数，效率在 (0, 1]，`0 ≤ soc_min < soc_max ≤ 1`，期末 SOC 在范围内，衰减单价非负，来源必须是
  `config://`，未知键报错。`validate_storage_config` 逐项返回 `config_invalid`，`StorageConfig` 构造时有任何一项不合格就拒绝，
  不会进到求解。
- 模型（与 B08 账本同一口径）：充放为非负 kW，`E[t] = E[t−1] + η_c·P_c·Δt − P_d·Δt/η_d`，E 是时段末电量，
  `SOC 下限 × 容量 ≤ E ≤ SOC 上限 × 容量`；充放互斥用二进制 `u[t]`，负电价时也成立；期末电量不低于初始电量，或不低于配置的
  `end_soc × 容量`；`degradation_cny_per_kwh` 给出时才按充放电量计衰减成本（`storage.degradation`）。
- 初始电量只读快照 `pack_states["storage"]["energy_kwh"]`（由执行器 B16 写入），缺失、非有限、超出 `[0, 容量]` 都拒绝。
- 备用比例 `reserve_ratio`：参数规格 `RESERVE_SPEC`（0～0.5），备用线 `E ≥ 容量 × (soc_min + r × (soc_max − soc_min))`。
  取值只看快照的 `version_manifest_id`，找不到对应参数就拒绝，不用默认值代替；包上的参数是只读的。候选由 B18 生成、B04 激活，
  本包不发布候选。`reserve_ratio_range(配置, 初始电量, 时段小时)` 给出候选可行域上界：首个时段满功率充电后能到的电量。
- 规则：`storage_soc_window`（`quantity=soc`、单位 `1`，bounds 在 0～1）与 `storage_power_limit`（`quantity=power`、单位 kW、
  只给 `bounds.upper`），按 `applies_to` 逐时段生效；对象不是本储能、单位不对或范围不合格的作为待处理问题返回。
- 计划动作：`charge_power`、`discharge_power`（kW）。账本用 `config.ledger_params()` 取 `StorageParams`。

## 样例结果（2026-10-31 本机，HiGHS）

B09 构造日（Fake 光伏、base_load、演示电价），初始电量 500 kWh：

| 场景 | 状态 | 日电费（元） | 充 kWh | 放 kWh | 最低电量 kWh | 期末电量 kWh |
| --- | --- | --- | --- | --- | --- | --- |
| 基准 `vm-0003`，备用 0 | optimal | 1590.42 | 1263.2 | 1140.0 | 100.0 | 500.0 |
| 候选 `vm-0004`，备用 0.3 | optimal | 1759.71 | 757.9 | 684.0 | 340.0 | 500.0 |
| 基准 + 衰减 0.05 元/kWh | optimal | 1710.58 | 1263.2 | 1140.0 | 100.0 | 500.0 |
| 无光伏，午间 6 小时 −0.5 元/kWh | optimal | 521.47 | 1741.5 | 1571.7 | 100.0 | 500.0 |

负电价场景里没有一个时段同时充放。初始电量 150 kWh 时备用比例可行域为 [0, 0.1367]；取上界 optimal，上界 + 0.01 判 infeasible，
冲突线索指向 storage 包。计划经 B08 `compute_ledger` 复算判平，逐时段 SOC 与模型电量轨迹的差在 `soc_abs` 以内。

```powershell
& "$env:USERPROFILE\venvs\plantkg\Scripts\python.exe" -m pytest -q tests/packs/storage
```

## 待交接

- `pack.yaml` 已声明 provides，但保持 `status: placeholder`：注册表目前没有登记 `DomainPack` 实例的入口，`StoragePack` 由装配方构造后
  交给 `SharedPlanner`。入口确定后再改为 active。
- 参数按 `version_manifest_id` 取值是本包对“参数随版本清单走”的落地方式；正式参数存储与 `parameters` 组件的内容格式由 B04/B18 定，
  接上后把 `parameters` 换成从版本清单读出的映射即可。
- `reserve_ratio` 范围 0～0.5 是本包的保守设定，待业务确认。
