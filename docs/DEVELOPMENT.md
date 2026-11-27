# 开发指南

本指南用于日常本地开发与目录约定速查；分支、PR 与 Issue 流程见根目录 `CONTRIBUTING.md`。

## 常用校验命令

```bash
python contracts/build.py                    # 按代码重新生成 contracts/ 下的契约文件
python contracts/build.py --check            # 校验契约生成物是否过期
python scripts/check_boundaries.py           # 检查跨包单向依赖与模块边界
python -m pytest -q                          # 运行全量测试
```

## 开发约束

- **契约唯一源**：记录与端口只改 `plantkg/core/contracts.py`、`plantkg/core/ports.py`，不手改 `contracts/` 下的生成文件。
- **新增领域包**：在 `plantkg/packs/<名称>/` 下放置 `pack.yaml`，系统启动时自动发现并注册，无需修改上游核心代码。
- **文档与设计优先级**：已定取舍记录在 `docs/architecture/adr/`；实现与文档有分歧时，以 ADR 及 `docs/architecture/` 下的设计说明为准。
- **数据标注**：测试样例与场景数据须标注 `measured` / `derived` / `simulated` / `synthetic` / `configured` 五档性质之一，设备侧指标注明仿真场景来源。
- **冒烟测试**：新增模块或工具时，在 `tests/` 对应子目录补充最小冒烟测试。
