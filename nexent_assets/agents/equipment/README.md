# 设备监测专家的 Nexent 配置（B22）

导入命令 `python -m plantkg.integrations.nexent.import_assets` 只扫描 `nexent_assets/agents/*.yaml`，且文件名必须等于
Agent 名。所以本专家的配置在上一级的 `plantkg_expert_equipment.yaml`（B22 已替换 B23 的占位），
本目录只留说明，避免出现两份同名配置。

- 提示词与工具：`../plantkg_expert_equipment.yaml`
- L2.5 领域要点 Skill：`../../skills/domain-equipment/`
- propose 工具：注册名 `equipment_propose`（MCP 上为 `plantkg_equipment_propose`），由 B23 统一包装落库；
  本分支在 `plantkg/experts/equipment/` 提供业务函数，场景内走 `EquipmentResultSource`。
- 观测读数：注册名 `sensor_query`（MCP 上为 `plantkg_sensor_query`），B22 登记正式实现；
  返回传感器读数与 Quality、残差报警、候选假设与虚拟传感器推算值。

真实导入需要本机 Nexent 与仓库外凭据，本卡不调用外部 API，未跑导入。
