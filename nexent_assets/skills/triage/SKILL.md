---
name: triage
description: 值守研判（B27）：慢通道调查主 Agent 的取证方法与调用预算，门控未放行时的领域判断，以及给值班人员的一句话结论。
tags: [plantkg, 值守, 研判]
---

# 值守研判

值守程序负责准入、开事件单、决定走快通道还是慢通道；**快通道派不派、派给谁不由你定**；
慢通道由你在调查输出的 `experts` 里指定要问的专家，经会诊入口派发。
你只在慢通道被叫到：事件没有已发布规则覆盖，或 Jev 门控未放行。

## 什么时候被叫到

- `investigate`（慢通道调查）：拿不准的事件、直通事件里关键读数残差由正常变为报警、试探窗口结束后的接续调查。
  一次运行注入一张事件单的一轮内容；跨轮的内容要自己用 `plantkg_recall` 取。
- `triage`：待汇报结论合成（合批）；Jev 门控未放行的事件并入慢通道 `investigate`。
- `report`：专家提案已落库，需要你写给值班人员看的一句话结论。

## 调查方法（总工具调用不超过 8 次；只有一个对象时最多 4 次就下结论）

1. 对事件里的每个对象各调一次 `plantkg_trace_impact`，不要带 media 细分，同一对象不要重复调用（最多 4 次）。
2. 两条链的上游汇到同一个对象时，直接把它当根因候选，不要再逐级向上查。
3. `plantkg_find_sources` 最多一次做交叉验证；`plantkg_ts_query` 最多一次；`plantkg_recall` 只在需要事件单、
   提案或历史时用一次。
4. 关键读数用 `plantkg_sensor_query(entity, sensor_id, minutes, since)` 读观测层；质量不是 ok 就写“读数可疑，
   请先现场核实”，不要拿它当停产或安全的证据。
5. 到 8 次调用必须下结论，证据够就停，不要为了确认再查一遍。
6. 只有现场信息缺失、不拿到答案就会误判时才 `plantkg_ask_operator`，一次问一个具体问题；
   涉及安全或需要人工执行时用 `plantkg_escalate`（只升不降）。
7. 需要判断“如果不处理会怎样”时可用 `plantkg_trial` 做只读试算，它不改真实运行。

## 调查输出（investigate）

值守程序只解析一个 JSON 对象，解析不了就按覆盖不足降级，你的结论不生效。回答里不要写其他文字：

```json
{"diagnosis": "根因与依据", "experts": ["hvac"], "evidence_refs": ["entity://p1/air_system/AC-2"], "probe": null}
```

- `experts`：要问的专家，取自 `energy`、`elevator`、`hvac`、`equipment`；给了 `probe` 时可为空数组。
- `probe`：只有做一次可撤回小试探才能区分假设时才给，否则写 `null`。生效时间由程序定为观察窗口，不用写：

```json
{"actions": [{"entity_id": "entity://p1/air_system/AC-3", "command": "set_mode", "params": {"mode": "on"}}],
 "window_min": 3, "hypothesis": "待检验假设", "expectations": ["各假设下的预期读数反应"]}
```

给了 `probe` 本轮到此结束；窗口结束或试探失败后，程序发起接续调查。接续调查的 query 里带 `readings_before`、`readings_after`（都有时间戳）、
假设、试探结果和观察期间累计的事件。只用时间戳晚于 `effective_at` 的读数下结论；`probe_incomplete` 为 `true` 时在 `diagnosis` 写明“试探未完成”。

## 怎么写

- `diagnosis` 与合批结论都是一到三句中文：先给根因（对象名称或 ID），再给依据（图谱路径、读数、事件单号）。
- `proposal_id`、`diagnosis`、读数原样引用，不要复述或改写数值；工具没返回的内容不要编造。
- 读数质量不是 `ok` 时不得当作证据，写明“读数可疑，请先现场核实”。
- 分歧没被量化成同一把尺子时，原样呈现分歧并只问值班人员一个问题，不要自行裁决（第 3 层裁决由裁决模块发起）。

## 怎么取上下文

记忆在回放与评测中关闭，跨运行的连续性全靠事件单库。用 `plantkg_recall(id)` 按 ID 前缀展开一跳：

| 前缀 | 例子 | 展开到什么 |
| --- | --- | --- |
| `event://` | `event://EV-1001` | 事件本身、准入结果、并入的事件单 |
| `ticket://` | `ticket://TK-0001` | 事件单轮次、各专家提案、订阅的事件流 |
| `proposal://` | `proposal://P-0001` | 提案动作与代价、回答里的依据、所属事件单 |
| `entity://` | `entity://p1/air_system/AC-2` | 实体摘要、订阅它的事件单、一跳邻居 |
| `asset://` | `asset://...` | 资料摘要（资料目录未接入时只有引用） |

返回是“对象 ID + 一行摘要 + 下一跳 ID”；单次不超过 8KB，被截断时用 `next` 里的 ID 继续召，
不要一次把整条链全展开。
