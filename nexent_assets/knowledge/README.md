# 专家知识库（B26）

四个专家各一个 Nexent 知识库，资料按专家放在 `data/docs/<专家>/`。本目录的内容：

| 文件 | 内容 |
| --- | --- |
| `catalog.yaml` | 资料清单：哪位专家、标题、类型（文本/表格/图片）、来源等级、生效时间、数据性质、激活事件 |
| `indexes.json` | 真实上传结果：每个专家的 Nexent 索引名、每份资料上传后的 `path_or_url` 与处理状态（`scripts/nexent_kb_upload.py` 生成） |
| `live_checks/` | 本机 Nexent 实测证据：索引隔离、网关注入表、真实专家问答、按 as_of 注入、工单挖掘的 Agent 解读、主 Agent 工具绑定 |

## 本机实测（Nexent v2.8.2，2026-11-04 / 11-05）

| 专家 | Nexent 索引 | 入库 | 说明 |
| --- | --- | --- | --- |
| elevator | `2-1cec13e7528644238749368350d42b46` | 4/4 | 规程、维护手册、点检表、困人记录 |
| hvac | `3-37a28fbf8b3943f9a837ee7a0d44e9d6` | 4/4 | 设计参数、冷水机组手册、设定值表、维保记录 |
| energy | `4-4a6c9aea13fa479e8e5c2fb33f8e313d` | 3/4 | 电价摘要、储能手册、光伏曲线已入库；426 号文 PDF 处理失败（见下） |
| equipment | `5-68a54cfcfa064dd89a6e92fcc6283c42` | 5/5 | 旧版运行规程、空压手册、历史工单、点检表、铭牌结构化描述 |

嵌入模型 `qwen3.7-text-embedding-flash`。图片不直接进知识库：`data/docs/**/*.md` 是 deepseek-flash
（`deepseek-v4.1-flash-1017`）识读后的结构化描述，上传与检索都用这份描述。

`index_names` 写在 `nexent_assets/agents/plantkg_expert_*.yaml`；每次运行由网关经 `nexent.document_paths`
钩子按 `as_of` 注入可见文档（清单见 `live_checks/document_paths_injection.json`），不把 `document_paths`
写死在 Agent 配置里。

## 历史工单挖掘（D54）

- 流程段在 `plantkg/knowledge/documents/workorders.py`：按（设备，故障现象）分组，再按 90 天时间窗切类；
  不按语义合并不同写法。
- Agent 段在 `mining.py`：`AgentInterpreter` 把每类的工单原文交给设备监测专家解读，要求一行 JSON
  （是否真规律、根因、候选规则）；离线与测试用 `RuleBasedInterpreter`（synthetic）。
- 候选规则是 `Candidate(kind=knowledge, changes={rules: …})`：`KnowledgePublisher` 已登记时调
  `propose_publish`；未登记时由 `AssetCandidateStore` 落成 B06 的 `rule_candidate` 派生资产，
  记录里带候选规则与引用的工单原文（可点开核对）。
- 主 Agent 慢通道工具：`workorder_search`（契约 `TOOL_SPECS`，provider B26），按设备、现象、时间段
  返回工单原文摘要与聚类；按 `ctx.as_of` 只读可见工单，`page_size` 上限与契约一致（50，大页只回摘要字段）；
  不进专家私有库，专家仍只经自己的 `document_paths` 检索。

本机实测（2026-11-05，`live_checks/workorder_mining.json`）：11 张工单聚成 4 类；设备监测专家对 4 类
逐类解读，判 3 类为重复规律并各生成一条候选规则（含 AC-2 主管压力下降 6 张工单的预警规则），
1 类单张工单判为非规律；3 条候选落库，引用工单 10 张。

## 注入与失败关闭

网关钩子按 `strict=True` 调 `KnowledgeLibrary.paths`：没有 `indexes.json` 或某份该入库的文档未完成上传时抛
`PRECONDITION`，不退回派生路径、也不静默剔除文档；`kb_upload: false` 的资料（目前只有解析失败的 426 号文 PDF）
不计入上传检查。离线演示与测试用 `strict=False`，才按派生路径展示。

## 已知问题

- `fixtures/packs/tariff/sources/js-2025-426.pdf`（清单直接引用，不复制）在 Nexent 里状态 `PROCESS_FAILED`（data-process 解析 PDF 失败），
  清单里标 `kb_upload: false`，按设计不进检索。政策原文仍作为 B06 资产入库；检索用同目录的
  `tariff-summary-2025.md`（据原文与仓库已核对方针缩写，标 derived）。重传或换解析器后可改回 `kb_upload: true`。
- 换了 Nexent 租户或重建索引后，索引名会变。重跑上传脚本后要把 `indexes.json` 里 `experts.*.index_name`
  的新值同步到 4 个 `nexent_assets/agents/plantkg_expert_*.yaml` 的 `knowledge.index_names`，再跑一次
  `import_assets`；只改 `indexes.json` 不会更新 Agent 绑定的索引。
- 2026-11-05 8700 空出来后已重新导入 Agent：`plantkg_main` 实际绑定 `plantkg_workorder_search`
  （导出见 `nexent_assets/agents/exports/plantkg_main.export.json`）。导入时还报了一条
  “`plantkg_main` 实际 Skill ['triage'] 与配置 [] 不一致”，那是 B27 的占位 Skill 在平台侧残留，与本卡无关。
- `indexes.json` 的 `index_names` 只是平台导出的默认参数（空数组）；实际检索范围由 yaml 的
  `index_names` 加网关每轮注入的 `document_paths` 决定，见 `live_checks/document_paths_injection.json`。
- 证书与密钥不进仓库：管理端凭据在仓库外 json，模型密钥只在环境变量里。

## 怎么复现

```powershell
# 1. 起 plantkg（Nexent 容器要能回调 MCP）并导入 Agent，另开终端
python -m plantkg --profile dev --set server.host=0.0.0.0 --set nexent.enabled=true
$env:NEXENT_API_KEY = Get-Content "$env:USERPROFILE\.plantkg-probe\nb_key.txt" -Raw   # 北向 Key
python -m plantkg.integrations.nexent.import_assets --credentials "$env:USERPROFILE\.plantkg-probe\tenant-dev.json"

# 2. 重新上传资料（幂等，已完成的同名文件跳过）
python scripts/nexent_kb_upload.py --credentials "$env:USERPROFILE\.plantkg-probe\tenant-dev.json"

# 3. 离线演示：入库、图片描述、隔离检索、注入清单、激活率
python -m plantkg.knowledge.documents

# 4. 真实平台验证（会发请求）：同一个电梯问题分别问电梯与暖通专家
python scripts/nexent_kb_demo.py --experts elevator,hvac

# 5. 验证按 as_of 注入：2025-12-01 时储能手册还没生效，同一问题答不出 SOC 规则
python scripts/nexent_kb_demo.py --experts energy --as-of 2025-12-01T00:00:00+00:00 `
  --query "储能低 SOC 时怎么处理？引用知识库里的规则回答；一句话即可。"

# 6. 历史工单挖掘：确定性聚类 → 设备监测专家解读 → 候选规则（需要 8700 空闲；先导入 Agent）
python scripts/nexent_workorder_mining.py --import-agents
```

实测结论：电梯专家引用自己库里的《电梯安全技术规程》5.12.1 原文；暖通专家回答自己库里没有电梯
应急救援资料（见 `live_checks/expert_kb_answers.json`）。混合检索总会返回 top_k 条，隔离判据是返回与存储的
文件全部属于该专家，跨专家 0 条。

`live_checks/as_of_filtering.json` 是同一个储能问题在 2025-12-01 与默认 2026-06-30 的两次真实回答：
前者只能看到电价摘要，答“知识库没有低 SOC 规则”；后者能看到储能手册，引用了 4.2 节 SOC 管理规则。
这证明注入的 `document_paths` 确实按 `as_of` 过滤，而不是整库检索。
