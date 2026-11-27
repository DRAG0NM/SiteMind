# B33 场景对照

### Issue #244 云端实验预检（2026-11-16）

本轮未执行 Nexent 首采或 D54 真实对照：百炼账户的可靠 5 元账单硬上限尚无法核验。没有调用指定云端模型，不能把既有离线、缓存或历史共享用户探针作为本轮真实结果。首采、重放、来源、覆盖缺口、token、延迟及费用均待真实实验取得；D54 的独立用户和关闭记忆状态也未验证。

百炼[预算管理说明](https://help.aliyun.com/zh/model-studio/budget-management)指出达预算停机存在延迟，延迟期间仍计费，因而该开关本身不证明本轮 5 元硬上限。免费额度用完即停的适用模型、额度与开关状态须在账户中核验，不能假设适用。Docker 曾因 Secrets Engine 无法重命名 `engine.sock` 启动失败，之后用户恢复了 Docker：Engine 29.9.2、Nexent v2.8.2 的 13 个容器运行，Web HTTP 200，现有配置只读预检通过。未重置或清理共享数据。Nexent 只读模型列表显示对话模型 ID 6 指向百炼兼容端点，ID 2 指向本机 Ollama；向量模型 ID 7 指向百炼。百炼模型列表包含指定的两个名称，但不等于已验证推理权限或真实调用。仓库 `nexent_assets/probes/p_b27_slow/suite.py` 现支持指定独立 Agent、随机请求 ID 和隔离输出目录；运行前仍须独立核验用户、模型绑定、记忆与费用保护。

2026-11-17 续查：Docker 和 13 个现有 Nexent 容器已恢复；Web、认证、Northbound 健康接口与 MCP 管理接口可访问。模型保护和独立关闭记忆用户仍未能核验，本轮没有真实推理、首采或 D54 结果；详见 [Issue #244 指南](../issue-guides/ISSUE-244-NEXENT-LIVE-VALIDATION.md)。

当前入口 `python scripts/evaluate_scenarios.py configs/scenarios/factory_demo_day.yaml` 离线运行两个自然日的 B17 场景，使用独立数据库。B0 是无事件动作基线：不派专家、不自动响应事件，仍接受剧本中的人工临时指令，并与其他组使用相同末端充电规则；它不代表固定时段调度策略。B1 使用直派器，B2 使用值守层。电费为整个剧本窗口，来自分钟账本，不是元/日；结果是 simulated，不代表 Nexent 实测。

默认目标是共同初始电量，若剧本指定目标则采用指定值；结果中明确列出三组起止电量与不可比原因。B17 根据实际输入记录 data_sources 并在分叉后核对（见 #83）；缺失、HIPE 回退或来源不一致时不计算组间差值。代码不从输入文件名称推断来源。

## 方法与限制

离线命令：`python scripts/evaluate_scenarios.py configs/scenarios/factory_demo_day.yaml --out runtime/b33-results --cache-version v1 --orchestration`。结果为两天合计，`metrics.json` 的 `metrics` 保持 DayMetric 字段；附加元数据和知识结果分别保存在独立字段，`comparison.json` 不对不可比组计算差值，`cost.svg` 展示窗口费用。B32 可通过 `--b32-results` 读取外部 JSON；缺省标记 pending_dependency。

B0 无事件动作，不调用专家；B1 是 B17 独立专家直派；B2 是 evaluation 模式的 B27/B29/B28/B47 装配。三组隔离数据库、种子与输入；最后两小时以统一的末端恢复规则，通过正常仿真动作按储能功率与效率充电，账本记录购电，目标未满足仍标不可比。该规则是评测装配的实施选择，并非 B19 已有的日内优化策略。B17 的 RunInfo.data_sources 按 D52④ 记底座（`d05`/`hipe` 小写来源名）、装配（`b17-inputs`）、光伏与电价四项；评测缓存键另用 `data_source_refs` 纳入装配包版本与输入文件散列；其来源名与 `data_sources` 一样统一小写（`d05`，此前误写为 `D05`），这改变了缓存键：旧缓存全部失效，升级后需用 `--collect-offline` 离线重新首采一次。分叉继承并核对，见 Issue #83。

缓存的 evaluation/trial 登记与 live 隔离；缺失调用对应 deterministic 结果并统计覆盖不足。首次采集、重放命中和缺失分别计数；采集后本次使用不算重放命中。采集成功的那一次返回按 `nexent` 记入跟踪记录并带真实墙钟耗时，重放命中记为 `cache`、耗时 0 ms，因此 `nexent_latency_p50/p95` 只统计采集调用自身的墙钟耗时，重放不计入；专家、triage、investigate 三类采集都计入，`n` 为该组实际采集次数。未采集的组保持 null。实际执行的是确定性实现（缓存缺失回退，或离线首采的确定性结果）时，主 Agent 记录与 live 回退同一口径：`run_mode=deterministic`、`source=deterministic`、`coverage_gap=true`；专家记录同口径：B33 协同组与 B36 的专家走 `consilium`，trial/evaluation 下来源声明为 deterministic（缓存缺失回退或离线首采的确定性结果）或专家来源未登记而取到确定性实现时，记 `run_mode=deterministic`、`source=deterministic`、`coverage_gap=true`；缓存声明为真实采集的重放仍记 `evaluation` / `cache`。直派器（B1 独立组）走 `TimeCoordinator`，口径相同。缓存只重放可安全重新绑定的结果，提示词和输入来源的散列进入版本键。默认不调用 Nexent，不将确定性结果写为 Nexent 实测。

**缓存状态与覆盖不足。** 每组 `groups[*].cache` 给出 `hits`、`misses`、`collected`、`hit_rate`（命中 / (命中 + 缺失)，无调用为 null）和 `mode`：`cold` 没有命中（本次是采集或全部缺失），`replay` 全部命中，`mixed` 命中与采集或缺失并存，`none` 该组没有缓存调用（B0 基线不派专家）。`groups[*].coverage_gap` 给出 `deterministic_fallbacks`（该组 `coverage_gap=true` 的主 Agent 与专家运行数）和 `cache_misses`（只统计没采集到、实际走了确定性回退的次数。注意区别：组顶层的 `cache_misses` 与 `cache.misses` 是全部未命中，含“未命中但随后采集成功”的，所以冷跑时它等于采集次数；`coverage_gap.cache_misses` 才是实际回退次数；“未命中但随后采集成功”计入 `cache.misses` 与 `cache.collected`，不计入这里）。`comparison.json` 汇总三组的 `cache`、`coverage_gap` 与一句 `coverage_note`；覆盖不足只提示，不改可比性判定，也没有新增阈值。离线首采的确定性结果本身就是覆盖不足，所以默认离线命令的 `deterministic_fallbacks` 不为 0。

**冷热两跑。** 电费、能量类指标在冷跑（首采）与热跑（重放）下一致；动作数过去会不同（曾见 13 对 11），根因是同一分钟的待汇报事项顺序随运行期随机编号而变，合批后 triage 键不同，走安全回退。#262 把待汇报编号改为由内容派生，并按（安全类优先、开单时刻、事件单号、种类）排序，修复后默认命令冷热两跑动作数一致（13 对 13），重放缺失为 0；此前 coordinated 重放 15/16 的那条安全缺失同一根因，已随之消失，不再有“状态确实不同”的残留。重放命中率与缺失数由 `tests/evaluation/test_scenario_evaluate.py` 断言（命中率 ≥ 0.9 且缺失 ≤ 2）。

同一运行内，首次采集后再次请求同一个键仍是实际重放命中；并发请求先后观察到缺失时可以多次记缺失，但单键只执行一次采集。缓存键除世界状态、储能、知识版本、提示词与数据版本外，还包含 `expert`（专家名）和 `query`（follow_up、investigate 的查询文本，#228 起）；triage 合批按（分钟、安全类优先、开单时刻、事件单号、种类）排序，提案按内容而非运行期 ID 指纹化。异步专家使实际合批内容不同时，重复运行仍可能出现不同次数的安全未命中；`cache_miss_details` 和 `cache_miss_differences` 标明原因及变化字段，不把它当作已命中或移除状态约束。

D54 离线探针使用十个已知类型的新事件实例，涉及不同现有实体与症状；A 为程序直派器，B 为值守双通道，两个路径共享输入。样本分别配置预期领域和站点拓扑中的上游配电柜与变压器；报告领域派发正确率、所需引用是否齐全、墙钟处理耗时和 fast/slow/urgent 通道占比。这不是诊断处置正确率的实测，完整真实 Nexent 双路线实验仍未完成。B17 的队列仿真没有逐人等待记录，平均及最长候梯标为 unavailable；B47 工单完成时间从其独立记录读取，未完成工单不纳入分母。B31 和 B48 的仓库产物带版本、样本及 synthetic 范围，不冒充 B30 的真实实验。

### 本机 Nexent 实测记录（2026-11-09）

经用户授权，预算上限 20 元人民币。本次实际使用现有 Nexent Docker、northbound 及本机 Ollama 的 qwen3.5 4B 别名，未使用云端 DeepSeek；本机推理模型费用 0 元。隔离运行 `scripts/nexent_live_scenario.py` 到演示日 08:10，仿真在首批事件处停留约五分钟后中止。数据库 `runtime/b33-live-probe-20261109.db` 和脱敏元数据 `runtime/b33-live-probe-20261109-summary.json` 留在本地：11 次 Agent 记录，6 完成、2 超时、3 失败；未记录 MCP 工具调用，部分回答将调用写成文本。仅能证明 Nexent 收到请求并产生回复或失败，不能称为演示主流程或 D54 评测通过。主 Agent 管理端显示已绑定 24 项工具；问题现象和复现见 Issue #159。缓存真实首采与 D54 Nexent 实验依旧未完成。

补充定位：同一 Ollama 模型以单一工具的原生 `/api/chat` 探针在 11.7 秒返回结构化 tool_call。此结果只说明单工具原生协议可用，不表示 Nexent 多工具会诊通过；已写入 Issue #159。

### 百炼云端受控探针（2026-11-09）

按用户授权使用 20 元人民币上限。由于原有 Nexent 模型条目指向本机 Ollama，本次新增独立 Nexent 模型 `qwen3.5-flash-2026-02-23`（百炼北京 OpenAI 兼容端点）和独立 Agent `b33_bailian_probe_20261109`，未改已有 Agent。该模型配置在 Nexent 租户中保留，使用现有百炼密钥。

首次直接 northbound 测试记录 12,915 输入及 1,247 输出 token（仅已返回的步骤用量），触发 trace_impact 事件但因输出协议及步数限制未完成；按当时官方小于 128K 阶梯原价估算约 0.0051 元，未含可能未返回用量的调用，实际账单以百炼为准。随后经 B23 网关注入会话进行第二次独立连接探针：22.17 秒，Agent 运行 finished，`trace_impact` 工具实际执行并返回业务状态 `done`，回答包含 TX1；网关记录见 `runtime/b33-bailian-gateway-20261109.json` 与同名独立 SQLite 数据库。第二次未取得可核对的 token 明细，因此不报告总费用。

测试使用的共享 Nexent 用户仍启用记忆，并出现 `search_memory`；为保护现有用户设置，未更改全局记忆开关。上述两次仅为真实百炼连接与工具链探针，不是独立 D54 评分，不代表已完成三组缓存首采或 B33 整日验收。真实首采、完整双路线和输出指标等工程缺口仍待完成。

### 用户指定模型的连接探针

在原有模型之外新建隔离的百炼 `deepseek-v4.1-flash` 连接及 `b33_deepseek_probe_20261109` Agent；Nexent 健康检查通过，北向调用返回最终回答，耗时 3.08 秒。共享用户记忆工具仍被调用，且该次流未返回可核对的 token 用量，因此这只是连接探针，不作为独立实验或费用总额。未改变原有同名本地模型或 Agent。

另新增 `b33_cloud_plantkg_main` 和四个 `b33_cloud_plantkg_expert_*` 隔离 Agent，绑定上述百炼模型、原工具及原技能；主 Agent 关联这四个隔离专家。它们尚未接入三组离线评测或完成全链路验证。

独立调用百炼 `decision-model-preview` 完成一个合成事件的三个选项顺序，均选择 equipment；该单题不代表准确率。百炼 `qwen3.7-text-embedding` 独立文本向量调用成功，输出 1024 维、22 输入 token；新增独立 Nexent 向量模型连接且健康检查通过，但尚未进行 Nexent 知识库检索验收。模型调用上限按用户授权为 20 元人民币，实际费用及未返回的用量应以百炼账单为准。

缓存增加了显式 `CachedSource.collect` 接口和首次采集时间；首采要求调用方提供 live 来源，专家提案还需提供提案查询并核对绑定，不能安全脱离原运行的结果会被拒绝。`ArbitrationCollector` 可将 B28 请求转给 B23 live 来源并限制调用次数，评测装配可显式注入；提案按内容指纹匹配后重新绑定本轮 ID。调用方必须控制费用。

### 续作：首采与隔离实验入口

离线首采与刷新分别追加 `--collect-offline`、`--collect-offline --refresh`，普通命令为离线重放。B23 真实采集使用 `--collect-nexent --model-version 模型标识 --budget-cny 总预留额 --per-call-cny 单次预留额 --max-calls 次数`，可用 `--agents-dir`、`--main-agent` 选择独立 Agent 资产目录，并从 `NEXENT_API_KEY`、`NEXENT_ADMIN_JWT` 环境变量读取凭据。采集前核对每个 Agent 的实际模型名和端点（可用 `--model-base-url` 指定），防止把本地别名误认为云模型。必须按模型实际输入输出上限、服务端最多步骤、工具、重试及价格设置保守的单次费用上界；该程序不能读取百炼账单，也不能阻止服务端违反其配置，预留额不是实际账单。B23 自动重试设置为 0，超时由 `--timeout` 限制；缓存和失败摘要保存在输出目录，调用日志写入预算账本。缓存带模型、剧本组别、事件、知识版本、提示词、数据、状态、轮次与提案身份；缺失或不能安全重绑时回退并记录覆盖不足。

调用日志写入 `--budget-ledger`，并非总在输出目录。同一费用授权下的所有调用应使用同一个账本（默认 `runtime/b33-model-budget.json`），以累计预留与调用次数；不要用新账本重置授权余额。跨输出目录复用缓存时，采集与重放须指定同一个 `--agents-dir` 和 `--model-version`。进程异常退出后若留下账本锁，应先核实没有采集进程仍在运行并核对账单，再由运维人员处理锁。

`--d54-only` 可单独运行离线双路线；与真实采集参数结合时还要求单独的 `NEXENT_EVAL_API_KEY`、该评测用户的 `NEXENT_EVAL_ADMIN_JWT` 与 `--isolated-memory-disabled`，并查询管理端确认记忆开关关闭。运行者须确保 JWT 与北向密钥属于同一个独立评测用户；共享用户的历史记忆不能用于独立实验。诊断准确率缺少独立真值时为 null，派域正确率和证据完整率按合成标注分别计算。模型调用次数用预留额约束，真实账单、未返回的 token 和成本仍为未知。

本机已在独立输出目录完成一次 DeepSeek 专家首采和随后离线 evaluation 重放，均命中同一条 Nexent 缓存。共享用户仍启用记忆，且没有已核实的独立评测用户，故该次是缓存链路验证，不是独立 D54 成绩。

### 默认离线命令结果（2026-11-14，origin/main a7439be 加 B33 收尾分支）

按新口径先 `--collect-offline` 首采一次，再以默认命令（`--cache-version v1 --orchestration`，输出到临时目录，不写仓库）重放。三组均可比，`comparison.json` 正常计算组间差值，电费与 11-12 的结果一致。

| 组 | electricity_cost (CNY) | peak_import (kW) | actions | work_orders | work_order_completion (min) | automatic_handling |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 30837.87 | 1535.2 | 1 | 0 | unavailable | unavailable |
| independent | 29915.58 | 1490.5 | 16 | 3 | unavailable（完成 0 / 3） | 0.625 |
| coordinated | 30971.44 | 1490.5 | 13 | 3 | 10.0（完成 1 / 3） | 0.5 |

重放的缓存状态：baseline `none`；independent 命中 11 / 缺失 0 / 采集 0，`replay`；coordinated 命中 21 / 缺失 0 / 采集 0，`replay`；两组 `deterministic_fallbacks` 分别为 11 和 21（均来自离线确定性结果）。冷跑（首采）的三组动作数同为 1 / 16 / 13。`nexent_latency_p50/p95` 三组均为 null（无真实采集）。D54 离线探针每组 10 个事件，派域正确率均为 1：A 程序直派全部为 programmatic；B 值守 fast 0.7、urgent 0.3，slow 与 unrouted 均为 0；两组证据完整率均为 0。以上均为 simulated 离线结果，不是 Nexent 实测。

`metrics.json` 同时带展示页读取的指标名（见 `docs/architecture/metrics.md` 的“B33 产物指标名”表）：知识侧四项与 `harness_pass_rate`、`learning_agent_share` 为 null 加 note；`doc_qa_accuracy`、`doc_qa_accuracy_rag` 与四项编排对照有值（synthetic）。批次、版本维度写在 `variant` 字段，不再拼进指标名。

### 续作：四条入口共用一本账与“请求 vs 下游调用”的记法（2026-11-17）

`plantkg/evaluation/ledger.py:CallLedger` 与 `plantkg/evaluation/collection.py:BudgetedCollector` 使用同一份 JSON 记录格式，
所以 B23（`scripts/nexent_live_scenario.py`）、weird_combo（`nexent_assets/probes/p_b27_slow/suite.py`）、
健康检查（`nexent_assets/probes/p_issue244/model_healthcheck.py`）与 B33/D54（`scripts/evaluate_scenarios.py`）
可以指向同一个 `--budget-ledger`，累计判断 `--max-calls` 与 `--budget-cny`，不会因为换入口而重置额度。

记录字段与口径：

| 字段 | 含义 | 不能拿它当什么 |
| --- | --- | --- |
| `reserved_cny` | 请求前写入的单次预留上限 | 不是账单金额，也不是服务端硬上限 |
| `actual_cny` | 恒为 `null`（客户端读不到百炼账单） | 不是“零费用” |
| `usage` | 仅当响应真的返回用量时才写；Nexent 的 SSE 与 `/nb/v1/chat/run` 都不回 token 用量 | 不能据此估算总 Token |
| `request_id` | 网关注入的 `X-Request-Id`，等于 `agent_run_id` | 不是百炼侧 request id |
| `model_calls` | 一次 Agent 请求实际触发的下游模型调用数（`retries+1`） | 不是 Token 数，也不含 Nexent 内部并行子调用 |
| `source` / `kind` / `model` | 来源入口、实验类型、模型名 | — |

B33 首采与缓存重放的记录口径不变（`cache_source` 与缓存命中/缺失计入产物），本轮只补了 `request_id` 与 `model_calls`，
并把账本读取改成容忍 UTF-8 BOM；账本损坏时现在会关锁、清锁再报错，不会留下永久 `.lock`。
`tests/evaluation/test_call_ledger.py` 覆盖“请求前已预留”“超限拒绝且不发请求”“失败记录且不重试”“B23/B33 共享累计账本”
“只包装 live 结果源”五条；`tests/evaluation` 全量 46 passed。

真实 B33 首采、weird_combo 的 10 次单题与两轮六题回归、D54 双路线仍**未执行**：本轮没有新的云推理授权。
执行顺序与命令见 `nexent_assets/probes/p_issue244/RUNBOOK.md` 的 §3 与 §4。

## 独立租户下的采集口径（2026-11-18）

实验对象已从共享租户迁到独立租户 `issue244-isolated`（模型记录 id=9，5 个 `i244_*` Agent 20–24，
记忆与做梦开关均为 `N`）。采集脚本本身不需要改口径，只是换一套环境变量：

- `NEXENT_ADMIN_JWT` = 新租户管理员 JWT（B33 用）；`NEXENT_EVAL_ADMIN_JWT` = 新租户评测用户 JWT（D54 用）；
- `NEXENT_API_KEY` = 管理员入口 Key，`NEXENT_EVAL_API_KEY` = 评测 Key，两者必须不同（D54 的既有校验）；
- 账本换独立文件 `runtime\issue244-batch-budget.tenant.json`，SQLite 与输出目录各自带 `-tenant` 后缀，
  与共享租户的历史产物隔离；四条入口先用 `--preflight-only` 回读租户/Agent/模型，真实请求 0 次。

费用边界需要写清楚，避免把预留当账单：

1. 账本只覆盖**外层 Agent 请求**（一次 `/nb/v1/chat/run` 或一次专家 consultation）。Nexent 内部一轮请求里
   是否再调模型、调几次，客户端看不到，`model_calls` 只能记 `retries+1` 这种可观测值。
2. 因此“预算 5 元 / 单次预留 0.30”只能约束**入口请求数**（16 次），不能证明 Nexent 内部总调用量；
   若要按内部调用计费，必须先有服务端用量回传，当前版本没有。
3. B23 已把 `nexent.run_retries` 固定为 `0`：一次预留对应一次 Nexent 运行。若允许内部重试，
   一次 `get()` 可能打两轮模型而账本只记一次，属于未覆盖支出。
4. 真实跑批前仍需解决“整批 ≥44 次 vs 5 元只够 16 次”的算术：按实测用量下调单次预留，或由用户提高客户端预算；
   拆账、换 Key、重试刷次数都禁止。
