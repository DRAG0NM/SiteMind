# 参与开发

欢迎为 SiteMind 提交代码、补全领域包或改进文档。提交流程与工程约定如下。

## 环境

需要 Python 3.11–3.12。安装开发依赖并确认本地可跑通：

```bash
pip install -e ".[storage,dev]"
python -m pytest -q
```

## 工程约定

- **契约先行**：数据结构与端口定义在 `plantkg/core/contracts.py` 和 `plantkg/core/ports.py`。改动后运行 `python contracts/build.py` 更新 `contracts/` 下的生成物（JSON Schema、OpenAPI、MCP 工具清单与样例），提交前执行 `python contracts/build.py --check` 确认无过期。
- **模块边界**：跨包只通过契约、注册表与钩子交互，上游包不 `import` 下游包。提交前运行 `python scripts/check_boundaries.py` 校验依赖方向。
- **数据标注**：每份数据须标明性质（`measured` / `derived` / `simulated` / `synthetic` / `configured`）；设备侧指标统一注明仿真场景来源。
- **测试**：新增功能或修复缺陷时，在 `tests/` 对应目录补一条冒烟测试，并确保 `python -m pytest -q` 全量通过。

## 提交与 PR

- 一个分支只做一件事，不直接向 `main` 推送。
- PR 说明写清改动动机、影响范围与本地验证命令。
- 涉及架构或指标定义变更时，同步更新 `docs/architecture/adr/`、`docs/data/` 或 `docs/experiments/`；日常开发速查见 `docs/DEVELOPMENT.md`。

## 报告问题

请通过 Issue 提交问题，写清复现步骤、当前现象与期望行为；涉及跨模块交互的问题请单独标注。
