# ADR-0002 网关会话、ctx 注入与失败关闭

- 状态：已决（2026-10-29，07 Q03～Q05，09 D18/D19）；B01 冻结
- 影响分支：B02、B04、B15、B18、B19、B24、B29、B35

## 背景（实测）

Nexent 调 MCP 时只带注册时填的静态请求头，没有用户、租户或会话身份；北向请求的 `tool_params` 能覆盖工具参数，模型看不到也改不了；注入是盲写——给不接受该参数的工具注入会让整次运行失败，写错工具名会被静默忽略；续聊不保留上一轮注入。

## 决定

1. **RunSession**：工作台的每次 Agent 调用都经网关。网关认证用户，在服务端登记 `RunSession`（actor/roles、plant_id、industry_pack、as_of、可见文档、agent_run_id、trace_id、过期时间），生成 128 位随机 `ctx`（32 位十六进制），经 `tool_params` 注入每个白名单工具；知识库检索另注入 `document_paths`。每次运行完整注入。
2. **Context 由服务端得到**：MCP 工具凭 `ctx` 查 RunSession 得到 `Context`，不从工具参数逐项读取 plant_id、actor 等。`Context.actor` 不接受模型自报。
3. **ctx 可选、默认空**：`ToolSpec.accepts_ctx` 声明注入白名单；输入 schema 中 `ctx` 为可选字符串、默认 `""`，普通参数不得叫 `ctx`/`document_paths`。
4. **失败关闭**（B02 `security.py` 实现）：

   | 情况 | 写入/提议/后台任务工具 | 只读工具 |
   | --- | --- | --- |
   | 无 ctx（Nexent 界面直接发起） | `PLANTKG_E_NO_CTX` | 降级结果，文本以 `PLANTKG_NOTICE_NO_CTX:` 开头 |
   | ctx 查不到 | `PLANTKG_E_UNKNOWN_CTX` | `PLANTKG_E_UNKNOWN_CTX` |
   | ctx 过期 | `PLANTKG_E_CTX_EXPIRED` | `PLANTKG_E_CTX_EXPIRED` |
   | 伪造 ctx | 同“查不到” | 同“查不到” |

   查不到与伪造对只读工具也报错而非降级：错误的 ctx 说明注入链路出了问题，不能静默退化。
5. **特权写入“Agent 提议、工作台确认”**：`propose_review_decision`、`propose_plan_confirmation`、`propose_publish`、`propose_rollback` 只生成 `PendingAction`；复核员在工作台经统一确认端点 `POST /api/v1/actions/{id}/confirm|reject` 确认后，B04 调用由 B15/B18/B24 注册的 `ActionHandler` 执行，确认人、时间与原因写入审计。模型无法凭自己的判断发布或回滚，资料中的提示词注入也不能触发写入。
6. **不回显**：两层防线。契约层递归检查 `ToolResult.data`、`EvidenceEvent.params/model_params`、`UIEvent.payload`、`PendingAction.payload/edits`、错误详情，任何层级出现注入参数名的键都拒绝；契约不知道 ctx 的值，所以自由文本字段（`notice`、`error.message`、`summary` 等）里的值由出站层（工具注册层与网关）用 `contains_secret` 扫描后才发出。日志只记请求/版本 ID；`NexentRunPayload` 是唯一携带注入值的契约。

   脱敏的责任划分：

   | 位置 | 契约已做（B01） | 后续负责 |
   | --- | --- | --- |
   | 对象字符串 | `NexentRunPayload` 的 `str`/`repr`/格式化输出、`RunSession` 的 ctx 都不显示原值 | — |
   | 校验报错 | `RunSession`、`NexentRunPayload` 设 `hide_input_in_errors`，只隐藏 `str(exc)`；`exc.errors()`、`exc.json()` 仍带原始输入。契约提供 `safe_error_details(exc)`，只留位置、类型、说明 | B02 入口层与日志：ValidationError 一律经 `safe_error_details` 转换后才能返回或记录，禁止直接记录异常对象、`errors()` 或 `json()` |
   | 序列化 | `model_dump` 保留原值（发给 Nexent 需要） | B19：只在出站 HTTP 请求处调用，不写日志、不进证据 |
   | 网关出站与 SSE 合流 | — | B19：发往前端的 UIEvent 与回答文本出站前用 `contains_secret` 扫描 |
   | 工具出站文本 | `ToolResult` 递归拒绝注入键名 | B02 注册层：`to_agent_text()` 结果出站前用 `contains_secret` 扫描 |
   | 日志与异常 | — | B02：日志格式化器对 ctx 正则脱敏；异常处理不记录请求体与工具参数原文 |
7. **mock/dev 默认 ctx**：`ContextChannel.mock_default` 只用于本机在 Nexent 调试页测试写工具，正式运行关闭（B02 配置）。

## 后果

- 在 Nexent 界面直接发起的运行只能查看、调试。
- 网关启动时按 Agent 实际绑定与 `mcp/tools.json` 核对白名单，对不上即拒绝启动运行（B19）。
