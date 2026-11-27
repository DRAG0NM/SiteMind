# B43 候选 Skill：挖掘、评估、发布 / 回滚

重复成功的处置流程 → 候选 Skill → 评估 → 发布 / 回滚。发布与回滚沿用 B32 的统一确认动作，不另造通道。

## 怎么看效果

```powershell
$env:PYTHONPATH = "<工作区>"
python -m plantkg.evolution.skills demo
```

输出依次是：挖到的流程与动作签名、候选版本、评估结论（命中率 + 回测）、发布 v1、再挖 v2、回滚 v1，
最后一张版本记录表（version / action / manifest / candidate / verdict / at / operator / source_tickets / run_id / current）。

保留结果后单独查版本记录：

```powershell
python -m plantkg.evolution.skills demo --db runtime/b43-demo.db
python -m plantkg.evolution.skills history compressor-pressure-drop-procedure-<签名哈希> --db runtime/b43-demo.db
```

## 数据与接口

- `skills/fixtures/executions.json`：合成处置记录（synthetic），v1 三条、v2 两条同签名工单。
- `CandidateGenerator`（kind=skill）、`ValidationPolicy`（b43-v1）经注册表登记；包 `packs/skill_evolution` 加载在 evolution 之后。
- `GET /evolution/skills` 与 `GET /evolution/skills/{name}/history`：只读版本记录，供工作台。
- B32 `record_feedback` 的成功反馈经 `evolution.skill_mining` 钩子触发一次挖掘；同类已有待发布候选时跳过。
- 发布前由 `evolution.skill_publish_check` 钩子在 `propose_publish` 与 `PublishHandler.apply` 各评估一次：缺 B43 评估记录时直接用候选已有回测报告判定并落 `skill_evaluated`，真实链路“挖掘 → 提议发布 → 复核员确认”不用单独调评估入口；禁区命令按 B28 词表归一，步骤文本含停机/断电/切断也否决。
- 发布后推 Nexent 的 live 上传未实现（卡里标可选、默认关）。
