# Nexent 北向 SSE → UIEvent 合流约定

依据：`probe/platform-capability` 三轮探针（`nexent_assets/probes/REPORT.md`、`p_round3/NOTES.md`），07 Q03，09 D19。实现方是 B19 网关；前端 B35 只消费 UIEvent。

## 原则

1. **工具调用以 plantkg 证据事件为准。** EvidenceEvent 由 plantkg 写入即可订阅，自带 `call_id`，实测早于 SSE、结果逐条实时。Nexent 的 `tool` 事件只作“正在调用”的提示：其中 `tool_arguments` 是模型填的值，不是实际参数；结果在 `execution_logs` 里且不分成败；并行时整步结束才发。
2. **SSE 只取回答文本与调用提示。** 终态由网关判定（见下表），不信任 HTTP 200。
3. **不回显注入参数。** `ctx`、`document_paths` 不进入 UIEvent 任何字段；界面不显示 SSE 里的参数值，改为显示“模型填值 → 系统绑定值”（来自 EvidenceEvent 的 `model_params` 与 `params`）。

## 事件映射

| Nexent 事件（v2.8.2 实测） | UIEvent | 说明 |
| --- | --- | --- |
| 北向请求已受理（网关侧） | `run_started` | payload：`agent_name`；网关此刻已登记 RunSession |
| EvidenceEvent `tool_called` | `tool_started` | 带 plantkg `call_id`；payload：`tool`、`model_params`（若已配对） |
| EvidenceEvent `tool_result` | `tool_finished` | payload：`tool`、`business_status`、`evidence_ids` |
| 其他 EvidenceEvent | `evidence_added` | payload：`evidence_id`、`event_type` |
| `tool`（`tool_name`、`tool_arguments`、`tool_call_id`、`step_id`） | 不单独发 | 按非 ctx 参数与 plantkg 调用配对，配上后把 `tool_call_id` 记入 `nexent_ref`；配不上只记日志 |
| `execution_logs` | 不单独发 | 内容是工具返回文本；成败以 plantkg 证据为准 |
| `parallel_executor` | 不单独发 | 并行打包事件；子调用以证据事件为准 |
| `search_content`（知识库检索） | `evidence_added` | 网关转为 `retrieval` 路径的证据事件（文档标题、路径） |
| JobRunner 进度 | `job_progress` | payload：`job_id`、`status`、可选 `progress` |
| PendingActionStore `propose` | `review_required` | payload：`action_id`、`kind` |
| plantkg `plan_created` | `plan_ready` | payload：`plan_id`、`revision` |
| `final_answer` + `answer_phase:"delta"` | `answer_delta` | payload：`text` |
| `final_answer` + `answer_phase:"complete"` | `run_finished` | complete 的 `content` 为空，回答以拼接的 delta 为准：`terminal_reason=completed_from_deltas`；complete 带非空内容时为 `completed` |
| 只有一条“MCP 工具调用超时”的 `final_answer` | `run_failed` | `terminal_reason=timeout`；工具已在 Nexent 侧被取消，后台 Job 不受影响 |
| `final_answer` 以 `Run Agent Error:` 开头，或 `error` 事件 | `run_failed` | `terminal_reason=error`，`message` 取原文 |
| 网关调用 stop 后连接关闭，或连接关闭且无 complete | `run_cancelled` / 见下 | 用户中止：`terminal_reason=cancelled` |
| 连接意外断开 | `run_finished` 或 `run_failed` | 查会话历史补结局：补到则 `stream_lost_recovered`，补不到则 `run_failed` + `stream_lost` |
| `step_count`、`token_count`、`model_output_thinking`、`model_output_deep_thinking`、`parse`、`model_attempt_control`、`other` | 不发 | 只记日志 |
| （无事件超过 15 秒） | `keepalive` | 网关自发；Nexent SSE 无心跳。前置代理空闲超时须大于 40 秒 |

## 顺序与去重

- 每个 `agent_run_id` 内 `seq` 单调递增，由网关分配；`event_id` 取来源证据事件或 Nexent 事件，重复的按 `event_id`/`seq` 丢弃。
- 终态事件（`run_finished`/`run_failed`/`run_cancelled`）每次运行恰好一条，之后不再发事件。
- 续聊是一次新的运行：每轮都重新登记会话并完整注入（实测续聊不保留注入）。

## 注入白名单

只给“已绑定到该 Agent 且 schema 声明了 `ctx`”的工具注入；给不接受该参数的工具注入会让整次运行失败，写错工具名会被静默忽略（07 Q05、P14）。白名单来源是注册表里该工具的 `ToolSpec.accepts_ctx`（契约 `TOOL_SPECS`）；`mcp/tools.json` 只是它的生成物。网关启动时与 Agent 实际绑定核对，对不上即拒绝启动运行。
