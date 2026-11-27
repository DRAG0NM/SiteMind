# B06 资料身份、实体标识与资产血缘

代码在 `plantkg/knowledge/assets/`，表由 `plantkg/storage/migrations/assets/B06_001_catalog.sql` 建立，测试在 `tests/assets/`。

## 两种身份

| 身份 | 格式 | 由什么决定 | 何时变化 |
| --- | --- | --- | --- |
| 资料版本 | `asset://{行业}/{完整 sha256 前 8 位}/{类型}/{定位}` | 文件字节；定位取不含扩展名的文件名：转小写，Unicode 字母、数字、`_`、`.`、`-` 保留（中文文件名会得到含中文的定位），其余连续字符改成一个 `-`，去掉首尾的 `-` 和 `.`，为空时用 `asset` | 内容一变就是新版本 |
| 稳定实体 | `entity://{厂区}/{类型}/{编号}` | 显式登记的编号 | 不随资料改版变化 |

- **去重用完整散列**：同厂区、同类型、同字节只有一个版本，无论文件在哪个路径。ID 里的 8 位短哈希只供展示；8 位相同而完整散列不同时拒绝（`STATE_CONFLICT`），不会误合并。
- **血缘**：每次接入（含命中已有版本）都记一条“来源路径 → 版本”。同一来源路径、同一类型的文件内容变化后再接入，新版本的 `parent_version_id` 指向该路径最近一次接入的版本。未显式指定实体时，新版本继承父版本的全部关联，每条关联的生效时间取“原关联生效时间”和“新版本可见时间”中较晚的一个，因此回放时既不会早于原关联，也不会早于新版本。内容改回旧版字节时返回旧版本，之后的改版以旧版本为父；同字节出现在新路径时，新路径的后续改版也能接上血缘。
- **资料与实体的关联单独存放**：版本身份只由内容决定，关联可以追加，但已有关联不删除也不改写。两台同型号设备共用同一份说明书时，字节相同得到同一版本，两个实体都关联在这个版本上。关联带生效时间，`get`/`list` 返回的 `entity_ids` 只含 `as_of` 时已生效的关联，回放时不会看到后来才建立的关联。
- **实体只按 ID 判同**：同名或同别名的两台设备是两个实体；`find_entities(名称)` 返回全部匹配，有歧义由调用方（B22 链接候选或人工）决定。多份资料关联同一 `entity_id` 即“多来源同设备”。同 ID 不同内容的登记拒绝（`IDEMPOTENCY_MISMATCH`）。
- **不可变原文**：接入时把字节按内容寻址复制到 `blob_dir/sha256/xx/<散列>`。`read_content` 返回该快照并校验散列，之后原文件被改也不影响已接入版本，规则来源能追到当初的原文。

## 接口

`SQLiteAssetCatalog(writer, plant_id, blob_dir, base_dir=...)` 实现 B01 的 `AssetRepository` 端口（`ingest`、`get`、`list`、`resolve_entity`），另有：

| 方法 | 用途 |
| --- | --- |
| `ingest(..., nature=, entity_ids=, title=, license=)` | 端口签名之外的可选项。未给 `nature` 时 `timeseries` 记 `measured`，其余记 `configured` |
| `ingest_detail(...)` | 同 `ingest`，另返回是否命中已有版本 |
| `register_entity` / `find_entities` | 登记稳定实体；按名称或别名列出候选 |
| `lineage(context, id)` | 祖先版本 ID，从近到远，只含当前可见的 |
| `read_content(context, id)` | 取不可变原文快照 |
| `record_activation(context, id, stage, ref=, available_at=)` | 登记使用事件，返回当前阶段 |
| `activation` / `activation_summary` | 单个资料的阶段；可见资料的阶段分布 |

`register(registry, catalog)` 登记端口、`ingest`/`list_assets`/`get_asset` 三个工具和 `GET /assets`、`GET /assets/{asset_version_id}`，按注册表规则替换 mock 的 Fake；工具规格取自契约 `TOOL_SPECS`，不另定义。

## 规则

- **厂区隔离**：一个目录实例只服务一个厂区；`context.plant_id` 不符返回 `CROSS_PLANT`，SQL 也按 `plant_id` 过滤。
- **可见性**：`list`、`get` 只返回 `available_at <= as_of` 的版本；存在但未到可见时间返回 `NOT_VISIBLE`。
- **写入**：`ingest`、`register_entity`、`record_activation` 需要可信写上下文（`context.can_write`），否则 `NO_CTX`。
- **幂等**：同内容重复接入返回原版本，可见时间沿用首次接入的（内容早已可见）。这次新指定的实体会追加为关联，生效时间取两次可见时间中较晚的一个；未登记的实体返回 `NOT_FOUND`。
- **路径**：支持本地路径、`file:///D:/x`、`file://localhost/D:/x`；`file://datasets/x` 按契约样例视为 `base_dir` 下的相对路径。UNC 共享路径与其他协议暂不支持。
- **分页**：按 `asset_version_id` 排序，`page_size` 1～50，cursor 不透明。

## 激活状态

阶段依次为 `dormant`（沉睡）→ `identified`（已标识）→ `in_graph`（已入图）→ `referenced`（被规则/计划引用）。

- 接入时带实体即记 `identified`；`in_graph` 由 B22 登记，`referenced` 由 B12/B25/计划侧登记且必须带引用方 `ref`。
- 事件只追加，重复登记幂等，回填更早的可见时间会生效；当前阶段取 `available_at <= as_of` 的事件中最高的一级，回放时不会看到未来的引用。
- `activation_summary` 返回各阶段数量和 `referenced_rate`，并固定附带说明：激活率只是辅助指标，被引用不等于产生业务收益。

## 与 B03、B04 的边界

- **存储访问与 B04 相同**：B03 的 `Repository.insert_record/read` 只支持带 `version_manifest_id` 的点查，没有列表和按来源查询，而资料目录是清单的输入、不随清单分区。因此 B06 沿用已合并的 B04（`plantkg/core/versions.py`）的做法：装配层注入 B03 的 `SQLiteWriter`，写用 `writer.submit(fn(con))`，读用 `writer.read_only()`，只读写本模块迁移建立的 `asset_*` 表，不导入 `plantkg.storage`，也不碰 B03 或 B04 的表。该约定已由 B03 维护人在 PR #9 确认，记录在 B03 文档的“模块自有表”一节。
- 版本清单固定哪些 `asset_version_id` 由 B04 负责；本模块只保证 ID 不可变、内容可校验。
- **装配**：`storage.enabled=true` 时 `bootstrap.py` 在 B04 之后构建目录并调用 `register`。原文快照放在 SQLite 文件旁的 `asset_blobs/`；相对资料路径按仓库根解析，与存储路径规则一致。存储关闭时仍由 mock 的 Fake 提供 `list_assets` 和 `GET /assets`。
- **路由**：资料 ID 含 `/`，`api.py` 给 `asset_version_id` 注册了专用转换器，只匹配恰好四段的 `asset://{行业}/{哈希8位}/{类型}/{定位}`。B06 生成的定位不含 `/`，所以匹配没有歧义，也与路由顺序无关：`…/policy/fragments` 是文件名为 `fragments` 的资料详情，`…/policy/tou/fragments` 才是 B21 的片段子路由。原样请求或整段 URL 编码都可以；ID 不存在或格式不符返回 404。契约正则允许定位含 `/`，这种 ID 由其他模块生成时需要另行约定。

## 验证

```powershell
python -m pytest tests/assets -q
python -m pytest tests -q
python scripts/check_boundaries.py
```

测试覆盖：同内容重复接入同一版本、不同路径同字节去重、改版生成新版本且实体不变、改回旧内容与换路径后的血缘、`file://` 写法、同名不同设备不合并、同型号说明书关联两台设备、多来源同设备共用实体、原文被改后仍取回快照、可见时间与厂区隔离、写权限与幂等冲突、激活阶段与回放可见性、工具替换 Fake 与分页、继承关联保留各自生效时间、启用存储后经真实 HTTP 读取带 `/` 的资料 ID（含名为 `fragments` 的资料与真实片段子路由）。

`tests/storage/test_sqlite.py` 中一个 B03 测试原先在复制的迁移目录里新建 `assets/` 作为假下游模块，与本分支的真实 `assets/` 目录撞名；已把该假模块改名为 `downstream_demo`，测试内容不变。
