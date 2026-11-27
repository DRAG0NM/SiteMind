# Nexent Agent 配置（B23）

每个 yaml 是一个 Agent：主 Agent `plantkg_main` 与四位专家 `plantkg_expert_{energy,elevator,hvac,equipment}`。
文件名必须等于 Agent 名（Nexent `tool_params` 按 Agent 名分桶，写展示名会静默不注入）。
现在除电梯专家（B20 已替换为真实提示词、工具与 `domain-elevator` Skill）和暖通专家（B21，说明见 `hvac/README.md`）外都是占位配置（`placeholder: true`）：
其余专家的提示词、工具与 L2.5 Skill 由 B19 / B22 替换，主 Agent 的 triage / arbitration / correction-evolve
由 B27 / B28 / B32 替换；改完 yaml 重跑导入命令即可。`exports/` 是导入后从 Nexent 导出的快照。

## 怎么看效果

前提：本机 Nexent 在跑（Docker），北向 Key 放在环境变量 `NEXENT_API_KEY`，管理端凭据在仓库外的 json 里。

```powershell
# 1. 起 plantkg（让 Nexent 容器能回调 MCP），另开一个终端导入 Agent
python -m plantkg --profile dev --set server.host=0.0.0.0 --set nexent.enabled=true --set storage.enabled=true
python -m plantkg.integrations.nexent.import_assets --credentials "$env:USERPROFILE\.plantkg-probe\tenant-dev.json"
# 2. 停掉第 1 步的服务后跑演示：同一事件单两位专家并行会诊 + 问主 Agent 一个问题，打印 agent_runs
python scripts/nexent_demo.py --experts energy,hvac --ask "能源这边今天下午峰段要注意什么？问一下能源专家。"
```

演示输出里能看到：两路专家运行时间重叠、各自的提案 ID 与轮次；每次工具调用的“模型填值 → 系统绑定值”；
问答路径里主 Agent 调用专家子 Agent（depth=1），用户只收到主 Agent 的回答。
Nexent 界面的对话记录里也能看到主 Agent 调用子 Agent 的轨迹。工作台走 `POST /api/v1/agent/run`（SSE 返回 UIEvent）。
