---
name: plantkg-tools-l1
description: plantkg 工具的通用用法（L1）：返回格式、错误码、分页、后台任务与专家提案。所有 plantkg Agent 都适用。
tags: [plantkg, L1]
---

# plantkg 工具用法

- 工具名都以 `plantkg_` 开头。参数里没有 `ctx` 要填：身份、厂区和查看时点由系统绑定，模型填的同名值无效。
- 返回是 JSON 文本。以 `PLANTKG_E_<代码>:` 开头表示失败，照实告诉用户原因，不要重试同样的参数；
  以 `PLANTKG_NOTICE_NO_CTX:` 开头表示只读降级（不是从工作台发起的运行），不能写入。
- `business_status` 为 `pending` 时带 `job_id`：用 `plantkg_get_job(job_id, wait=40)` 查结果，最多查 3 次，不要自己估算结果。
- 分页工具返回 `page.next_cursor` 时，需要更多再把它填进 `cursor`；单次返回不超过 8KB。
- 专家的 `plantkg_<域>_propose` 只在事件单会诊里调用，它把提案落库并只返回 `proposal_id` 与摘要；
  回答里引用 `proposal_id`，不要复述或改写数值。
