# B04 版本清单与运行管理

实现 `VersionStore`、`RunStore`、`EvidenceSink`、`RunSessionStore`、`JobRunner`、`PendingActionStore` 六个端口（`plantkg/core/versions.py`、`runs.py`、`evidence.py`、`sessions.py`、`jobs.py`、`actions.py`），表结构在 `plantkg/storage/migrations/versions/`。全部共用 B03 的 SQLite 单写者。每个写方法自带一个 `BEGIN IMMEDIATE` 事务（COMMIT 失败也回滚），不能放进外层 `writer.transaction()`，否则直接报错。

## 启用

B04 随 `storage.enabled=true` 装配；关闭时 mock/dev 仍用骨架的 Fake，prod 因缺 `RunSessionStore` 拒绝启动。

```bash
python -m plantkg --profile dev --set storage.enabled=true --set storage.sqlite=runtime/b04/catalog.db --set storage.graph=runtime/b04/graph --set storage.series=runtime/b04/series
```

配置 `actions`：`reviewers` 为可确认的复核员 ID 列表（团队两人，写在 plant.yaml 或场景里），`ttl_s` 为待确认动作有效期（默认 24 小时），`token_file` 为本机运维令牌文件。

## 版本清单

发布顺序固定：写不可变产物 → `prepare(components, reason)` → `check_integrity(id)` → `activate(expected_active, id, actor, reason)`。

- `prepare` 按组件内容幂等：同内容且未丢弃的清单直接返回。
- `check_integrity` 按引用前缀调用校验函数。`graph:v1:`、`series:v1:` 由装配层登记，要求 B03 目录登记完整且 `content_hash` 等于目录 checksum；其他前缀由产物所有者用 `register_verifier(prefix, fn)` 登记。缺失或损坏 → `discarded`；暂时无法校验（未登记校验函数、产物未登记）→ 保持 `prepared` 并返回 `PRECONDITION`，可重试。
- `activate` 激活前再校验一次，然后在一个 `BEGIN IMMEDIATE` 事务里比较预期活动版本、退役旧版本、激活候选、移动活动指针并写审计。预期不符返回 `STATE_CONFLICT`；同一请求重试（候选已激活且其 `previous_active_id` 等于预期）原样返回；调用方因此分不清“刚激活”与“早已激活”，需要区分时看 `activated_at`。
- 回滚 = `activate(当前, 某个 retired 清单)`；`discarded` 不可回滚。产物与历史不删除。
- `history()` 给出每次状态变化的操作人、时间、原因与前一活动版本。

B18/B27/B30 只能调用这些原语，不直接改表。

## 运行

`create_run(context, kind, clock, forked_from=None, *, seed=0, inputs=None)` 固定清单（`context.version_manifest_id`，缺省取当前活动清单；只接受 verified/active/retired）、时钟区间、`as_of`、种子与输入切片。`run_inputs(run_id)` 返回复现所需的全部固定输入。`append_event(run_id, event_type, actor, payload, to_status=None)` 追加事件，状态只按 `created→running→finished|failed`、`created→failed` 迁移；每条事件记录 actor、时间、run_id、清单与前后状态。

## 固定清单解析

`build_services` 未注入 `storage_resolver` 时，B03 的图与时序读取使用 `ManifestReferences`：只看 `context.version_manifest_id` 指向的清单（verified/active/retired），图按 B03 登记名匹配清单里的 `graph:v1:` 引用，旧清单的时序返回清单里的 `series:v1:` 引用；新清单的 `data_sources` 可以指向按 quantity 映射到单个已登记分区的不可变 `series-map:v1:` 产物。没有固定清单、映射项或清单不可用时返回空，不回退到最新版本。每次时序查询仍只读取一个分区。

## 证据

`append(event)` 分配全局单调 `seq`（忽略调用方的 seq）；同一 `event_id` 同内容返回原事件，不同内容 `IDEMPOTENCY_MISMATCH`。`list(run_id, cursor, visibility, *, agent_run_id, action_id, trace_id, page_size)` 按 seq 分页，run/agent/action/trace 与 `occurred_at <= visibility.as_of`、`available_at <= as_of` 都在 SQL 里过滤；`/evidence` 带 `run_id` 时取该运行的时钟（回放游标，场景运行取已写入事件的最新时刻），不带时用只读上下文的 as_of。`subscribe(trace_id)` 先给已有事件再阻塞等待新事件，sink 关闭时结束。

## 运行会话

`MemoryRunSessionStore` 只放在服务进程内存，不落库；重启后旧 ctx 一律 `UNKNOWN_CTX`。有效期 1 秒～24 小时；过期后保留 1 小时以返回 `CTX_EXPIRED`。B19 在 Agent 运行结束时调用 `expire(ctx)` 或 `expire_agent_run(agent_run_id)`。

## 后台任务

执行函数按 `JobKind` 登记：`runner.register("run_day", fn)`，`fn(payload, context, job_id) -> result_ref`，失败抛 `PlantkgError`。任务在服务进程的后台 daemon 线程执行（默认 2 个），与调用方无关：MCP 调用超时或中止后照常完成。同一幂等键只建一个任务，内容不同 `IDEMPOTENCY_MISMATCH`；未登记执行函数时提交返回 `DEPENDENCY_UNAVAILABLE` 且不建任务。`get(job_id, wait_seconds)` 最多等 40 秒。默认预算：`run_day` 取 `planning.solver_budget.time_limit_s`，其余取 `planning.background_budget`；payload 的 `time_limit_s` 优先；执行函数从 `payload["time_limit_s"]` 读到实际时限并自行遵守。服务重启时遗留的 pending/running 任务记为 `failed`（可重试），需换幂等键重新提交。payload 提交时深拷贝；执行函数抛出任何异常（含 `SystemExit`）都记为 `failed`，不影响工作线程。

## 待确认动作

Agent 侧工具调用 `propose(kind, payload, context)`；`payload` 里的 `target_ref`、`target_revision` 写入同名字段。幂等键取 `context.idempotency_key`，缺省按内容、agent_run_id 与 trace_id 派生。

复核员确认时，B04 依次检查：复核员角色 → 幂等键（同键同内容返回原结果）→ 过期（`ACTION_EXPIRED`）→ 状态 → `expected_revision`（`REVISION_CONFLICT`）→ 执行函数是否登记（`DEPENDENCY_UNAVAILABLE`）。随后调用 `Registry.action_handlers[kind].apply(action, actor)`；返回 `ok=False` 或抛错时动作保持 `proposed`。成功后记录确认人、时间、原因与执行结果（`result(action_id)`）并写证据事件；证据写入失败只记日志，不影响已生效的决定。确认、执行与过期在进程内串行：同一动作只执行一次，执行期间不会被并发读取标为过期。执行成功但记录写入失败（如磁盘故障）时动作仍为 `proposed`，重试会再次调用执行函数，所以执行函数应按 `action_id` 幂等。B15/B18/B24 只登记执行函数（`registry.register_action_handler`），不另建确认端点。

## 确认端点与命令行

`POST /api/v1/actions/{id}/confirm|reject` 由 B04 提供。工作台登录（B35/B19）接入前只接受本机运维令牌：服务启动时生成随机令牌写入 `actions.token_file`，停止时删除；请求头 `X-Plantkg-Operator-Token` 与 `X-Plantkg-Actor`（须在 `actions.reviewers` 中）。命令行用同一配置读取令牌并调用端点：

```bash
python -m plantkg --profile dev --set storage.enabled=true actions list
python -m plantkg --profile dev --set storage.enabled=true actions confirm act-xxx --actor u-reviewer --reason "回测通过"
python -m plantkg --profile dev --set storage.enabled=true actions reject act-xxx --actor u-reviewer --reason "费用升高"
```

`--expected-revision` 缺省取当前修订；默认幂等键由动作、决定、修订与复核员派生，重试同一命令得到同一结果。强制结束服务时令牌文件可能残留，下次启动会覆盖。

## needs_repair：执行已发生但决定没落库

确认时执行函数已成功、但终态决定写库失败（如磁盘故障）时，动作记录仍是 `proposed`，执行意图停在 `needs_repair`；服务重启扫描也会把停在 `confirming` 的意图转成该状态。没有自动修复：

1. 用 `python -m plantkg --profile dev --set storage.enabled=true actions list --status needs_repair` 或 `GET /api/v1/actions?status=needs_repair` 列出待处置动作；
2. 不要对它们再执行 confirm/reject：执行意图还在，两者都会被 `STATE_CONFLICT` 挡住；
3. 按 `action_id` 到执行函数对应的业务对象（发布、工单、复核项等）核实副作用是否已经生效，并人工补记结果；
4. 需要重做时另提一个新的待确认动作（用新的幂等键），原动作与执行意图保留作审计。处置由运维人工完成，不提供自动修复。

## 共享文件改动

- `plantkg/bootstrap.py`：存储启用时装配 B04（`VersionRun`），并把 `ManifestReferences` 作为 B03 默认解析器；`Services.close` 先停任务线程与证据订阅再关存储。
- `plantkg/api.py`：提供者改在线程池执行（`GET /jobs?wait=` 不再阻塞事件循环）；登记了提供者的写入路由把请求体与请求头交给提供者自行认证，其余写入仍拒绝。
- `plantkg/__main__.py`：`actions` 子命令；运行服务时写入并清理运维令牌。
- `configs/defaults.yaml`：新增 `actions` 段。

## 不在本次范围

`run_day` 等任务的执行函数（B17）、计划确认/发布/回滚/复核结论的执行函数（B15/B18/B24）、工作台登录（B35/B19）、网关调用会话登记（B19）、其他非存储产物（如规则）的校验函数（各产物所有者；冻结预测参数已有校验器）。
