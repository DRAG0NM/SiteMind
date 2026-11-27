# 模块所有权与导入规则

依据 development/04「完整推荐目录」与 branch-plan.json 的 owned_paths。每个共享文件只有一个合并者；后续分支只写自己卡片的目录，公共入口、契约与配置的改动交维护人前置处理。

## 契约与公共入口的维护人

| 文件/目录 | 维护分支 | 规则 |
| --- | --- | --- |
| `plantkg/core/contracts.py`、`plantkg/core/ports.py`、`contracts/` | B01（合并后由契约维护人） | 改动走契约前置变更，见 `contracts/README.md` |
| `pyproject.toml`、`plantkg/__main__.py`、`plantkg/bootstrap.py`、`plantkg/api.py`、`plantkg/mcp_server.py`、`configs/defaults.yaml`、`configs/plant.yaml` | B02（骨架维护人） | 模块以清单注册，不逐个修改入口；依赖由骨架维护人统一声明 |
| `plantkg/core/config.py`、`registry.py`、`security.py`、`plantkg/testing/` | B02 | |
| `plantkg/core/versions.py`、`runs.py`、`evidence.py`、`sessions.py`、`jobs.py`、`actions.py` | B04 | 统一确认端点的执行函数由 B15/B18/B24 注册，不改 actions.py |
| `plantkg/core/ledger.py` | B08 | |
| `docs/dev-log.md` | 当期集成者 | 串行汇总 |

## 平面与包目录

| 目录 | 所有者 |
| --- | --- |
| `plantkg/storage/`（runner 与仓储） | B03；`storage/migrations/<模块>/` 归对应模块（B04/B06/B15/B16/B18/B24/B25） |
| `plantkg/adapters/` | B05 |
| `plantkg/knowledge/assets/` | B06 |
| `plantkg/knowledge/parsing/`、`ontology/`、`calibration/`、`review/`、`rules/`、`retrieval/`、`changes/` | B21、B22、B23、B24、B25、B26、B27 |
| `plantkg/knowledge/subscriptions/` | B52（可选） |
| `plantkg/world/replay/`、`world/record/` | B07、B16 |
| `plantkg/decision/planner/`、`forecasting/`、`gate/`、`evaluation/`、`explanation/` | B09、B14、B15、B17、B26 |
| `plantkg/application/run_day.py` | B17 |
| `plantkg/evolution/engine/`、`backtest/`、`generators/parameters/` | B18 |
| `plantkg/evolution/generators/knowledge/`、`generators/skills/` | B30、B42（可选） |
| `plantkg/packs/pv/`、`storage/`、`tariff/`、`production/` | B10、B11、B12、B13；`production/multi/` B28 |
| `plantkg/packs/environment/`、`hvac/`、`compressed_air/`、`elevator/`、`lighting/` | B41、B45～B48（可选） |
| `plantkg/backends/` | B23 |
| `plantkg/integrations/nexent/gateway/`、`tasks/` | B19、B29 |
| `nexent_assets/probes/`、`bootstrap/`、`agent/`、`skills/`、`skill_candidates/` | B02、B19、B29、B29、B42 |
| `web/` | B35 起；`src/features/plans|evidence|review|versions|timeline|source_viewer/` 归 B36/B37/B38/B39/B39/B51 |
| `evaluation/`、`scripts/loop.py`、`scripts/package_submission.py` 等 | 见各任务卡 |

## 导入规则（B02 `scripts/check_boundaries.py` 强制，契约测试先查 core）

1. `plantkg/core/` 只导入标准库、`pydantic` 与 `plantkg.core.*`；不导入 packs、backends、任何平面实现。
2. `packs/`、`backends/` 实现 core 端口，只依赖 `plantkg.core`。
3. 平面（knowledge/decision/world/evolution）只依赖 core 与自身子模块；跨平面只交契约记录，不调用对方内部函数。
4. `application/`、`bootstrap.py` 通过端口装配多个平面，可以调用端口，不导入另一平面的私有函数。
5. `api.py`、`mcp_server.py` 是薄入口：只做校验、上下文绑定、端口调用与结果转换，不写策略。
6. `web/` 不直连模型或 Nexent；浏览器 bundle 不含任何 Key。
7. 新增一个领域包时，`git diff` 只出现在 `packs/<新包>/`（`packs: auto` 自动发现，03 §1.1 接口验收）。
