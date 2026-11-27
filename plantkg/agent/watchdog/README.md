# B42 Watchdog

一条命令运行 synthetic 模型中断、重新检查失败、事件单受阻、服务恢复及原因查询演示：

```sh
python -m plantkg.agent.watchdog.demo
```

启用存储并装配 B27 后，服务每 60 秒巡检。巡检目标由 B27 的 `watchdog.tickets` 钩子每次给出当前运行视图：主路由用 `plant_id`，协同组场景运行用 `plant_id#run_id`，受阻写回与结论也落在同一作用域。B34 工作台「待处理事项」显示 `/api/v1/watchdog/blocked` 返回的受阻事件单；在主 Agent 提问框输入“为什么 TK-0001 一直没有处理完成？”查询记录。此处是当前运行状态，不随回放时间轴变化。无存储或 B27 未装配时接口返回 503。

Nexent 使用配置的北向服务 `/nb/v1/health`；模型健康地址由可信服务端环境变量 `PLANTKG_MODEL_HEALTH_URL` 配置（必须是无需凭证的健康检查地址），未配置显示 unknown。检查端点不支持或需要认证时不报告正常。后台队列经 B04 `JobRunner.health` 端口读执行器状态；未接端口时退回只读统计并标 unknown。依赖巡检结果可从 `/api/v1/watchdog/health` 只读查询。运行记录先读 B27 的 `b27_agent_runs`（按事件单作用域），没有记录时退回 B23；都没有才把原因标未知。

仅对同一失败运行的同一依赖执行一次重新检查；不重试业务任务、不改权限、不替代人工确认。失败时仅在事件单仍为 open、轮次未变且未运行时标 blocked；连通恢复不会自动执行原业务操作。受阻结论与恢复依据写在 B27 `ticket_state` 的 `watchdog_conclusion`，事件单关闭或出现新的失败 episode 前一直沿用，60 秒巡检不会把已恢复结论覆盖成“未知”。人工等待、试探与活跃任务不作为停滞。

要在 B34 界面查看同一 synthetic 故障，先在 `web/` 执行 `npm ci && npm run build`，再执行 `python -m plantkg.agent.watchdog.demo --serve`，打开 http://127.0.0.1:8700。界面使用 B34 的构建产物与 B42 的临时内存演示服务，不修改现有运行数据。

## 人工解除受阻

依赖恢复不会自动解除受阻。维护者先查询 health、blocked 和相关运行记录，确认故障已排除、无需等待人工确认或其他任务，再通过 B27 的 `store.set_ticket_status(ticket_id, 'open')` 人工解除对应事件单（需在服务端取得当前厂区的 RouterStore）；此操作不重派任务。应避免与事件单处理并发执行，并核对解除后的状态；尚有其他受阻原因时不要解除。
