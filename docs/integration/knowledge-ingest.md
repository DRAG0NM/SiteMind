# 工作台资料入库与正式图谱

Issue #232 的实现入口。上传只保存资产和生成候选；知识发布仍经过已有复核、B32 回测和 B04 人工确认。前端 `#knowledge` 默认连接后端，只有显式选择“离线演示”才运行原有 synthetic 本地匹配。

## 接口与持久化

受控例外 `POST /api/v1/assets:ingest` 使用现有本机 operator token 和配置复核员身份。Vite 代理从后端令牌文件注入请求头；浏览器不读取令牌。接受 UTF-8 `.txt/.md/.markdown/.json`，最多 1 MiB，不接收浏览器本地路径。JSON 必须能解析。请求示例：

```json
{
  "filename": "manual.txt",
  "text": "AC-2 空压机，主管压力5分钟内下降超过0.2 bar。",
  "expert": "equipment",
  "nature": "synthetic",
  "document_id": "manual-ac2",
  "entity_ids": ["entity://demo-electronics/air_compressor/AC-2"],
  "idempotency_key": "manual-ac2-v1"
}
```

`expert` 为 `equipment/hvac/elevator/energy`。新资料可省略 `document_id`；改版必须沿用回执中的编号，更换幂等键。相同键、相同请求重试复用结果；相同键不同请求报冲突。相同内容复用 B06 内容寻址资产，新内容沿用稳定来源并产生 `parent_version_id`。原文保存在 B06 不可变 blob，`b26_uploads` 只登记归属、接收时间、幂等请求与处理回执，不代替资产目录。

`entity_ids` 可省略；提供时必须已在 B06 登记，且原文含 ID 或唯一名称。未解析内容保留原文和待人工解释的规则候选，不凭关键词制造正式实体。规则模板抽取复用 B30；本体候选包含实际资产和行定位。`processing_failed` 表示原文已存储、候选处理失败，原键重试可继续；存储本身失败返回统一错误。

读接口均接受带时区且不晚于现在的 `as_of`。省略时沿用仓库默认查看时间，查看刚上传的内容应明确发送当前时点。

| 接口 | 内容 |
| --- | --- |
| `GET /api/v1/assets/{asset_version_id}/ingestion?document_id=manual-ac2` | 指定逻辑资料的来源、候选、审核、回测、动作与发布清单回执 |
| `GET /api/v1/assets/{asset_version_id}/fragments` | B06 不可变原文、真实行号、分页 |
| `GET /api/v1/knowledge/documents:search?expert=equipment&q=AC-2` | 本地确定性原文检索；按专家和接收时间隔离，分页 |
| `GET /api/v1/knowledge/graph?manifest_id=vm-...` | 已发布固定清单的正式三元组，分页 |

资产 ID 路径段需 URL 编码。B06 相同内容可能由不同资料共享资产版本，因此回执查询应同时提供 `document_id`；旧请求省略该参数时，仅在该时点唯一对应一个逻辑资料才返回回执，多资料匹配返回 409，调用方须补充资料编号。相同资料的重复接收仍取该资料最新回执。每页最多 200；`next_cursor` 继续读取。正式图谱必须指定清单且该清单在 `as_of` 已激活，旧清单不会混入新版图。页面在 URL 中同时保留资产 ID 和资料编号，刷新或后端重启后重新读取对应回执、原文及发布图谱；旧链接按上述唯一性规则恢复。前端还会核对响应身份，防止连接未支持资料编号的旧服务时显示其他回执。切换离线演示会清空上一模式结果。

## 专家检索与发布

动态上传源经 `knowledge.documents.library` 钩子加入现有 B26 `KnowledgeLibrary`，片段从 B06 快照读取，包含 `asset_version_id` 和 `line:N`。REST 检索是本地确定性适配器，**未宣称同步 Nexent 或运行真实模型**。静态资料及既有在线路径保留。

在线使用已有 `propose_review_decision`、`propose_publish` 工具和工作台确认卡。本体复核 `revision` 需从候选 options 选择 `choice`；规则复核需适用 `subject_id` 和 `effective_from`。确认复核不等于确认发布；B32 回测通过后仍需确认独立 publish 动作。拒绝候选不能发布。上传不触发设备执行。

提供本地比赛环境的离线运维脚本，直接复用注册工具，只创建待确认动作：

```powershell
python -m scripts.knowledge_review --profile demo --set storage.sqlite=E:/demo/catalog.db --set storage.graph=E:/demo/graph --set storage.series=E:/demo/series --actor dev-reviewer --reason 核对原文 review ont-... --decision accept --revision '{"choice":"https://brickschema.org/schema/Brick#Compressor"}'
python -m scripts.knowledge_review --profile demo --set storage.sqlite=E:/demo/catalog.db --set storage.graph=E:/demo/graph --set storage.series=E:/demo/series --actor dev-reviewer --reason 发布本体 publish ont-...
```

SQLite 是单写进程：**运行离线运维脚本前暂停使用同一库的服务**；运行后重启服务，再通过现有确认卡或 `python -m plantkg actions confirm ACTION_ID --actor dev-reviewer --reason 核对资料` 确认。每一步分别操作，不能把两条 proposal 命令直接连续执行；publish 前必须确认 review。脚本不新增审核 REST 写口，不绕过确认机制。

dev/demo/mock 下仅对本次上传产生的候选提供 synthetic 快回测配置：复用既有 B32 实际 baseline/candidate 场景评估，日期取复核时点次日，缺月计价基础补 0.62 元/kWh，产物与回执明确标记 synthetic。该值不代表公告电价，回测不代表真实工厂业务验收，不影响其他候选或 prod。

## 独立验收数据

```powershell
python -m scripts.knowledge_ingest_demo --out E:/demo-new-directory
python -m plantkg --profile demo --set storage.sqlite=E:/demo-new-directory/catalog.db --set storage.graph=E:/demo-new-directory/graph --set storage.series=E:/demo-new-directory/series --set actions.token_file=runtime/operator.token
# 另一终端，在 web/ 下：
$env:PLANTKG_ACTOR='dev-reviewer'
npm run dev
```

初始化脚本拒绝覆盖已有目录，生成两份不同内容的 synthetic 手册、已登记 AC-2 及 B32 持久化初始策略。无批准知识、无假回测。拖入 v1、沿回执处理本体候选并确认发布，刷新正式图谱；再沿用资料编号上传 v2，检查 parent 和旧时点隔离；最后重启服务检查恢复。

未启用存储的 profile 使用现有未装配拒绝响应，绝不降为假成功。B06 `identified` 在入库时记录，`in_graph` 仅在实际 B32 激活通知后记录；不滥记 `referenced`。B17/B36 day 格式、B49 读数以及既有公共端口签名不变。
