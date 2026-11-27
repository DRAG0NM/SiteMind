# B16 回放执行：原始前置诊断（保留）

本文件保留公共前置修复之前的原始诊断。当前服务接口、使用方法、数据边界与限制请看 [IMPLEMENTATION.md](IMPLEMENTATION.md)；以下关于尚无 Executor 的陈述是历史记录，不是当前代码状态。

本目录尚无 Executor 实现，不能用于执行或验收。以下为当前分支对 B07 接口的可复现检查；测试数据仅为 synthetic/test_only。

## 阻塞：槽结束的实测数据与原子推进缺少公共接缝

`SourceRecord` 对 `measured` 区间要求 `available_at >= interval.end`。B07 `ReplaySeries.query` 通过 `ReplayClock.current` 拒绝 `context.as_of` 晚于**已提交**游标；B03 按 `available_at <= context.as_of` 过滤数据。因此，对于从当前游标开始的第一个 15 分钟槽，提交前既不能用当前截止时间读到该槽完整实测值，也不能使用槽结束时点作为截止时间读取。B07 `advance` 自行提交事务，并未提供供执行事实和游标共同提交的公共事务参与接口。先单独推进再计算/保存违反 B16 原子性；直接查询底层存储或手写时钟规则则绕过 B07 可见性和所有权。

运行 `python -m pytest tests/execution/test_replay_prerequisite.py -q` 可复现：查询当前槽返回空，使用未来截止时间报 `NOT_VISIBLE`；只有 `advance` 已提交后才读到实测记录。该测试使用真实 B03 SQLite/Parquet、B07 回放时钟及冻结来源清单；它只证明接口限制，不是 B16 执行测试。

建议 B07 提供受信任、仅内部使用的两阶段接缝：在验证 run/manifest、来源白名单和候选目标时点后，允许为候选已结束槽准备数据，结果在游标提交前不得对普通读取者可见；并提供由调用方事务使用的时钟 CAS 参与接口，在同一 SQLite 短事务内重验游标、状态及候选边界。还需定义采集延迟使 `available_at` 晚于槽结束时的等待/拒绝语义。B16 可在事务外计算、事务内重新校验身份与批准状态并一并提交事实和时钟。具体 API 和并发约束需由 B07/B15 所有者确认；不能通过调用者注入 actuals 代替。

## 其他未完成事项

当前仓库缺少指南指定的 `内部设计文档/docs/development/branch-plan.json`；工作区上级有同名文件，但不能假定它与本次远程提交一致。B15 默认材料提供者仍缺少已核验的正式规则、费用等材料，参见 `plantkg/decision/gate/README.md`。尚无执行迁移、动作映射、状态投影、执行账本、服务注册或 B17 读取接口；不得将本诊断视为交付。
