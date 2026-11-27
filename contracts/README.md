# contracts：plantkg 公共契约（B01）

契约版本 **1.2.0**（1.2.0：v0.8 契约前置（B46），只加字段、记录与端口，旧数据照常加载；1.1.0：HealthResponse 依赖状态增加 unchecked、placeholder、fake；版本号以 `CONTRACT_VERSION` 为准，并有测试保证本行一致）。权威定义在 `plantkg/core/contracts.py`（记录、枚举、常量、REST 路由表、MCP 工具全集）与 `plantkg/core/ports.py`（端口）；本目录是由它们生成的、只用标准库 `json` 就能读的副本，加上手写样例与平台事件约定。

| 路径 | 内容 | 来源 |
| --- | --- | --- |
| `schemas/*.schema.json` | 每个契约记录的 JSON Schema（draft 2020-12） | 生成 |
| `enums.json` | 全部枚举、平台常量、错误码与 HTTP 映射、四个状态机迁移表 | 生成 |
| `ports.json` | 全部端口的方法签名 | 生成 |
| `openapi/plantkg.openapi.json` | REST 全集（`/api/v1`），OpenAPI 3.1 | 生成 |
| `mcp/tools.json` | MCP 工具全集：Nexent 名、平面、提供分支、是否要 ctx、输入 schema | 生成 |
| `events/events.json` | UIEvent 类型、必需 payload 键、终态原因；证据事件类型 | 生成 |
| `events/nexent-sse-mapping.md` | Nexent v2.8.2 北向 SSE → UIEvent 的合流规则 | 手写 |
| `examples/valid/`、`examples/invalid/` | 有效样本与单项改动的反例（`expect_error` 为报错片段） | `examples/make_examples.py` |
| `examples/mcp/`、`examples/rest/` | 工具调用与 REST 往返样例 | `examples/make_examples.py` |
| `day.schema.json` | “一天”数据格式（`DayRecord`），开发工作台实时流与回放展示页共用 | 生成 |
| `examples/day.sample.json` | “一天”手写样例；`python -m plantkg.contracts.validate contracts/examples/day.sample.json` 校验 | 手写 |

## JSON Schema 能管到哪里

JSON Schema 只表达结构：字段、类型、枚举、取值范围、时间格式（UTC 文本，与 pydantic 同一正则）。跨字段规则（如“optimal 必须带计划”“确认必须有原因”“账本残差在容差内”）只在 pydantic 的 `model_validator` 里，`examples/invalid/` 中大部分反例 schema 拦不住。标准库消费方（如前端）把 schema 当作结构校验；业务一致性以 plantkg 服务端为准。另外 pydantic 对标量是宽松模式（`"5"` 可转成整数），schema 更严，以 schema 为外部口径。

## 命令

```bash
python contracts/build.py                  # 改 contracts.py / ports.py 后重新生成
python contracts/build.py --check          # 校验生成物最新（CI 用）
python contracts/examples/make_examples.py # 重新生成样例
python -m pytest tests/contracts           # 契约测试
```

## 依赖需求（交 B02 在 pyproject 中集中声明）

- 运行：`pydantic>=2.14,<3`（core 唯一的第三方依赖）。
- 测试：`pytest`、`jsonschema>=4.26`（校验生成的 schema 与样例一致）。
- Python 3.11（本机探针环境 3.11.9）。生成物以 Python 3.11 + pydantic 2.14.1 为准；`ports.json` 只取端口自身文档，不受 Python 版本影响，但 JSON Schema 的细节随 pydantic 版本变化，`--check` 须在锁定版本下运行。

B01 不建 pyproject；在 B02 落地前用任一装有上述依赖的环境在仓库根运行即可。

## 改契约

契约合并后由契约维护人负责。已知变更走独立的契约前置变更：改 `contracts.py`/`ports.py` → 运行 `build.py` 与 `make_examples.py` → 契约测试全绿 → 单独 PR。其他分支不自行改字段名或枚举。新增字段优先可选；删除或改名须升 `CONTRACT_VERSION` 主版本并给兼容样例。
