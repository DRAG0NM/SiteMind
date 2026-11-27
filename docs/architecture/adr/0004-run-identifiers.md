# ADR-0004 两种运行标识：agent_run_id 与 run_id

- 状态：已决（09 D29）；B01 冻结
- 影响分支：B04、B07、B16、B17、B19、B20、B35、B37

## 决定

| 标识 | 含义 | 由谁分配 | 出现在 |
| --- | --- | --- | --- |
| `agent_run_id` | 网关登记的一次 Agent 运行（一次北向调用及其全部工具调用） | B19 网关 | RunSession、Context、UIEvent、EvidenceEvent、PendingAction.proposed_by、Job |
| `run_id` | 一次回放或业务运行：固定一个回放时钟与一份版本清单 | B04 RunStore | Context、StateSnapshot、Plan、Ledger、ExecutionResult、EvidenceEvent、ToolResult、Job |
| `trace_id` | 一次请求链路，日志与证据关联 | 入口（网关/REST/离线） | Context、RunSession、EvidenceEvent、UIEvent |
| `call_id` | plantkg 为每次工具调用分配 | 工具注册层 | EvidenceEvent、UIEvent |

- 一次 Agent 运行可以不产生、也可以产生多个业务运行（每次 `run_day` 建一个）。
- 离线入口（`loop.py`）只有 `run_id`，`Context.channel=offline` 时契约禁止 `agent_run_id`。
- 两者不共用字段、不互相推导；查询端点分别按 `run_id`（`/runs/{run_id}`、`/evidence?run_id=`）与 `agent_run_id`（`/evidence?agent_run_id=`、`/agent/stop/{agent_run_id}`）。
- 运行类结果必须带 `run_id` 与 `version_manifest_id`；`PlanningRequest` 契约校验两者存在。
