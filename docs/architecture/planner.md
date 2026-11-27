# 共享规划器（B09）

`plantkg/decision/planner/` 实现唯一的共享 MILP（ADR-0001、ADR-0008）。四个领域包（B10～B13）只通过 `SharedModelBuilder` 往同一个 Pyomo 模型里贡献变量、约束、平衡项和成本项；时间集合、购电/外送变量、公共功率平衡和总目标只归 B09 所有。

## 模型

```text
时间集合      T = horizon ÷ step（日前 96 × 15 min）
公共变量      grid_import[t] ≥ 0，0 ≤ grid_export[t] ≤ 外送上限（配置，默认 0，07 Q16）
功率平衡      Σ supply[t] + grid_import[t] = total_load[t] + grid_export[t]，对每个 t
总负荷        total_load[t] = 不可平移负荷预测 base_load[t] + Σ 各包 demand[t]
目标          第一阶段 min Σ 各包成本项（唯一目标汇总器，结果按 {pack}.{label} 分项返回）
              第二阶段 整数变量固定为第一阶段取值，原费用 ≤ 第一阶段费用 + max(1e-6, 1e-7×|费用|) 下 min Σ 购电（词典序，LP）
```

- 第二阶段的作用：电价为 0 的时段里，“用光伏”和“弃光再购电”的费用相同，第一阶段可能选后者，B08 会判“弃光同时购电”不平。第二阶段把原费用限制在第一阶段的值以内（只放宽求解器容差量级），再尽量少购电，因此原费用不会被换成更贵的方案，第一阶段的下界仍是原费用的有效下界。报出的目标值是最终解的原费用，下界和 gap 都来自第一阶段。第二阶段把整数变量固定为第一阶段的取值，变成 LP 求解：第一阶段的解一定可行，本机演示日约 0.06～0.08 s（主要是 Pyomo 重建模型），预算取 max(剩余预算, 0.5 s)，所以总耗时最多可能超出 `time_limit_s` 约 0.5 s。最终费用最多比第一阶段高 max(1e-6, 1e-7×|费用|)，远小于账本费用容差。原费用为常数（没有费用项）时，不加费用上限。第二阶段构建或求解失败时，保留第一阶段的解。
- **账本口径核对**：返回计划前，从 Plan 中 `quantity="pv_curtail"` 的动作读出弃光量（与 B08/B17 只凭 Plan 复算的口径一致，包不用另外登记），按 B08 口径核对：同一时段弃光和购电不能同时超过 1e-3 kWh，不满足就返回 error、不带计划，无论解来自第二阶段还是回退到第一阶段。规划器只核对这一条；初始 SOC 越界这类由输入状态决定的问题，由 B08 账本自己判定。光伏包（B10）的弃光动作必须用 `pv_curtail` 这个动作量。
- 第二阶段的结果记在 `SolverInfo.params["stage2"]`（`ok` / `no_solution` / `failed: …`）；耗时只算实际求解，不含排队。

- `base_load` 和 `pv_available` 两类预测由规划器读取，只用 `request.forecast_ids` 列出的那几条，同一个量出现两条就拒绝。`base_load` 必须有。预测单位必须是 kW、步长一致、时段网格和规划区间对齐、完整覆盖规划区间、无缺测（缺测直接拒绝，不当 0 规划），并且发布时刻对 `as_of` 可见。`base_load` 由规划器自己登记为负荷；`pv_available` 通过 `builder.pv_available` 只读提供给光伏包。
- 工序负荷只由 production 包通过 `x[s]` 贡献（07 Q25）。

## 包怎么贡献

```python
def contribute(self, mb, state, rules, forecasts):
    pc = mb.add_var("charge", "continuous", (0, 250), Unit.kW, action=(BAT, "charge_power"))
    u = mb.add_var("is_charging", "binary", (0, 1), Unit.fraction)
    mb.add_constraint("charge_excl", lambda t: pc[t] <= 250 * u[t], [Ref(kind="config", id="config://…")])
    mb.register_balance("demand", pc, "charge")
    mb.register_cost(lambda t: price[t] * mb.grid_import[t] * h, "energy_cost")   # 电价包
    return ContributionReceipt(pack=self.name, ...)
```

| 方法 | 约定 |
| --- | --- |
| `add_var(name, domain, bounds, unit, *, index=None, scalar=False, action=None)` | 默认按时间集合建索引；`scalar=True` 建标量；`index=[s…]` 只为指定时段下标建（工序只建能完整放进窗口的开始时段）。`index` 为空集合表示没有任何候选（如工序没有可行开始时段），直接拒绝，不会退化成标量。名称自动加前缀 `{pack}__{name}`，重名拒绝。给了 `action=(实体, 动作量)` 时，按非零取值生成 `Action` |
| `add_constraint(name, expr, refs)` | `expr` 可以是单个关系式、`t → 关系式` 的函数（可返回 `Constraint.Skip`）或关系式列表；`refs` 必填 |
| `register_balance(side, expr, label)` | 按时段给出：索引变量、列表或 `t → 表达式` 的函数 |
| `register_cost(expr, label)` | 标量表达式，或按时段给出（求和） |
| `time_set`、`grid_import`、`grid_export`、`pv_available` | 只读；`total_load` 在所有包贡献完之后才能访问 |

登记时，或包贡献完后由规划器核对（`audit`），直接拒绝（`PlantkgError DOMAIN_INVALID`）的情况：
- 约束、平衡项或成本项引用了其他包的变量（只允许本包变量和公共购/售电）；
- 平衡项里出现不是 kW 的连续变量（二进制或整数选择变量可以作为系数出现）；
- 约束没有 `Ref`；单位不是契约单位；动作对象不是 `entity://` 标识；
- 绕过 builder 直接往 `model` 上加组件，或改动公共购/售电变量的上下界、固定状态（包能拿到 Pyomo 对象，所以在贡献后比对）；
- 包内抛出的其他异常，转成 `DOMAIN_INVALID` 并带上包名。

包不能自建模型、设置目标或求解参数；`finalize` 之后不能再贡献。

## 规则

请求里的 `rule_ids` 必须全部取回：漏取的报 `NOT_FOUND`，多出或重复的报 `DOMAIN_INVALID`。缺规则时不出计划。

`RuleCompilerRegistry` 按 `Rule.kind` 找到注册它的包：
1. 先筛选：对 `as_of` 不可见的记为 `not_visible`，未审核的记为 `not_reviewed`，没有包注册或所属包未启用的记为 `unsupported_kind`，这几类都作为 `PendingIssue` 返回，不静默忽略；不在规划区间内生效的直接跳过，不算问题。
2. 再编译：`RuleKind.compile` 声明这条规则会生成哪些约束，规则随后交给所属包的 `contribute` 去建。
3. 最后核对（`check_built`）：声明的约束必须由同一个包建出、带上该规则的 `Ref(kind="rule")`，并且至少有一条实际约束（全部 `Skip` 等于没生效），否则返回 `error`。

包拿到的是整条规则。规则可能在规划区间中途才开始或结束生效，包必须对每个时段用 `rule.applies_to(slot_start)` 判断是否适用。

## 求解与状态

用 HiGHS（Pyomo `pyomo.contrib.solver` 接口）求解，参数 `time_limit_s / mip_rel_gap / threads / random_seed` 都取自 `PlanningRequest.solver_budget`。

Pyomo 求解时会接管 `sys.stdout/stderr`，同一进程里并发求解会冲突甚至挂起，所以版本探测、求解和结果加载都在进程级锁内串行执行。`time_limit_s` 只约束拿到锁之后的求解时间，排队时间另记（`SolveOutcome.queued_s`），不计入 `elapsed_s`。

HiGHS 在一个进程内只按首次求解的线程数初始化调度器，之后换线程数会返回 `unknown`（本机实测）。因此进程内锁定首次的线程数，后续请求一律沿用，实际使用的值记在 `SolverInfo.params["threads_effective"]`。

| 求解器结束情况 | `solver_status` | 计划 |
| --- | --- | --- |
| 达到配置的 gap | optimal | 有 |
| 触及时间或迭代预算，且有可行解 | feasible（附实际 gap，另附目标值和下界） | 有 |
| 有可行解但求解器没给出有效下界（纯 LP 最优除外） | error，不用目标值冒充下界 | 无 |
| 触及预算，没有可行解 | timeout | 无 |
| 证明不可行 | infeasible，附冲突线索 | 无 |
| 无界、不可行或无界未区分、求解器异常、规则约束对不上、独立校验未通过 | error，附 `validation_errors` | 无 |

- **独立校验（`validate_solution`）**：先把整数变量取整、把绝对值 ≤ 1e-6 的连续变量置 0，再逐个检查有限性、上下界、整数性（容差 1e-6）和全部约束（含公共功率平衡，相对容差 1e-5）。不采信求解器自报的状态。
- **冲突线索**：逐个停用一个包的全部约束后重新求解，停用后变为可行的包列为冲突的参与者，同时附上相关约束名和依据。这只是线索，不是 IIS，也不会用放宽后的解生成计划。诊断的总预算是原预算的一半，用完就停止，并在线索里列出未检查的包；`elapsed_s` 包含诊断耗时。
- **外送动作**：外送非零的时段生成 `grid_export` 动作（对象 `entity://{厂区}/grid/main`，依据为外送上限配置项），B08 计划账本只凭 Plan 就能取到外送量。购电不生成动作，由账本按差额推导。
- **plan_id**：对请求内容、实际编译的规则全文（含版本）、外送上限和求解器版本取哈希，不含 trace、agent 运行号、幂等键和操作人。同一请求复算得到同一个 ID；规则内容或配置变了，ID 也会变。
- **目标值**：按取整、置零之后的解重新计算，和 B08 账本费用口径一致。

## 使用

```python
from plantkg.decision.planner import SharedPlanner

planner = SharedPlanner(packs, snapshot_of, forecasts_of, rules_of, export_limit_kw=0.0)
result = planner.solve(request)          # PlanResult；只在 JobRunner 后台任务里调用（ADR-0006）
```

`snapshot_of / forecasts_of / rules_of` 是按 ID 取记录的函数，正式实现分别归 B07、B14、B24；在那之前由测试替身提供。

## 验证（2026-10-30，本机 HiGHS 1.15.1）

使用 `fixtures/planner/day_96.yaml` 中的构造日（非实测，电价是演示配置），Fake 光伏、储能和电价包：

| 场景 | 状态 | 日电费 | 求解耗时 | 说明 |
| --- | --- | --- | --- | --- |
| 光伏 + 电价（无储能） | optimal | 2289.28 元 | 0.05 s | 弃光 1026.3 kWh，外送为 0 |
| 光伏 + 储能 + 电价 | optimal | 1590.42 元 | 0.15 s（含第二阶段） | 充 1263.2 kWh、放 1140.0 kWh，充放互斥，期末电量回到初始值 |
| 多维背包，预算 0.5 s | feasible | — | 0.56 s | gap 0.0022 |
| 多维背包，预算 0.001 s | timeout | — | — | 无计划 |
| 期末电量要求 5000 kWh（超过容量） | infeasible | — | — | 线索指向 storage 包 |

计划经 `compute_ledger` 用计划账本口径复算后判为平衡，费用与目标值的差在 `COST_TOLERANCE` 以内（`tests/planner/test_planner.py::test_plan_passes_b08_ledger_with_objective`）。

```powershell
& "$env:USERPROFILE\venvs\plantkg\Scripts\python.exe" -m pytest -q tests/planner
```

## 交接与待定

- `policy`（B0/B1/B2）只原样记在计划里。三种策略具体对应启用哪些包、用什么初始状态，由 B17 的基线装配决定；规划器不按策略名做特殊分支。
- 外送上限默认 0（07 Q16，待讨论），通过构造参数 `export_limit_kw` 设置。
- `ContributionReceipt` 由各包自己填写，规划器只核对 `pack` 字段，并用回执里的 `refs` 兜底动作依据。
- `add_var` 的 `index`、`action` 和 `pv_available`、`total_load` 是 `SharedModelBuilder` 在冻结端口 `ports.ModelBuilder` 之上的扩展（关键字参数可选，与原签名兼容）。是否写回 `ports.py`，由契约维护人决定。
- `SharedPlanner.last_builder` 只供测试和离线复核使用；并发求解时会互相覆盖。
