# Issue #232 本地功能验收记录

验收日期：2026-11-15。依据 main `a7439be458e84e3b67961ba07142a6eeafcd9a0c`、内部设计文档 `227a736420dd1c403d1da5806352da968d4c2252`，沿用 `fix/issue-232-knowledge-ingest`。操作仅限本任务工作树，未修改或合并队友 PR。

## 实现范围

- 经既有运维身份、注册表和契约路由接收 UTF-8 资料，复用 B06 不可变 blob、内容散列、幂等及版本血缘。
- 上传资料经 B26 动态源进入本地确定性专家检索，返回真实资产及行定位；复用 B30 规则、本体候选。
- 复核、拒绝、真实 B32 回测、人工确认发布均沿既有流程；回执投影真实动作、报告与清单，上传不冒充发布。
- 页面刷新恢复资产回执，正式图谱读取固定清单的后端三元组。保留显式 synthetic 离线演示，切换时清空前一模式结果。
- 使用说明、HTTP 请求和独立 synthetic 初始化脚本见 [资料入库说明](../integration/knowledge-ingest.md)。

## 实际浏览器和 HTTP 证据

独立数据目录为本机 `E:/PlantKG/outputs/issue-232-browser-proof-20261115`；未覆盖已有运行。Chrome + Playwright 通过真实 DragEvent/DataTransfer/File 拖入手册，访问运行中的 Vite 和 Uvicorn，无 fetch mock。持久化摘要与确认记录保存在 [验收证据 JSON](issue-232-evidence.json)。

| 对象 | 实际值 |
| --- | --- |
| v1 资产 | `asset://manufacturing/4f1bfb5c/process_doc/source` |
| v1 完整 SHA-256 | `4f1bfb5c85e1505070be6de368a1cf2f6a84691c8a1aa24966f6dee066153edd` |
| v2 资产 | `asset://manufacturing/e36300eb/process_doc/source` |
| v2 parent | v1 资产 ID |
| 本体候选 | `ont-7ce629fa546bf033` |
| 复核确认动作 | `act-614d3d467f958fd4` |
| 发布确认动作 | `act-849ee2c7591bbe8a` |
| B32 实际回测报告 | `b32-report-8d13de284d0ba519`，`pass`，`fast_window` |
| 正式清单 | `vm-0a544b489145ca81` |

执行结果：

1. 浏览器上传 v1 返回 HTTP 200、`needs_review`，存在规则及本体候选，`published=false`；原文显示后端第 1 行。
2. 本体 review 提议经统一确认接口确认，随后 `propose_publish` 执行实际 B32 baseline/candidate 回测。确认独立 publish 动作后，回执为 `published=true`。
3. 正式图包含 3 条后端三元组：AC-2 的 Compressor 类型、确认动作中的复核员 ID、指向 v1 资产的 `sourceAsset`。浏览器读取并显示 4 个节点、3 条边，G6 canvas 实际可见。
4. 沿用同一资料编号上传不同内容 v2，生成新资产且 parent 指向 v1；v2 未自动发布。同键重试不生成第三版。旧资产原文仍是 0.2，v2 为 0.3。
5. v2 接收之前的 `as_of` 检索不到 0.3，读取 v2 回执返回 404；正式清单发布之前读取该图返回 404。已发布旧清单三元组在 v2 入库后保持相同。
6. 正式发布后再次停止并重启后端，浏览器仅凭 URL 中资产 ID 恢复回执和正式图，G6 canvas 可见，未产生 pageerror；切换离线演示后真实节点及恢复参数被清空。
7. 只读 SQLite 与磁盘核对：两个 asset_versions 记录对应两个实际 blob，重新计算完整 SHA-256 均相等。B06 只为 v1 记录发布后的 `in_graph`；未滥记执行引用。

本机截图：`browser-upload.png`、`browser-refresh.png`、`browser-restart.png`，均位于上述独立目录。重启截图等待 G6 canvas 完成后取得。

## 集中验证

- 后端任务专项：3 项通过，包含输入拒绝、授权、幂等、版本、旧时点、专家隔离、实际 B26 原文检索、存储失败、处理失败重试、候选拒绝、实际回测发布、跨厂区及重启恢复。
- 实体编号边界修复后，独立复跑上传专项、B06 和本体相关回归：64 项通过（351.74 秒）。拒绝将 `AC-20` 子串误关联为 `AC-2`，原文定位使用相同匹配规则。
- 前端全量：28 个文件，136 项通过；包含资料页 7 项。
- B36 两次完整演示：各 14 个事件、2880 个分钟账本，全部 2880 分钟平衡；运行原有验收 5 项通过，包括 B32/B47 真实业务路径及两次完整记录一致性。
- 契约生成物：128 个文件 `--check` 通过；契约样例专项 189 项通过。
- TypeScript + 主工作台构建、B49 showcase 构建、模块边界、所改 Python 静态检查及 `git diff --check` 通过。

后端全量 `python -m pytest -q`：1720 项全部通过，0 失败、0 跳过，耗时 2856.19 秒（47 分 36 秒）。末次实体编号匹配收紧后另行复跑的 64 项相关专项全部通过，覆盖最终匹配实现。

## 兼容性与限制

复用 AssetRepository、PendingActionStore、VersionStore、GraphStore 和现有钩子，未改公共端口签名；B17/B36 day 结构与 B49 读数字段无变化。未修改 PR #267 的页面/读数实现、PR #268 的资产统计实现或其工作树。#268 与本任务共享 contracts/OpenAPI，合并时按最终契约重新生成；#266 与本任务在 bootstrap、rules/pack、rules/tools 有交集，本任务仅追加资料接线及来源时点可见性检查，未修改 B32 行为。

本地运维脚本遵守 SQLite 单写者，必须暂停同库服务后提出动作，再重启服务经既有确认路径确认；在线继续使用既有网关工具。初次验收使用的 B17 临时场景引用在独立进程中无法校验，已改为持久化 B32 初始策略，不放松完整性检查。

全部业务资料、场景和复核员均明确为 synthetic。dev/demo/mock 的本任务快回测缺月电价采用已标注 synthetic 的 0.62 元/kWh，不代表公告电价或工厂实测。未启动或验收付费模型、在线 Nexent 或真实工厂业务；待业务验收。

非阻断 P2：[Issue #269](https://github.com/DRAG0NM/SiteMind/issues/269)：B30 候选经真实 B32 回测后仍保留 fallback 策略名称。实际报告和发布门禁正常，不在本任务跨包修改。
