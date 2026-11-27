# B42 Watchdog 状态

- 已实现周期性依赖检查、B27 事件单诊断、有界重新检查与记录、只读查询和 B34 受阻展示。巡检视图经 B27 `watchdog.tickets` 钩子取当前运行作用域（主路由 `plant_id`、协同组运行 `plant_id#run_id`），受阻写回与 `watchdog_conclusion` 结论写在同一作用域；运行记录先读 B27 `b27_agent_runs`，没有时退回 B23；后台队列经 B04 `JobRunner.snapshot/health` 端口读。
- 恢复只重新检查依赖：每个事件单 / Agent 运行 / 依赖组合最多一次，持久认领防止重复；不重派任务、清除数据或绕过人工确认。恢复失败且事件单仍空闲时标记受阻，恢复连通后不自动解除受阻；人工解除步骤见 README，业务仍由原流程处理。恢复结论持久化，后续巡检沿用，不因 60 秒轮询丢失。
- 长时间无新运行与重复相同调查结论是供维护者核查的线索；运行中的任务超出预算且两次进度不变时提示核查，不中断任务。无凭据的原因显示未知。
- 数据性质：演示与专项验收使用 synthetic 替身；真实 Nexent 北向健康端点已检查连通及客户端断网恢复；未停启 Nexent 服务，真实模型健康端点和现场值守数据待业务验收。模型需配置 `PLANTKG_MODEL_HEALTH_URL`，未配置为 unknown；没有 JobRunner 端口的旧装配退回只读统计并把队列整体标 unknown。自然语言查询使用事件单编号匹配和基于证据的固定答复，不作语义推断。
- 容器验收：`tests/agent/watchdog tests/contracts/test_examples.py tests/agent/router tests/test_b34_day_api.py` 253 passed（合入 main e9e88619 后）；全量 pytest 1254 passed（上一轮）；`python scripts/check_boundaries.py` 通过；`python contracts/build.py --check` 通过。synthetic 演示与工作台 HTML/API 验证通过。
- 前端容器：`npm run build` 通过，`npm test` 11 passed；构建提示现有主包超过 500 kB。
- 跨模块接口：B04 队列快照及执行线程健康接口见 https://github.com/DRAG0NM/SiteMind/issues/69；默认装配已接 `JobRunner` 端口，旧装配或端口缺失时退回只读 SQLite 统计并标 unknown。
