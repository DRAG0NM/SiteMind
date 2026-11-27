# ADR-0007 JudgeResult 与一致性门控

- 状态：已决（2026-10-29，07 Q06，09 D17，development/11）；B01 冻结
- 影响分支：B22、B23、B24、B31

## 决定

`JudgeResult` 字段：`choice`、`score`、`prob`（可空）、`coverage`、`backend`（`jev`/`fallback`/`fake`）、`model`、`calibration_version`、`orders`、`order_agree`、`cross_backend_agree`（只跑一个后端时为空）、`top_k`（≤ 3，前 3 名及概率）、`candidate_recall_source`、`gate_accept`。

- 流程：向量从完整类别表召回约 30 个候选 → 判别后端打分 → 一致性门控。`jev` 默认（百炼 `decision-model-preview`，华北 2，preview），3 种选项顺序取平均后归一。
- **不放 Unknown 选项**：`choice` 为“其他/无法确定/unknown”等时契约拒绝；“无法确定”由门控转人工表示。
- **门控**：至少 `JUDGE_MIN_ORDERS=3` 种**不同**选项顺序的第一名都等于 `choice`，且候选来自向量召回、`vector_top == choice`，才 `gate_accept=true`。契约逐项校验：顺序不足、顺序重复、任一顺序不一致、缺 `vector_top` 或与之不一致，都不能自动接纳。否则转人工，复核卡片展示 `top_k`。
- **每种顺序自身一致**（`OrderResult`）：`order` 无重复；`distribution` 恰好覆盖 `order` 的全部选项（缺分数不能补 0）、取值在 0～1；`choice` 必须是 `distribution` 的唯一第一名。第一名平分或后端无法判定时 `choice` 为空，这条结果不算一致，不能支持自动接纳。各顺序必须是同一候选集的排列，`choice` 与 `vector_top` 必须属于该候选集。
- **score 与 prob 分开**：不从 score 推断概率；有 `prob` 必须给 `calibration_version`，校准产物按后端与模型版本隔离，校准样本与测试集分开。prob 用于 ECE、复核量-F1 与反事实评分，不单独决定放行。
- 反事实评分（09 D31）中 `prob` 为空的条目排队首、不打分。

## B24 兼容扩展（2026-11-04）

依据 v0.8 B24 任务卡，新增 `gate_kind`，默认 `ontology` 保留上述本体门控；`domain` 派域题不要求向量首选，只检查三种不同顺序的唯一第一名一致。新增 `distribution` 保存全部选项的未校准平均归一分布，旧记录省略时为空。`prob` 仍只保存带校准版本的校准概率；`top_k` 保存原始分数的前三名。派域门控不能作为执行安全检查。公共契约改动与生成 Schema 随 B24 PR 提交，依据 12 开发规则与 branch-plan 公共热点规定，不另建阻塞流程。

派域题的 `candidate_recall_source` 不能为 `vector` 且 `vector_top` 必须为空，契约与后端均校验。少于三个候选时可输出分布，但不足三种不同排列，`gate_accept` 必为 false；B27 走主 Agent 慢路径，B30 走复核。B27 卡片未规定是否可能缩减到二元派域，因此不能假设其候选数始终不少于三。
