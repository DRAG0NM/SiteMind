# 运行时与工程骨架（B02）

## 快速开始（Mock，离线）

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Windows；Linux/macOS 用 .venv/bin/python
python -m plantkg --profile mock --dry-run   # 只装配并打印健康状态与工具清单
python -m plantkg --profile mock             # 监听 127.0.0.1:8700
python -m pytest tests                        # 契约 + 骨架测试
python scripts/check_boundaries.py            # 模块边界
python contracts/build.py --check             # 契约生成物最新
```

Mock profile 不需要 Nexent、真实模型或任何密钥。服务起来后：

- REST：`GET http://127.0.0.1:8700/api/v1/health`、`GET /api/v1/plans/plan-20260701`
- MCP（streamable HTTP）：`http://127.0.0.1:8700/mcp`

## 让容器里的 Nexent 连上 plantkg

在 Nexent 里登记 MCP 服务时，地址写 `http://host.docker.internal:8700/mcp`。监听地址按 Docker 环境选：

| 环境 | 监听 | 启动命令 | 实测 |
| --- | --- | --- | --- |
| Windows / macOS 的 Docker Desktop | 默认 `127.0.0.1` 即可（Docker Desktop 把 `host.docker.internal` 转发到宿主机回环） | `python -m plantkg --profile dev` | 2026-10-30 本机 Docker 29.9.2：从 `nexent-runtime` 容器 MCP initialize 返回 200，`127.0.0.1` 与 `0.0.0.0` 两种监听都通 |
| Linux 原生 Docker Engine | 必须 `0.0.0.0`（或 docker0 网桥地址）；Nexent 的 compose 需有 `extra_hosts: ["host.docker.internal:host-gateway"]` | `python -m plantkg --profile dev --set server.host=0.0.0.0` | 未实测 |

**Nexent 端到端实测（2026-10-30，本机 Nexent v2.8.2 + Docker Desktop，plantkg 默认监听 `127.0.0.1:8700`，mock profile）**：经管理接口 `POST /mcp/add` 登记 `http://host.docker.internal:8700/mcp` 返回成功；`GET /mcp/tools` 列出 `plantkg_list_assets`、`plantkg_get_plan`、`plantkg_ledger`、`plantkg_run_day`、`plantkg_get_job`；`GET /tool/scan_tool` 返回 200；经工具测试入口 `POST /tool/validate` 调用：`plantkg_get_plan` 返回 `PLANTKG_NOTICE_NO_CTX:` 开头的降级结果，`plantkg_run_day` 返回 `PLANTKG_E_NO_CTX:`（无网关会话的写入被拒，符合预期）。验证后用 `DELETE /mcp/{mcp_id}` 删除登记。经 Agent 与北向 API 的调用（带 ctx 注入）由 B19 验收。

监听 `0.0.0.0` 时服务对局域网可见，只在比赛开发机上这样用。MCP 端不做 Host 来源限制（与探针实测配置一致），容器带来的 `Host: host.docker.internal` 不会被拒。

## 进程与装配

一个 Python 进程（uvicorn + Starlette）同时提供 REST（`/api/v1`）与 MCP（`/mcp`），保持 SQLite 单写者（ADR-0006）。

| 文件 | 作用 | 规则 |
| --- | --- | --- |
| `plantkg/__main__.py` | 解析参数、读配置、检查密钥、启动 | `--set` 只能覆盖非敏感键 |
| `plantkg/bootstrap.py` | 唯一装配层：注册表 → 领域包 → 安全壳 → 应用 | 模块经注册表登记，不改本文件 |
| `plantkg/api.py` | 按 `API_ROUTES` 生成路由；GET 交给注册的提供者 | 未接入返回 503 并写明提供分支；写入在工作台登录接入前一律拒绝 |
| `plantkg/mcp_server.py` | 按 `ToolSpec` 生成 FastMCP 工具，名字加 `plantkg_` | 每个工具都有可选 `ctx`（默认空），经 `ToolGuard` 调用 |
| `plantkg/core/registry.py` | 端口、工具、REST、确认动作、领域包登记 | 重复、未在契约声明、schema 版本不符都在启动时报错 |
| `plantkg/core/security.py` | ctx 解析、失败关闭、出站检查、日志脱敏 | 见下 |
| `plantkg/core/config.py` | 分层配置、密钥读取、日志 | 见下 |
| `plantkg/testing/` | Fake 端口、Mock 数据、Fake 事件流 | Fake 事件在 `fake_events/`，每行带 `_fake: true`；平台实测样例在 `nexent_assets/probes/` |

后续分支接入方式：在 `plantkg/packs/<名>/pack.yaml` 填 `entrypoint: 模块:函数` 与 `provides`，入口函数签名 `(registry, settings) -> None`，在里面 `registry.register(端口, 实现)`、`register_tool`、`register_rest`、`register_action_handler`。四个包当前是 `status: placeholder`。

**真实实现替换 Fake 的规则**（按 profile）：

| profile | 装配 | 缺失时 |
| --- | --- | --- |
| mock | 先加载包里的真实实现，再用 Fake 补齐所有空缺槽位 | 不会缺 |
| dev | 同 mock；模块接入后它的槽位自动改用真实实现，其余仍是 Fake | 不会缺 |
| prod | 不装 Fake | 缺必需端口即启动失败（退出码 2，提示由哪个分支提供）。**B04 交付 RunSessionStore 之前 prod 不可启动** |

- 真实实现总是替换 Fake，与登记先后无关；两个真实实现登记同一槽位仍报重复错误。
- 正常模块不用写 `fallback=True`；只有 `plantkg/testing` 里的 Fake 这样登记。
- `python -m plantkg --dry-run` 的 `fakes` 列出仍由 Fake 顶替的槽位，健康检查里对应 `fake.<名>: fake`。
- 依赖端口的工具与 REST 提供者，在**调用时**才从注册表取端口（`registry.resolve(端口)`），不在登记时捕获实例。Fake 的 `run_day`、`get_job`、`GET /jobs/{id}` 就是这样，所以提交与查询始终连同一个 JobRunner，不论它是 Fake 还是真实实现、先登记还是后登记。后续模块的处理函数也按此写。
- 未实现的包必须保持 `status: placeholder`；非占位却没有 `entrypoint` 的包在启动时报错。

**B04 接入时的验收要求**：真实 `JobRunner`、`RunSessionStore` 登记后，`--dry-run` 的 `fakes` 不再含这两项；在 B17 替换 `run_day` 之前，Fake `run_day` 提交的任务能被 `get_job` 与 `GET /jobs/{id}` 在真实队列中查到（`tests/skeleton/test_assembly.py::test_job_submit_and_query_share_one_queue` 覆盖）。

## 健康检查

`GET /api/v1/health` 只报告真实知道的状态，`status` 在有占位、Fake 或不可用依赖时为 `degraded`：

| 值 | 含义 |
| --- | --- |
| `ok` | 真实实现已装配（外部依赖：已做连通检查且通过） |
| `disabled` | 配置关闭 |
| `unchecked` | 已启用但未做连通检查（当前 Nexent、Jev 都是这样，连通检查由 B19、B23 补） |
| `placeholder` | 领域包仍是骨架占位 |
| `fake` | 由测试替身顶替 |
| `unavailable` | 检查失败或配置启用的包不存在 |

Mock 下的典型结果：`status: degraded`，四个包 `placeholder`，会话表、任务队列和 5 个工具为 `fake`。

## 配置

优先级：`configs/defaults.yaml` → `configs/plant.yaml` → `configs/scenarios/<scenario>.yaml` → `--set` 覆盖。

- 密钥只从环境变量读（配置写变量名 `*_env`），见 `.env.example`；缺失时报变量名，不打印值。Mock 不检查密钥；`nexent.enabled` 或 `judge.backend: jev` 时启动前检查。
- `plant_id`、`industry_pack`、`security.*`、密钥变量名不能从命令行覆盖。
- 未建模的顶层配置节允许存在，由对应模块用 `settings.section(名)` 读取并自行校验。
- `world.default_as_of`：无 ctx 的只读降级查询用的查看时点，取回放演示日 D-1 18:00，不用现实当前时间（防未来泄漏）。

## 安全壳（ADR-0002）

| 情况 | 写入/提议/后台任务工具 | 只读工具 |
| --- | --- | --- |
| 无 ctx | `PLANTKG_E_NO_CTX` | 降级结果，文本以 `PLANTKG_NOTICE_NO_CTX:` 开头 |
| ctx 查不到或伪造 | `PLANTKG_E_UNKNOWN_CTX` | 同左 |
| ctx 已过期 | `PLANTKG_E_CTX_EXPIRED` | 同左 |

- `security.default_ctx.enabled` 只在 mock/dev 生效，用于在 Nexent 调试页测试写工具；prod 开启时配置加载即失败。
- 工具结果出站前检查是否回显了 ctx，回显即替换为 `PLANTKG_E_INTERNAL`；处理函数抛出的异常不把细节带给模型。
- 日志：每行带 `run=`、`trace=`；`RedactingFilter` 对消息与异常堆栈中的 32 位十六进制 ctx、`sk-` 密钥、Bearer 令牌、JWT 脱敏（64 位内容散列不受影响）。ValidationError 只经 `safe_error_details` 记录。

## 依赖

`pyproject.toml` 锁定运行依赖：pydantic 2.14.1、PyYAML 6.0.3、starlette 1.10.0、uvicorn 0.54.0、fastmcp 4.1.4（版本依据 `nexent_assets/probes/c_env/requirements-lock.txt`）。求解与存储依赖作为 `solver`、`storage` 可选组，由 B03/B09 启用。Python 3.11（3.12 已验证契约检查）。

## 尚未包含（按任务卡归属）

后台任务 40 秒截止与转 pending 的真实执行（B04 JobRunner）、运行会话持久化（B04）、工作台登录与网关（B19/B35）、迁移目录（B03）、API 契约回归之外的业务测试。
