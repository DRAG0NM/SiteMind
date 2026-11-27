# 暖通专家（B21）的 Nexent 配置说明

B23 的导入命令只读 `nexent_assets/agents/*.yaml`（`load_assets` 要求文件名等于 Agent 名），所以这位专家的
真实提示词、工具与 Skill 绑定写在 **`../plantkg_expert_hvac.yaml`**，本目录放这个专家自己的说明与导入步骤。

| 内容 | 位置 |
| --- | --- |
| 提示词、模型、工具（`hvac_propose`、`list_assets`）、Skill 绑定 | `../plantkg_expert_hvac.yaml` |
| L2.5 领域要点 Skill | `../../skills/domain-hvac/`（`SKILL.md` + `config/config.yaml`） |
| MCP 工具实现 | `plantkg/experts/hvac/install.py`（登记 `hvac_propose`，B23 包装成 `plantkg_hvac_propose`） |

## 导入（替换 Nexent 里的占位配置）

```powershell
# 1. 起 plantkg（让 Nexent 容器能回调 MCP），另开终端
python -m plantkg --profile dev --set server.host=0.0.0.0 --set nexent.enabled=true --set storage.enabled=true
# 2. 导入（只按名字更新自己的 Agent，不碰租户里其他 Agent）
python -m plantkg.integrations.nexent.import_assets --credentials "$env:USERPROFILE\.plantkg-probe\tenant-dev.json"
```

Skill 与 Agent 的 `placeholder` 在 `../plantkg_expert_hvac.yaml` 里已是 `false`，导入时会上传并绑定 Skill。

## 现场情境怎么来

`plantkg_hvac_propose` 只拿得到本次运行的绑定（事件单、轮次、触发时刻），当分钟的区域温度与得热由会诊方
（B28）经 `hvac_situation` 钩子注入：`fn(context, binding) -> (event, state)`，`state` 的形状见
`HvacExpert.propose`，正好是 `ThermalSim.situation(world_state)` 的返回值。没注入时工具返回保守提案（不编数字）。

没接 Nexent 也能看效果：`python -m plantkg.experts.hvac.demo configs/scenarios/factory_demo_day.yaml`。

## 说明

井道通风的负荷目前挂在车间排风机 EXF-P1 上（站内没有电梯井道风机，这是替身，记入暖通类别），换成真实井道风机后改 `ThermalSim.fan_eid`。
