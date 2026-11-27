# B31 evidence chain

离线示例：`python -m plantkg.knowledge.evidence demo`。比较：
`python -m plantkg.knowledge.evidence eval --out runtime/b31-eval`。
这两项均不调用模型；示例与问答语料为 synthetic。

场景钩子将 B17 的实际事件与提案、B28 裁决及 B19 分钟控制的实际
计划快照、目标、状态和控制器结果关联到 B04 `EvidenceEvent`。写入端用装配传进来的
B04 `EvidenceSink` 端口（与 API 共库）；没有端口时（事件命令行离线跑场景）退回场景库，
事件 `params.assembly` 标 `scenario_db`，有端口时标 `storage`。B19 的滚动
重解计划如实记录，不声称通过额外审批。采集不改变派发或控制行为。
图谱路径来自 B25，文档检索受 B26 专家和可见时间约束；资料版本使用
SHA-256，原文保留定位。执行前观察的来源标为 `preflight_observation`，
事后匹配标为 `posthoc_candidate`，均不冒充专家真实使用的来源。图谱快照
版本和运行清单分别记录；清单未声明图谱组件，不能声称绑定已验证。
`EvidenceService.list_action` 提供按运行、动作、trace 和时间过滤的只读分页。
已有 HTTP 路由支持 `GET /api/v1/evidence?run_id=...&action_id=...`，可选
`trace_id`、`cursor`、`page_size`；按动作查询必须指定运行。`run_id`、
`agent_run_id`、`action_id`、`trace_id` 都是 SQL 过滤参数，事件在 SQL 里统一
按 `occurred_at <= as_of` 且（`available_at` 为空或 `<= as_of`）过滤；不带
`run_id` 时用只读上下文的可见时刻，带 `run_id` 时取该运行的时钟（回放游标，
场景运行取已写入事件的最新时刻）。返回空页时仍须按 `next_cursor` 继续分页。
网关 SSE 与 REST/EvidenceService 走同一可见性过滤。

照明切换和非安全照明削峰使用本目录的 synthetic 规程；切换前以停供作为
更严格的图谱安全检查。`air_system/AIR` 到图谱 `AIR-HDR` 的映射只用于
合成工厂演示，映射本身与图谱路径分开标注。证据钩子在启用该领域包时生效。提案和计划是实际决策输入的结构化
记录，不代表每个动作均有完整规程证据。

评测中的每题查询直接上游，并用运行记录找到规程，从另一份规程读出处置；
两组共享语料、时点、两次检索预算及确定性回答器。23 题上游只在图谱中，
一题图谱缺路径且资料包含上游，用于验证回退。这是刻意设计的合成数据集，
不能据此推断一般 RAG 或真实模型的表现。`results.json` 记录每题答案、出处和
评分，`results.csv` 及 `results.md` 供对照查看。

图谱和资料间的冲突比较支持同一动作的自动执行许可：图谱安全规则要求工单、
资料明确写允许自动执行时标记冲突。无法比较的情况保持未知。解释数字来自输入
结构化记录或场景账本，原文中出现的数字作为引用而不作为计算事实。资料版本与
`required_versions` 按 UTF-8 文本、LF 归一化后取 SHA-256，Windows 与 Linux
工作副本得到同一值；临时语料写入时固定 `newline="\n"`。
