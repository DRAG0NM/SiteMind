# B03 三类存储与迁移底座

## 基线与边界

开发起点：`main` 与 `feature/storage-repositories` 均为 `5c465c09f5a5fa5e90faa407b165f02a00e43444`，包含 B02 合并提交 `ab2465f1b17f62645e13539d7963e77919b21d42`；规划参考 `内部设计文档` 的 `c6cd2303862fcd29abee9a95c26f9b3eb6f2216d`。方案 A 包括依赖、正式装配、健康状态及生命周期。`plantkg/storage` 是基础设施，`plantkg/packs/storage` 仍是储能领域包。本分支不提供 B04 的发布、激活和运行服务，也不实现 B06 的资产业务。

实现期间 `origin/main` 前进到 `9f4e9de3a196bb54bd839fe36fb1ef8c27f96f2a`（B08 的 PR #3 已合并），新增 `plantkg/core/ledger.py`、其测试、夹具和说明，与 B03 修改文件不重叠。经负责人明确授权，本地 B03 已快进到该提交并保留全部 B03 修改，实际开发基线为 `9f4e9de`。`内部设计文档` 前进至 `b9d8e89`，仅更新 README 与任务图。

PR #6 首轮审查后，同步 `main` 的 `0d1c155`（已合并 B05 和 `tzdata`），并针对审查提出的迁移顺序、换行、线程、异步事务、图分页及时序参数问题修复；不将 B05 功能另行带入 B03 差异。

第二轮复审后同步 `main` 的 `6dcc6a1c737b4441b05784de8673e6516a030fdf`（已合并 B09）。`pyproject.toml` 冲突保留 main 的 Pyomo/HiGHS 基础依赖、B03 的 pyoxigraph 与 SQL 包资源，并保留原有 `solver` extra 以兼容安装命令。

## 启动、路径和所有权

安装：`python -m pip install -e ".[dev,storage]"`。默认 `storage.enabled=false`；禁用时不导入原生存储依赖。显式启用时，缺依赖、数据库或迁移错误导致启动失败，不降级为 Fake。mock 使用独立临时目录，关闭后删除；dev 使用 `storage.sqlite`、`storage.graph`、`storage.series`。相对路径相对安装的 `plantkg` 包所在目录的父目录解析，不依赖进程工作目录。安装到受保护的目录时应为 dev 指定可写的绝对路径。

例如在仓库目录运行：

```powershell
python -m plantkg --profile mock --dry-run --set storage.enabled=true
python -m plantkg --profile dev --set storage.enabled=true --set storage.sqlite=runtime/b03-dev/catalog.db --set storage.graph=runtime/b03-dev/graph --set storage.series=runtime/b03-dev/series
```

访问 `http://127.0.0.1:8700/api/v1/health`；停止后重复同一命令可重开同一目录。测试及评审应使用临时或独立目录，不能覆盖已有库。`prod` 不装 Fake；B04 的 RunSessionStore 尚未接入，因此仍按原有规则拒绝启动。`/health` 的 `dependencies` 新增 `storage` 键：关闭时为 `disabled`，启用时为 `unchecked`，初始化成功不等于所有历史引用已经检查；`disabled` 不使整体降级，`unchecked` 与其他未验证或占位依赖一样使整体状态为 degraded。依赖此响应的消费者应允许新增依赖键。

`build_services` 在领域包装配之前构建存储并注册三个 B01 端口；它们共用一个 SQLiteWriter。API lifespan 与 MCP lifespan 组合，结束时关闭图和写者；`--dry-run` 和装配失败也关闭资源。直接调用 `build_services` 的代码须在 `finally` 调用 `services.close()`。写线程为 daemon，避免遗漏关闭导致解释器无限等待；退出时仍应显式关闭，daemon 退出不保证完成尚未完成的任务。进程内同路径重复写者被拒；跨进程使用数据库旁的 `.writer.lock` 文件互斥，仅支持单服务写入。SQLite 使用 WAL、外键、有限 busy timeout；读连接 `mode=ro` 且 `query_only=ON`，结果取出后关闭。关闭等待已接受工作最多 10 秒，超时抛出错误并保留所有权，不能静默丢失任务；待工作完成后可再次关闭。

## 仓储和下游扩展

`Registry.resolve(Repository)` 返回真实仓储。B01 的 `transaction_short()`、`read(context, table, key)` 和 `append_once(key, record)` 签名保持不变。内建映射 `storage_idempotency` 按构造时绑定的 `plant_id` 与 `key` 存放记录：`append_once("k", {"value": 1})` 后，可用同厂区 Context 的 `read(context, "storage_idempotency", {"key": "k"})` 读取。该基础设施映射本身没有业务可见时间或业务版本，不能用它代替下游业务模型。

幂等内容以 UTF-8 JSON、排序键、无 NaN 规范化；Pydantic、枚举、日期和日期时间按类型转换，带时区的日期时间先转 UTC；字符串仍是字符串，不猜测其类型。同键同内容返回 `False`，异内容抛 `IDEMPOTENCY_MISMATCH`，首次返回 `True`。一次短事务由写线程持有真实 SQLite 事务，调用方的读己写走同一连接；嵌套事务拒绝。任何内部操作失败会使事务回滚，即使调用方捕获了该异常。计算、文件与图准备和迁移不得在短事务中执行。

事务体禁止 `await`。在已有运行事件循环的线程上进入 `transaction_short()` 会立即报错；异步代码应将整个同步事务体提交到工作线程，例如 `await asyncio.to_thread(sync_transaction)`，不要让不同协程共享线程局部事务状态。

下游模块先在自己的迁移目录建立类型化表，再由可信装配代码调用 `register_table(TableMapping(module, table, Model, key_fields))`。模型字段须与表列一致，并包含 `plant_id`、`version_manifest_id`、UTC `available_at`；键字段必须在每个厂区和清单内唯一。使用 `insert_record(context, table, pydantic_record)` 插入，使用 B01 `read` 按键读取；前者要求可信写 Context，后者检查厂区、固定清单和 `available_at <= as_of`。作用域两列按普通文本存储，其余模型字段以规范 JSON 文本存储；SQL 表应据此声明 TEXT 列和唯一约束。映射只接受已由对应模块迁移登记的表和合法标识符，下游无需获取连接，也不能通过 Repository 提交任意 SQL。

`insert_record` 遇到明确的主键、唯一键或 rowid 冲突映射为 `STATE_CONFLICT`；其他约束或数据库故障不伪装成键冲突。当前受控接口只覆盖插入和点查，不提供 B04 激活 CAS、运行状态更新等操作；B04 须在其设计中补充受控仓储操作，本轮不实现其业务。普通 Repository 使用路径不把 `writer.submit` 当作下游公共接口；需要列表、条件查询或条件更新的模块，按下一节“模块自有表”的受支持例外使用。

### 模块自有表（B04、B06 的受支持例外）

B03 维护人已于 2026-10-31 在 PR #9 确认本节约定，作为 B03 对 B04 和 B06 的受支持集成方式；确认不改变现有事务语义。

需要列表、按条件查询或条件更新的模块（B04 版本与运行、B06 资产目录），由装配层注入 `SQLiteWriter`：写用 `writer.submit(fn)`（`fn(con)` 在单写线程执行，多语句自行用 `BEGIN IMMEDIATE` 或 `SAVEPOINT` 保证原子），读用 `with writer.read_only() as con`。约束如下：

- 只读写本模块迁移目录建立、由 `schema_table_owners` 登记的表；
- 不执行 DDL、PRAGMA、ATTACH；
- 平面代码不导入 `plantkg.storage`；
- 继续遵守同步短事务、事务体内禁止 `await`、禁止嵌套事务等现有限制。

重构 `SQLiteWriter` 时须保持 `submit`、`read_only`、`transaction` 的签名和语义，并跑 `tests/versions` 与 `tests/assets` 回归。

## 迁移、备份和格式升级

`MigrationRunner(writer).apply()` 发现 `plantkg/storage/migrations/<模块>/B<编号>_<序号>_<描述>.sql`，按 B 编号、序号、模块和文件名确定顺序。同模块同 B 编号与序号冲突拒绝。`schema_migrations(module,id,checksum,applied_at)` 保存完整 SHA-256；`schema_table_owners` 与状态摘要用于核对表归属、schema 和迁移历史。禁止跨模块外键和写入另一模块已登记的表；未知旧表不能通过 ALTER 冒充已归属表。每个迁移的 SQL、归属和登记处于同一事务，前一迁移成功、后一迁移失败时保留前者；失败异常附带本轮已应用项和备份位置。禁止迁移自行控制事务、ATTACH 或 PRAGMA。以 SQLite `complete_statement` 识别语句和触发器，不使用会隐式提交的 `executescript` 或简单按分号分割。

每个模块独立检查已应用迁移是否为该模块顺序的前缀；全局排序仅决定本批待执行项的顺序。因此先应用 `assets/B06_001` 后加入 `runs/B04_001` 可以升级，但在同一模块中插入已应用迁移之前的文件会拒绝。执行和计算 checksum 前统一将 CRLF 规范为 LF，并通过 `.gitattributes` 固定 SQL 检出的换行。为兼容此前版本在 Windows 用原始 CRLF 字节登记的 checksum，runner 也验证对应的旧 CRLF 散列，不改写已有登记；其他内容变化仍拒绝。

已有库实际升级前通过 SQLite 在线备份 API 先写同目录 `.partial`，验证完整性并关闭连接后再确认文件非空；用同目录硬链接原子发布为 `<数据库>.<UTC时间>.backup`，该操作不会覆盖同名备份。文件系统必须支持同目录硬链接；不支持时备份失败并中止升级。失败时清理临时文件，不将空文件发布为正式备份；进程被强制终止时可能留下不可当作备份的 `.partial`。返回值含升级前、后条目、应用条目和备份路径；日志记录失败迁移。恢复验证应先确认备份文件存在且大小非零，再以只读方式检查数据库完整性，并复制到独立路径验证，不默认覆盖现有库。例如 `python -c "from pathlib import Path; import sqlite3; p=Path(r'备份路径').resolve(); assert p.is_file() and p.stat().st_size > 0; c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); print(c.execute('pragma integrity_check').fetchone()); c.close()"`。重复执行无待迁移项时不再备份。已执行 SQL 修改、未知未来迁移、缺失或损坏历史均拒绝；只能追加向前迁移。显式可丢弃开发库可在基线前由使用者重建；开始测进化时由对应阶段打基线 tag，本轮不建立项目冻结 tag。

`storage_formats` 记录 graph/series 格式版本 1。SQL 迁移更新目录结构；图与 Parquet 格式变化必须登记新版本并生成新产物，旧引用不原地改写。当前代码拒绝未知或缺失格式记录；无自动向下迁移或垃圾回收。

## 不可变图、分片与固定引用

图引用为 `graph:v1:<sha256>`，由厂区、图名和 Turtle **源文本** SHA-256 得出；它不是 RDF 语义规范化散列。先在隔离 Oxigraph 中解析，持久库的命名图写入后计算三元组内容散列，最后才登记目录。同名异内容拒绝，空图也登记为命名图以区分丢失。查询仅支持 `triples` 模板和 `page_size`，结果排序稳定；cursor 绑定厂区、清单、as_of、图、查询与参数。注册失败留下的图不可见，完整性检查报告 `not_prepared`。

图分页按不可变引用与内容 checksum 缓存已校验、已排序的快照；不同 as_of、页大小和查询范围可复用同一份数据，最多缓存四个图，淘汰后重新读取并校验。每次请求仍重新执行厂区、清单解析、可见性、参数和 cursor 范围校验。带外改动不会自动改变缓存；显式完整性检查会重新扫描，发现损坏时清除该图的缓存。关闭图存储时释放缓存。

时序引用为 `series:v1:<sha256>`，标识规范化 SourceBundle；完整 Parquet 文件另存 SHA-256。schema 元数据包含格式版本和 bundle 元数据，记录包含原始定位、时间、可见时间、单位、性质、质量和转换链。临时文件经 schema、元数据、行数和内容验证后以不覆盖方式发布，再登记目录。查询只读取解析器返回且登记完整的具体文件，并由 DuckDB 按 quantity、`available_at <= as_of` 及窗口过滤：点记录 `start <= event_time < end`；区间记录 `interval.start < end and interval.end > start`。缺测仍为 `None`。当前 B01 返回一个 SourceBundle，因此多于一个不同分片引用时明确拒绝，避免虚构 bundle 身份；下游应解析到可表达的单个引用。

当前分片策略要求下游用完整的新 SourceBundle 生成新引用，不能在一次查询中把追加分片拼成虚假的 bundle 身份。写入前会重建并比较 SourceBundle；`TransformStep.params` 内的 datetime、tuple 等无法保持原类型的值会在发布前明确拒绝，NaN 和无穷值映射为 `INVALID_REQUEST`；调用方应先按契约转换为明确的 JSON 类型。

B04 可在 `build_services(settings, storage_resolver=resolver)` 注入 `plantkg.storage.references.ReferenceResolver`，分别实现 `graph(context, graph_name)` 和 `series(context, quantity)`。该可信解析器必须根据 `context.plant_id`、`version_manifest_id` 和 `as_of` 返回固定清单中允许的引用；缺映射返回空，不得回退最新版本。B03 另检查厂区、清单存在、目录归属和完整性。未注入时全部拒绝读取；此接缝不创建发布服务。B04 在 SQLite 事务激活清单前可调用 `check_references(writer, graph, series, plant_id, refs)`，分别得到 `complete`、`missing`、`corrupt`、`not_prepared`、`unverifiable`；只有所有引用 complete 才应继续。失败不删除旧引用或残留文件。

## 共享文件改动

`pyproject.toml` 补齐 `pyoxigraph==0.5.13` 并打包 SQL；`configs/defaults.yaml` 增加开关；`bootstrap.py` 注册存储和接收固定引用解析器；`api.py` 组合 MCP 生命周期并报告健康；`__main__.py` 在 dry-run 和退出时关闭服务。各自只用于 B03 依赖、装配和资源生命周期，合并并行分支时需检查这些共享文件。

## 验证与后续责任

Windows、Python 3.11.5，真实 `duckdb==2.0.0`、`pyarrow==26.0.0`、`pyoxigraph==0.5.13`。存储测试使用临时磁盘，覆盖迁移失败重试、WAL 备份、历史篡改、跨模块外键、短事务、进程写锁、只读、队列恢复、图导入/登记失败、空图丢失、Parquet 半写/登记失败、可见时间与窗口、旧组合重启、正式装配、MCP 和 HTTP 生命周期。当前环境的沙箱内 RocksDB 初始化失败；真实引擎测试在沙箱外执行。

同步 `9f4e9de` 后，`python -m pytest tests` 为 456 passed，包含 B08 测试；契约生成、边界和 `pyflakes` 检查通过。mock dry-run 在默认禁用和显式启用存储两种配置下均成功；dev 命令对同一持久化目录重复自检，真实 MCP/HTTP 服务启动、检查、停止和重开通过。同步后重新构建的 wheel 在非仓库目录可加载 B08 与存储模块，并完成初始化和重复迁移。

针对 PR #6 复审，在同步 `0d1c155` 后 `python -m pytest tests -q` 为 498 passed；`python contracts/build.py --check`、`python scripts/check_boundaries.py` 和 `python -m pyflakes plantkg/storage tests/storage` 通过。新增回归覆盖跨模块后合并、模块内缺口、CRLF/LF 与旧 checksum、daemon 退出、事件循环拒绝、分页仅扫描一次、有损参数拒绝及 B05 PVGIS bundle 往返。Windows 本机临时磁盘上的真实 Oxigraph 测试以 10 万三元组、每页 500 条读取：首次校验和首页约 0.502 秒，后续 199 页合计约 0.112 秒；这些时间仅作本机测量，不构成性能保证。

同步 `6dcc6a1` 并处理第二轮四项建议后，在新建 Windows Python 3.11 虚拟环境安装 `.[dev,storage]`，全仓 `python -m pytest tests -q` 为 563 passed，`python -m pytest tests/storage -q` 为 46 passed；契约生成检查、导入边界和存储目录 pyflakes 均通过。默认 mock 和启用存储的 dry-run 均成功；测试覆盖 `/health` 的 disabled/unchecked、真实 MCP/HTTP 启停重开、失败备份清理与同名保护、唯一键和其他约束区分、非有限数值、跨请求图缓存复用，以及原有迁移兼容回归。全局 Python 安装依赖时因 Windows 的 `pyomo.exe` 文件操作失败，已改用独立 `.venv` 完成安装和验证。

执行命令：`python contracts/build.py --check`、`python scripts/check_boundaries.py`、`python -m pytest tests/storage`、`python -m pytest tests`、`python -m plantkg --profile mock --dry-run`。wheel 使用 `python -m pip wheel --no-deps --wheel-dir <临时目录> .` 构建；安装到独立目录后，从非仓库工作目录加载 `MigrationRunner` 并初始化临时库，验证 SQL 已随包提供。构建时系统旧 setuptools 缺少 `bdist_wheel`，改用项目声明的隔离构建依赖成功。

格式转换器、自动清理孤儿、正式清单激活、B04/B06 业务服务、Nexent 和真实业务数据均不属于本次实现。固定清单解析器是可信边界；下游必须正确计算 as_of，B03 不自行生成回放时钟。
