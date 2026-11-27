# ADR-0006 REST/MCP 协议：错误码、分页、幂等、长任务与 HTTP 框架

- 状态：已决；B01 冻结
- 影响分支：B02、B04、B17、B18、B19、B35

## 错误

- 错误码统一 `PLANTKG_E_<NAME>`（`ErrorCode`），HTTP 映射见 `enums.json` 的 `error_http_status`：400 请求不合法、401 无/未知/过期 ctx、403 无权或跨厂区、404 不存在或对当前 as_of 不可见、409 修订/状态/幂等冲突、422 领域输入无效或有待处理问题、503 依赖不可用或求解超时、500 内部错误。
- REST 错误体为 `ErrorResponse{error: ToolError, trace_id}`。
- MCP 侧异常到 Agent 只是一段文本（实测），所以 `ToolResult.to_agent_text()` 把错误码固定写在第一行开头：`PLANTKG_E_XXX: 说明`，第二行起是 JSON；无 ctx 的只读降级以 `PLANTKG_NOTICE_NO_CTX:` 开头。不靠 HTTP 200 判业务成功，看 `ok` 与 `business_status`。
- `retryable` 只对 `DEPENDENCY_UNAVAILABLE`、`SOLVER_TIMEOUT` 为真。

## 大小与分页

- 单次工具文本 UTF-8 ≤ 8 KB（平台 20000 字符会截掉中间部分，实测）；超出时 `to_agent_text()` 抛 `RESULT_TOO_LARGE`，工具层必须分页或给摘要 + 证据 ID。
- 列表统一 cursor 分页：REST `Page{items, next_cursor, as_of, version_manifest_id}`，MCP `ToolResult.page`；`page_size` 1～200（MCP 建议 ≤ 50）。

## 幂等与修订

所有写入（确认端点、run_day、执行、提议）带 `idempotency_key`；同键同内容返回原结果，同键不同内容 409。修改已有对象带 `expected_revision`。

## 长任务（09 D21）

- MCP 单次调用 60 秒硬上限、超时取消，同步求解会空跑（实测）。工具内部 40 秒截止：完成直接返回；否则返回 `business_status=pending` 与 `job_id`，求解在 JobRunner 后台继续。
- `get_job(job_id, wait)` 与 `GET /jobs/{id}?wait=` 在服务端最多等 40 秒。工具描述写明“返回 pending 时用同一 job_id 再调用”。导出的 Agent 设 `max_steps=12`。

## REST 边界

直接 REST 以只读为主；写入限于统一确认端点（`/actions/{id}/confirm|reject`）、网关（`/agent/run`、`/agent/stop/{agent_run_id}`）和受运维身份认证的资料入库（`/assets:ingest`）。资料入库只保存原文与待复核候选，不批准或发布知识。`advance_clock` 不开放；发布/回滚只能由复核员确认 Agent 的提议；图/时序查询只走白名单参数化查询。路由全集是 `contracts.API_ROUTES`，生成 `openapi/plantkg.openapi.json`。

## HTTP 框架（交 B02 落地）

用 Starlette（FastMCP 已依赖它）在同一 ASGI 进程里同时挂 REST（`/api/v1`）与 FastMCP streamable HTTP（URL 以 `/mcp` 结尾，07 Q04 实测可用），uvicorn 运行，保持 SQLite 单写者。请求与响应用 contracts.py 的 pydantic 模型校验；**OpenAPI 以本仓库生成的 `contracts/openapi` 为准**，不以框架自动生成的文档为准。不引入 FastAPI 等额外框架。
