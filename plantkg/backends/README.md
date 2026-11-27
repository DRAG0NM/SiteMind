# B24 判别后端

依据 内部设计文档 固定提交 `224d9c36ec2ec69c60d978174b9d95a1386d603b` 的 B24、03 §4.3、11 与 09 后续决策。开发基于 main `6c4354b`，B02 已合并。没有修改规划原文。

## 看效果

在仓库根目录运行（输出路径须未存在）：

```powershell
python -m plantkg.backends.demo --backend fake --output runtime/b24-domain-demo.json
python -m pytest tests/backends -q
```

输入为 08:05 用电突增、拥堵、空压机启动、低 SOC 的合成事件。fake 的配置分布是电梯 60%、能源 20%、设备监测 15%、暖通 5%；三顺序一致，派域门控通过。这些数字只是演示测试输入，不是模型实测或准确率。导出 JSON 包含完整领域分布、三个逐顺序结果、门控、后端与模型、校准版本、run_id、来源与性质，可被后续演示导出包收录。无校准产物时 `prob=null`；`score`、`distribution` 和 `top_k.prob` 为未校准分数，不能当作已校准概率。

默认配置仍为 `judge.backend: fake`，用 `jev` / `fallback` 切换；bootstrap 登记 `Judge` 端口。服务装配不会发模型请求。业务侧用 `registry.resolve(Judge).decide(state, candidate, options)`。`state` 传 `content`、每个选项的 `definitions`，本体题另传 `vector_top` 与已知的召回 `coverage`（未知时默认 0，不伪造覆盖率）。选项集必须与 `OntologyCandidate.options` 相同。调用 `plantkg.knowledge.judge.dispatch_domains` 可直接生成四领域派发题。

## 行为与实施选择

- 每种顺序先归一，三个分布平均再归一；候选定义逐一送入。Unknown 不进入选项，平分、不一致、响应缺分数都不能放行。**自动放行要求至少三个候选**：一个或两个候选仍返回完整分布与全部不同顺序，但 `gate_accept=false`。调用方必须按门控未放行走慢路径或人工复核，不能根据分数自行放行；派域由 B27 接主 Agent 慢路径，本体由 B30 进复核。三顺序要求来自 B24 任务卡，不能将两次不同顺序重复计作三次。
- 当前默认派域定义有四位专家；B27 任务卡没有确定是否按图谱缩减候选到两类，因此不假设永远有四类。派域输入必须非向量召回且 `vector_top` 为空；本体门控要求三种不同顺序首选一致且与向量首选一致，派域门控只要求顺序一致。门控仅表示判别可用，不替代图谱安全检查、动作置信度与人工工单。
- 保底方案的具体算法在卡片中未写定。本次实施选择为向量分数的 softmax：本体题要求显式 `vector_scores`，且首选与 `vector_top` 相同；派域题要求显式 `domain_scores`。没有分数即报错，不自动生成向量或冒充模型。此保底与向量召回不独立，不能把其一致性当作 Jev 实测的准确率证据；后续可换团队算法。未实现 deepseek 投票对照。
- `TemperatureCalibrator.fit(results, labels, FrozenSplit)` 只收 calibration 划分，后端和模型必须相同；用有限温度网格最小化校准集 NLL，`save/load(Path)` 保存校准产物、划分、样本 ID 与散列。构造后端时传 `calibrator=` 保存结果的 `prob/calibration_version`，其他模型返回空校准概率。调用方仍须保证 calibration 与最终 test 的样本和日期不重叠。当前只有合成测试，没有宣称有效的真实校准产物。
- `JudgeResult` 新增默认 `gate_kind="ontology"` 和可选空 `distribution`，旧结果照常加载，旧本体门控条件保留。公共改动涉及 core 契约、对应生成 Schema 和 bootstrap 装配；没有新增依赖、迁移或确认端点。
- Jev 启动时检查密钥和北京工作空间环境变量；运行时缺失或无效配置转为 `DEPENDENCY_UNAVAILABLE`，请求失败或返回不完整分布时明确报错，不静默切换到 fake。北京端点与请求/响应形状依据仓库 `nexent_assets/probes/b_bailian/p8_decision.py`，单次超时默认 10 秒、无自动重试；三顺序网络预算约 30 秒。离线协议测试用传输替身，不能替代真实服务验收。

## 真实 Jev 状态

用户先选择离线实现，随后授权“使用现有额度调用 3 次”。已完成一条用电突增题的三个真实请求，首选均为设备监测，派域门控通过；原始平均归一分布约为设备监测 95.02%、能源 3.98%、电梯 1.00%、暖通 0%。这是单条合成输入的模型结果，不是准确率或已校准概率。完整记录见 [REAL_CALL_STATUS.md](REAL_CALL_STATUS.md) 与 [JSON](samples/domain-jev-live-2026-11-04.json)。没有追加调用。

plantkg 规则类型与关系层复核样本的真实性能重测仍**待业务验收（不阻塞）**，不根据单条派域题声称通过。

后续另获额度授权后，可设置工作空间与密钥环境变量，显式复验：

```powershell
# DASHSCOPE_API_KEY 仅从环境读取；BAILIAN_WS 为北京工作空间 ID。
python -m plantkg.backends.demo --backend jev --output runtime/b24-jev-live.json
# 若机器已有 DASHSCOPE_WORKSPACE_ID，可使用 --workspace-env DASHSCOPE_WORKSPACE_ID
```

此命令会发起三个真实请求。真实结果导出标 `result_origin=live`，事件素材仍为 `synthetic`。不保存 Authorization、密钥、工作空间或服务错误响应体。当前 3 次额度已用完，再调用须重新取得授权。模型质量、延迟、ECE、规则类型准确率与关系判别效果均未确定。
