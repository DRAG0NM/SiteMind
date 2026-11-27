# Nexent v2.8.2 平台能力记录

本文记录 Nexent v2.8.2（本机 Docker 部署，对话模型 `deepseek-v4.1-flash-1017`）的实测能力，供 B19 网关与 B29 Skill 开发查阅。证据全部来自 `nexent_assets/probes/`（三轮探针，2026-10-29 实测），下表证据路径均相对该目录；逐项过程见 `REPORT.md`、`p_round2/NOTES.md`、`p_round3/NOTES.md`、`p_round3_timing/NOTES.md`。

结论含义：通过 = 按预期可用；降级 = 可用但有限制，需在 plantkg 侧补偿；失败 = 平台做不到，需换做法。

## 能力总表

| 能力项 | 结论 | 关键事实 | 对 plantkg 的设计约束 | 证据文件 |
| --- | --- | --- | --- | --- |
| MCP 传输与工具发现 | 通过 | SSE 与 streamable HTTP 都能接入，URL 以 `/sse` 结尾走 SSE，其余走 streamable HTTP；协商协议版本 `2025-11-25`，与 fastmcp 4.1.4 互通。注册后须调用一次 `GET /tool/scan_tool`，工具才进入工具表。扫描、工具测试入口、失败运行都会在 MCP 服务上各留一个会话 | plantkg 用 streamable HTTP，URL 以 `/mcp` 结尾；注册或更新工具后触发扫描 | `a_mcp/logs/requests.jsonl`、`a_mcp/nexent_samples/tools_probe_*.json` |
| 参数传递 | 通过 | 嵌套对象、枚举、可选参数原样传到服务端（`opt:3` 仍是 `int`，不传为 `None`）。Nexent 落库时只存 `properties`，丢掉 `required`，并给可选整数多加了 `"type":"string"` | 可选参数用带默认值的明确类型，服务端做宽松类型转换；不要靠 `required` 约束模型 | `a_nb/samples/run_echo.sse.txt`、`run_echo_noopt.sse.txt`、`p_round3/bind_v3required.json` |
| 错误返回 | 降级 | 工具抛 `ToolError` 后，Agent 拿到的只是字符串（`execution_logs` 里），无错误标记，流里无 `error` 事件；工具测试入口返回 HTTP 500 加错误文本。第三轮 `PLANTKG_E_…` / `PLANTKG_NOTICE_…` 前缀在 `execution_logs` 中可见，模型会带进回答 | 业务错误码固定放在返回文本开头，网关与工作台按前缀识别 | `a_nb/samples/run_fail.sse.txt`、`a_mcp/nexent_samples/validate_fail.json`、`p_round3/samples/p15_nb_write.sse.txt` |
| 北向 SSE 事件与四种异常终态 | 降级 | 工具调用有 `tool` 事件（带 `tool_name`、`tool_arguments`、`step_id`），结果在同 `step_id` 的 `execution_logs`，无成败区分。适配器须处理：① 超时只有一条“MCP 工具调用超时（60 秒）”的 `final_answer`，无 `execution_logs`、无 `error`；② 中止后连接直接关闭，无结束事件；③ `parallel_executor` 打包事件；④ 正常结束的 `answer_phase:"complete"` 的 `content` 为空，正文在前面的 `delta`。另有：运行失败 HTTP 仍 200，错误在 `final_answer` 的 `Run Agent Error: …` 里；准备阶段失败才有 `error` 事件；客户端断流后服务端继续跑完，流不能续接 | 以 `complete` 为结束信号并拼接 `delta`；“关闭但没收到 complete”按中止处理；同时识别 `error` 事件与 `Run Agent Error:` 前缀；断流后用 `GET /nb/v1/conversations/{id}` 补结果 | `a_nb/samples/run_hello.sse.txt`、`run_nomodel.sse.txt`、`run_disconnect.sse.txt`、`p_round2/samples/p4_slow_*.sse.txt`、`p10_*.sse.txt` |
| 可信用户身份 | 失败 | Nexent 调 MCP 只带注册时填的静态 `Authorization` 和自定义头（`custom_headers` 原样转发），不带用户、租户、会话信息；`mcp-session-id` 不能当业务身份 | 写操作不信任从 Nexent 进来的身份；身份由网关签发的 ctx 或令牌经 `tool_params` 注入 | `a_mcp/logs/requests.jsonl`、`p_round3/logs/requests.jsonl` |
| tool_params 注入与覆盖 | 通过 | `tool_params.agents.<agent>.tools.<tool>.<参数>` 覆盖 MCP 工具调用参数：模型传 `alice`，服务端收到 `fixed-by-config`。421 字符令牌逐字节到达并通过 HMAC 校验；不注入时模型自编的值被拒；一个隐藏参数 `ctx` 注入 5 个工具全部生效；模型伪造的 ctx 被查表拒绝。SSE 的 `tool_arguments` 仍是模型给的值 | 采用“网关签发 ctx + tool_params 注入”；SSE 里的参数值不当真实参数展示；ctx 一次一用、短时效；工具返回值不得回显注入参数，只回 sha256 前 12 位 | `a_nb/samples/run_tool_params_fix.sse.txt`、`p_round2/samples/p2_*.sse.txt`、`p_round3/samples/p14_nofill.sse.txt`、`p14_fill.sse.txt`、`p15_admin_forge.sse.txt` |
| 注入白名单与盲写风险 | 失败（盲写） | 工具名写错、未绑定、`*` 通配、空 `tools` 都静默不注入；给没有该参数的工具注入（`knowledge_base_search` 加 `ctx`）使整次运行在创建 Agent 阶段失败，HTTP 200，错误只在 `final_answer` 文本里。`required` 被丢弃，必填结构下模型先被 MCP 校验拒绝再补空串，一次 29 秒、6 次重试，无安全收益 | 网关运行前按 Agent 实际绑定的工具与每个工具的 schema 生成注入表并校验，对不上就让运行失败，不静默；ctx 用“可选 + 空默认 + 服务端拒绝”，不用必填 | `p_round3/samples/p14_b_*.sse.txt`、`p14_req_*.sse.txt`、`p15req_admin_write.sse.txt` |
| 注入泄漏范围 | 降级 | 平台不把注入值写进 SSE、会话历史、容器日志、Nexent/Supabase 数据库、Redis、ES（令牌复扫 0 命中）；但工具回显入参时，ctx 经 `execution_logs`、SSE、模型上下文到 runtime 日志一路扩散 | 工具实现规范：不回显注入参数；返回值统一过滤敏感字段 | `p_round2/p2_scan.json`、`p_round3/p14_leak_scan.json` |
| 续聊不保留注入 | 通过 | 续聊是新运行、新 MCP 会话（conversation 53 的两轮是两个 session）。不重新注入时 `tool_params` 不保留：hello 收到模型填的值，知识库检索不执行；重新注入后恢复。漏注入时写工具拒绝（`PLANTKG_E_NO_CTX`），只读降级提示（`PLANTKG_NOTICE_NO_CTX:`） | 网关每轮完整注入；漏注入是失败关闭，不会退化成读全量资料或以他人身份写 | `p_round2/samples/p3_*.sse.txt`、`p_round3/p13_sessions.json` |
| mcp-session-id 与运行归属 | 失败 | SSE 里 0 处出现 session id；一次运行按 MCP 服务建会话；会话里无租户、用户、Agent、conversation；10 次运行各一个会话，并发 3 个互不共用 | 运行标识由网关自己签发（`run_id` 绑定 ctx），session id 只能当同服务内的调用分组 | `p_round3/p13_sessions.json`、`p_round3/logs/requests.jsonl` |
| 并发 | 通过 | 并发 2 个和 5 个，会话 ID 各不相同，`tool_params` 与 `document_paths` 不串；并发 5 个时单个耗时 12～29 秒（单独约 12 秒）；3 个并发运行 0.4 秒内各建会话，调用不串 | B32 并发 ≤ 5，每个样本用新会话；服务端别按会话累加状态 | `p_round2/p6_check.txt`、`p_round2/samples/p6_c*.sse.txt` |
| 知识库 document_paths 过滤 | 降级 | 内置检索工具不支持标签；北向 `tool_params` 传内部参数 `document_paths`（模型看不到、改不了）后，三种检索词都只检到 2026 版；不传则两份都返回。管理端 `tag_predicates` 可按枚举值精确匹配标签。`basic` 分块把 “2026-07-01” 切成三段；与 MCP 参数覆盖可在同一次运行并存 | 由 plantkg 算出 as_of 下的可见文档清单，每轮注入 `document_paths`；入库时打标签并保存路径与生效日映射；范围条件按不支持处理 | `a_nb/samples/run_kb_docpath_2026.sse.txt`、`run_kb_nofilter.sse.txt`、`a4_kb/samples/search_*.json` |
| Agent 记忆 | 降级 | 默认开启，每次运行先调 `search_memory`，会把其他会话的事实带进来。只能按用户关（`MEMORY_SWITCH=N`）；按 Agent 禁用（`/memory/config/disable_agent`）不影响检索；北向请求无开关。关闭后同会话多轮仍靠会话历史正常 | 回放与评测使用专用 Nexent 用户（记忆关）及其北向 Key | `p_round2/p1_samples/`、`p_round2/samples/p1_*.sse.txt` |
| Skill 导入 / 导出 | 降级 | 上传、挂载、执行（模型调 `read_skill_md`、`read_skill_config`）、随 Agent 导出 ZIP 再导入均可行。`allowed-tools` 写成字符串导致 500；按名字在整个租户匹配工具；`version` 被丢弃；导入冲突只能改名“<名> 副本”；流程顺序不保证，模型会并行；`/mcp/disable` 不阻止调用。Agent 导出含 `mcp_info`（服务名与 URL），不含静态 token 与自定义头；导入按工具名重绑，同名工具会绑到别的服务，导入 Agent 与原 Agent 同名 | 工具名在租户内全局唯一；版本自管（名字或 `VERSION` 文件）；参数取值放 `config/config.yaml`，强制参数走 `tool_params`；导入后核对绑定并改名 | `p_round2/p7_skill/`、`p_round2/samples/p7_flow3.sse.txt`、`a5_export/plantkg_probe_agent.export.json`、`a_nb/samples/run_imported_hello.sse.txt` |
| 工具结构更新与删除 | 降级 | 加参数、改参数名：扫描后原地更新，tool_id 不变，已绑定的 Agent 自动跟上。删除工具：旧记录残留且仍显示可用，Agent 绑定还在，所有运行在创建 Agent 阶段失败，须解绑并重新发布。扫描会把未启用 MCP 服务的工具标为不可用；删除 MCP 服务不删工具记录 | B19 同步脚本做“扫描 → 对比 → 解绑已删除工具 → 重新发布”；开发期先弃用后删除 | `p_round2/p5_samples/`、`p_round2/samples/p5_*.sse.txt`、`p_round3/restore_diff.json` |
| 工具超时 60 秒与长任务 | 降级 | `RUNTIME_MCP_TOOL_TIMEOUT_SECONDS=60` 为部署级全局值，按每次 MCP 调用计，不按步或整次运行：35/45/50/55 秒单次调用正常，一步内顺序 3×25 秒（75 秒）与并行加顺序 135 秒都跑完。同步工具在 60 秒被切断后不会停：纯 CPU 线程跑满 90 秒，一周 MILP 超时后继续 85.8 秒并求到最优（145.8 秒，gap 9.5e-5），结果调用方拿不到。`t_submit` 立即返回 pending，后台线程不随 MCP 调用取消；同 `idem_key` 只产生一个 job；中途 stop 后另起运行用 `job_id` 取回。`t_get_job(wait=40)` 取 115 秒任务需 3 次、5 步，北向 SSE 静默约 40 秒。沙箱为 Docker，无本地回退，`NEXENT_SANDBOX_TIMEOUT_S=30` 不是单步上限。`parallel_executor` 的 120 秒是单任务默认超时，可被模型覆盖 | run_day 拆成“提交任务 + 查询结果”，求解在 plantkg 后台执行；工具内 40 秒截止返回 pending，不 join 求解线程；`get_job` 默认 wait=40（有 30 秒空闲代理时 20～25）；长任务 Agent `max_steps` 取 8～10（5 步在 150 秒任务上撞上限，默认 15）；工具描述写明轮询规则（少一次调用、少约 3.9k 输入 token） | `p_round2/samples/p4_slow_*.sse.txt`、`p_round3_timing/evidence_p17_p18.md`、`evidence_p19_watch.md`、`evidence_timeouts.md`、`evidence_p20_jobs.md`、`evidence_sandbox_runtime.txt` |
| 返回大小 20000 字符截断 | 降级 | 模型把工具返回值作为最后表达式输出时，超过 20000 字符会被截掉中间部分（20KB 那次丢第 11 段）；用 print 输出时 100KB 也完整，是否截断取决于模型写的代码 | 单次返回 ≤ 8KB，只返回摘要和证据 ID，长列表分页，详情走 plantkg 只读接口 | `p_round2/samples/p4_big_*.sse.txt` |
| 中止 | 通过 | `/nb/v1/chat/stop/{id}` 返回 200，正在执行的工具被取消，后续工具不再调用；流直接关闭，无结束事件；会话历史留下一条空的 assistant 消息。后台任务（job）不受影响，继续跑完 | 关闭但无 `complete` 当作中止；写工具能承受中途取消 | `p_round2/samples/p10_*.sse.txt`、`p_round3_timing/samples/p20_stop_115s.sse.txt` |
| SSE 事件时序 | 降级 | 服务端与容器同一时钟（偏差小于测量精度）。`tool` 事件晚于服务端收到调用：单工具 +3 ms，并行子工具 +115～218 ms，不是调用前钩子。并行时服务端实际开始顺序可与 SSE 子事件顺序不同（slow→echo→hello，SSE 为 slow→hello→echo）。多个结果合成一条 `execution_logs`，在最慢工具返回后才发。15 秒工具期间仅开头 0.22 秒有事件，之后 14.8 秒无心跳 | 不能靠 `tool` 事件拦截或确认，确认放在工具内两阶段（`propose_write` 立即返回，`apply_write` 另行落库）；按参数配对不按顺序；plantkg 自己生成 `call_id`；逐条进度与 keepalive 由网关自发 | `p_round3/p16_clock.json`、`p_round3/samples/p16_*.sse.txt`、`p_round3/NOTES.md` |
| logprobs | 失败（deepseek）/ 通过（qwen-plus 快照） | `deepseek-v4.1-flash-1017` 返回 400 `logprobs is not supported`，思考与否都一样。`qwen-plus-2025-12-01` 非思考返回 top-5，A/B/C 各 1 个 token；`qwen3-vl-plus` 非思考返回空列表。百炼 `decision-model-preview` 直接给选项概率：106 条 BAS 点位、60 类，去掉 Unknown 加 3 种选项顺序平均，top-1 78.3%、top-3 92.5%、ECE 0.046，单顺序仅 38%～49%；自带 Unknown 门控 27 条判成 Unknown 其中 25 条错 | 判别用 `decision-model-preview`，固定类别集、去掉 Unknown、多顺序平均、选项带定义；“其他/转人工”另做门控；preview 模型只在华北2可用，plantkg 四类规则上的质量由 B23 复核样本重测 | `b_bailian/samples/b1_logprobs_*.json`、`b_bailian/samples/p8_*.json`、`p8d_*`、`p_round2/P8_decision_NOTES.md` |
| 图像理解 | 通过 | 主模型非思考模式读真实铭牌照片 11/11、自绘折线图 4/4（`qwen3-vl-plus` 同样全对）；测试图 `image_tokens: 202`。思考模式下 50 token 全耗在推理，`content` 为空 | 清晰图可直接用主模型；调用时关思考或给足 `max_tokens` | `b_bailian/samples/p9_*.json`、`b2_image_nothinking.json` |
| 鉴权与指定 Agent | 通过 | `Authorization: Bearer <access_key>`，无 Key 或错 Key 返回 401；Key 须属于某租户用户，超管 Key 返回 401。请求体 `agent_name`，Agent 须已发布，不存在返回泛化 HTTP 500；同名 Agent 有风险 | 工作台代理持有租户用户的 Key；调用前用 `GET /nb/v1/agents` 校验 | `a_nb/samples/auth_none.sse.txt`、`auth_bad.sse.txt`、`agent_missing.sse.txt`、`a_nb/samples/auth_log_excerpt.txt` |

## 其他实测数值

| 项 | 数值 | 证据文件 |
| --- | --- | --- |
| 本机 MILP 求解规模 | 96 时段 1 道工序 0.37～0.48 秒；96 时段 5 道链式工序 0.99～1.14 秒；672 时段 5 道工序 113～117 秒（HiGHS 1.15.1 / Pyomo 6.10.1），默认线程无明显加速 | `e1_solver/e1_output.jsonl` |
| 运行时部署变量 | `RUNTIME_MCP_TOOL_TIMEOUT_SECONDS=60`、`RUNTIME_PARALLEL_EXECUTOR_TIMEOUT_SECONDS=120`、`NEXENT_SANDBOX_TIMEOUT_S=30`、`NEXENT_SANDBOX_CPU_QUOTA=1.0`、`NEXENT_SANDBOX_MEMORY_LIMIT_MB=2048`、`NEXENT_SANDBOX_NETWORK=disabled` | `p_round3_timing/NOTES.md`、`p_round3_timing/evidence_sandbox_runtime.txt` |
| 工具链版本 | Python 3.11.9；pyomo 6.10.1、highspy 1.15.1、pyoxigraph 0.5.13、duckdb 2.0.0、fastmcp 4.1.4、pyarrow 26.0.0 | `c_env/requirements-lock.txt` |

## 未测项

以下内容在资料中明确写为未测，或只读过源码，不得当作已验证的平台行为。

| 项 | 状态 | 出处 |
| --- | --- | --- |
| 知识库文档更新与删除（P11） | 未测；只读源码：同名上传自动改名 `name_1`，旧文件保留；残留分块、删除、替换都没有实测 | `REPORT.md` 第二轮 |
| 技能生成（P12） | 未测；仅看到路由 `POST /skills/nl2skill/run` 存在 | `REPORT.md` 第二轮 |
| 知识库“生效日 ≤ as_of”范围条件 | 未测，按不支持处理 | `REPORT.md` 关键问题 |
| 模糊、倾斜、反光的图像，需从坐标轴估读的图 | 未测 | `REPORT.md` 第二轮 P9 |
| deepseek 投票的判别质量 | 只在 2+2 简单题上测过 5 次，无法说明质量 | `REPORT.md` 第二轮 P8 |
| plantkg 四类规则上的判别质量 | 未测，待 B23 用复核样本 | `REPORT.md` 第二轮 P8b |
| 多 MCP 服务（一次运行 2 个以上）的会话数量；子 Agent 是否另开会话 | 只看了源码，未实测 | `p_round3/NOTES.md` 未测 |
| `parallel_executor` 同名同参数两次调用如何配对 | 只做了推理，未构造 | `p_round3/NOTES.md` 未测 |
| 必填结构下其他模型的自愈行为 | 只测了 `deepseek-v4.1-flash-1017` | `p_round3/NOTES.md` 未测 |
| `tool_params.agents.<名字>` 名字写错的行为 | 源码结论，未单独实测 | `p_round3/NOTES.md` P14 |
| 北向 SSE 经代理的空闲超时 | 只证明本机直连 40 秒静默不断，未测代理 | `p_round3_timing/NOTES.md` P20-6 |
| 并发 5 个运行的整批起止时间 | 未记录 | `REPORT.md` 第二轮 P6 |
| 超管 Key 返回 401 的 SSE 样例 | 未保存，只有后端日志摘录与源码 | `REPORT.md` A2 |
| 容器内文件的令牌复扫 | 只在测试当时扫过 | `REPORT.md` 第二轮 P2 |
| PVGIS（D2）、分时电价政策原文（D3）、plastering / BTS（D5） | 未测 | `REPORT.md` 数据核实 |
| 生产调度实例（D4） | 只完成下载和结构查看，时间单位与价格尺度未确认 | `REPORT.md` 数据核实 |
