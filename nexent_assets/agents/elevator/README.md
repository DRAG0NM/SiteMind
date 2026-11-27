# 电梯专家 Agent 配置（B20）

B23 的导入命令只扫描 `nexent_assets/agents/*.yaml`（桶名必须是 Agent 名），所以可直接导入的配置是
[`plantkg_expert_elevator.yaml`](../plantkg_expert_elevator.yaml)，不是本目录下的文件。

- 提示词、工具与 Skill 绑定都在那份 yaml 里；本目录只放说明。
- `skills.domain-elevator` 对应 [`nexent_assets/skills/domain-elevator/SKILL.md`](../../skills/domain-elevator/SKILL.md)。
- 改完配置后在本机 Nexent 运行时执行：

```powershell
python -m plantkg.integrations.nexent.import_assets --credentials "$env:USERPROFILE\.plantkg-probe\tenant-dev.json"
```

导入会按 Agent 名更新 `plantkg_expert_elevator` 并重新发布；本文不调用外部 API。
