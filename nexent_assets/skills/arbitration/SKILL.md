---
name: arbitration
description: 专家提案冲突或动作依赖诊断时的仲裁规则：安全标签硬否决、同一场景快照的组合比对、needs_human 与 needs_diagnosis 动作的流转与升级。
tags: [plantkg, arbitration]
---

# Arbitration

Use this skill when expert proposals conflict or an action depends on a diagnosis.

1. Treat any safety tag as a hard veto. Escalate through the registered urgent handler and do not execute the affected action automatically.
2. For ready actions, compare combinations from the same scenario snapshot. Reject every TrialRunner result with `feasible=false`, then score feasible results with the active policy weights for CNY, wait minutes, and comfort minutes.
3. Keep `needs_human` actions in the operator workflow. Keep `needs_diagnosis` actions pending until the diagnosis is available.
4. Send disagreements that cannot be quantified to the arbitration agent with the proposal IDs, concise evidence, and active priority profile. Ask the operator one question only when required field information is missing.
5. Return the decision as conflict, conversion, and result records. Never treat a rehearsal proposal as an executable decision.
