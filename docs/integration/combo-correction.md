# 组合异常纠错与执行

B32 的纠错候选可以只包含人工说明。对组合异常，这类规则可在对应设备的慢通道调查中作为人工诊断参考；它没有可执行动作，不能凭此进入快通道或自动执行。

要让已发布规则供设备专家提出自动处置方案，复核员需要在候选中明确事件类型、事件主体、匹配条件和动作。例如（实体 ID 为示例）：

```json
{
  "event_type": "unknown_combo",
  "entity_id": "entity://demo-electronics/lighting/LT-P",
  "note": "值班人员确认的检修结论",
  "condition": {
    "event_type": "unknown_combo",
    "observation_contains": "组合异常",
    "actions": [{"entity_id": "entity://demo-electronics/lighting/LT-P", "command": "repair"}]
  },
  "autonomy_category": "unknown_combo|lighting",
  "autonomy_level": "L2"
}
```

这只是结构示例，不能将其当作实际设备的处置授权。B32 仍须完成回测并由复核员确认发布；待确认动作显示拟授权的类别和等级。规则与策略以运行绑定的清单为准。事件必须匹配规则的主体、条件和时态；B47 还会核对整份提案是否由同一规则授权，并执行既有置信度、动作后果、试算和安全门控。规则发布本身不等于事件命中或动作获准。

回测只按实际场景中可见、生效的目标规则及设备统计覆盖。文字建议须有绑定到具体运行、知识版本、事件和轮次的专家证据；它不计为自动处置。无法可靠归属合批事件的证据、没有覆盖的规则或策略类别，会导致回测不可验证。对于旧版已持久化的 B47 类别等级，迁移按安全上限保守处理；经回测和复核员明确授权的类别才可在新清单解除相应上限。旧运行仍使用自身绑定的清单和限制。

## 谁能提交纠错（operator 与 reviewer）

纠错入口（`propose_candidates_evo`、`evolution.record_correction`，含 CLI 演示脚本）分两类：

- **只记反馈**（`feedback`：批准、驳回、抽查结果）：任何角色都可以提交，不要求复核员。
- **由纠错内容生成候选**（`rule`、一句话纠错、`sensor_case`）：要求上下文里是 reviewer 角色，operator 会得到 `FORBIDDEN`。这是有意保留的：候选里的规则记录为 `review_status=approved`，`reviewer` 必须是真实批准内容的人，operator 没有批准权限，不能替他署名。operator 发现问题时，应通过 B47 驳回/纠错反馈记账（反馈照常累计；自主等级候选只在复核员上下文里生成），或把一句话纠错交给 reviewer 提交；发布仍须 reviewer 确认。

## B36 演示接入

默认场景的 `correct` 操作不会自动执行 B32 的复核发布流程。B36 可在启动场景前包装 `scenario.document_upload` 钩子，先调用原钩子，再针对 `act4-combo` 模拟复核员按纠错、回测、提出发布、确认的顺序操作。以下片段假设已有 `services`、场景 `scenario` 和由 `default_registry(services.registry)` 创建的 `runtime`，应在 `runner.start()` 前注册；示例数据为 synthetic，确认动作模拟人工复核：

```python
from plantkg.core.contracts import Actor, Role
from plantkg.core.ports import PendingActionStore, VersionStore
from plantkg.evolution.service import EvolutionService, evolution_config
from plantkg.evolution.store import EvolutionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts

registry = services.registry
original_upload = runtime.hooks_for("scenario.document_upload")[-1]

def upload_and_review(runner, at, payload):
    original_upload(runner, at, payload)
    if payload.get("event") != "act4-combo":
        return
    ticket = next(t for t in runner.trace.tickets.values() if "act4-combo" in t.get("event_ids", []))
    reviewer = Actor(actor_id="demo-reviewer", roles=(Role.reviewer,))
    context = runner._context(runner.run_info.version_manifest_id).model_copy(
        update={"actor": reviewer, "run_id": runner.run_info.run_id, "as_of": at})
    correction = {
        "note": "照明与空压异常由生产总配电柜接触不良引起",
        "event_type": "unknown_combo", "entity_id": runner.ids["LT-P"],
        "autonomy_category": "unknown_combo|lighting", "autonomy_level": "L2",
        "condition": {"event_type": "unknown_combo", "observation_contains": "组合异常",
                      "actions": [{"entity_id": runner.ids["LT-P"], "command": "repair"}]},
    }
    result = registry.hooks_for("evolution.record_correction")[-1](context, ticket["ticket_id"], correction)
    if not result.ok:
        raise RuntimeError(result.to_agent_text())
    actions, versions = registry.resolve(PendingActionStore), registry.resolve(VersionStore)
    evolution = EvolutionService(EvolutionStore(versions.writer, versions.plant_id), versions, actions,
                                 RuleArtifacts(versions.writer, versions.plant_id), registry, context,
                                 config=evolution_config(services.settings))
    report = evolution.backtest(result.data["candidate_id"], [{
        "scenario_id": "factory_demo_day", "seed": scenario.seed,
        "path": "configs/scenarios/factory_demo_day.yaml"}])
    if report.verdict != "pass":
        raise RuntimeError(f"回测未通过: {report.failures}")
    proposal = registry.tools["propose_publish"].handler(
        context, candidate_id=result.data["candidate_id"], rationale="模拟复核")
    if not proposal.ok:
        raise RuntimeError(proposal.to_agent_text())
    action = actions.get(proposal.data["action_id"])
    actions.confirm(action.action_id, reviewer, "模拟人工确认发布", action.revision, "demo-combo-publish")

runtime.register_hook("scenario.document_upload", upload_and_review)
```

完整可运行的装配和两日断言见 `tests/evolution/test_issue_260.py`。实际应用中应由有权限的复核员确认，不应自动代替人工授权。
