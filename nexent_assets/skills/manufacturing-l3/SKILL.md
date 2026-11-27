---
name: manufacturing-l3
description: 行业配置（L3）骨架：电子制造厂区的对象、事件类型、专家分工与汇报口径。具体取值在 config/config.yaml。
tags: [plantkg, L3, manufacturing]
---

# 电子制造厂区配置

用 `read_skill_config` 读取 `config/config.yaml`，其中给出厂区、四位专家的分工、事件类型与汇报口径。
汇报一律先给结论，再给依据（提案 ID、规则条款、资料版本）。涉及安全的动作只提议，由值班人员在工作台确认。
