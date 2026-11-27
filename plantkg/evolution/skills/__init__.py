"""B43：重复成功的处置流程 → 候选 Skill → 评估 → 发布/回滚（沿用 B32 引擎）。

模块划分：
- miner：执行记录 + B32 成功反馈 → 重复成功流程（Pattern）
- generator：Pattern → 候选 Skill 体与 Nexent SKILL.md，经 B32 的 skill_artifacts 落库
- validation：样本工单重放命中率、硬约束、禁区命令 → validated / rejected
- history：某 Skill 的版本链（发布/回滚记录）
- routes：GET /evolution/skills 与 /evolution/skills/{name}/history
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from plantkg.core.contracts import Actor, CandidateKind, ErrorCode, PlantkgError, Role
from plantkg.core.ports import CandidateGenerator, PendingActionStore, ValidationPolicy, VersionStore
from plantkg.evolution.artifacts import skill_artifacts
from plantkg.evolution.store import EvolutionStore

# 挖掘候选的系统 actor：反馈人（常是普通操作员）不直接写候选产物，由 B43 用它准备 artifact；
# 发布仍必须由复核员经统一确认端点确认，这里不放宽发布权限。
MINER_ACTOR_ID = "b43-skill-miner"


def skill_config(settings: Any = None) -> dict[str, Any]:
    """B43 参数（configs/defaults.yaml 的 evolution 节，标 configured）。"""
    data: dict[str, Any] = {}
    if settings is not None:
        section = settings.section("evolution") if hasattr(settings, "section") else None
        if isinstance(section, Mapping):
            data = dict(section)
    return {"min_successes": int(data.get("skill_min_successes", 3))}


def register(registry: Any, settings: Any = None) -> None:
    """B43 包入口（由 packs/skill_evolution 加载在 evolution 之后）：挖掘、候选生成、评估与只读路由。

    没有存储装配时什么都不挂：挖出的候选无处落库，发布/回滚本来就依赖 B04 端口。
    """
    from plantkg.evolution.skills import generator, miner, routes, validation

    if VersionStore not in registry.ports or PendingActionStore not in registry.ports:
        return
    versions = registry.resolve(VersionStore)
    artifacts = skill_artifacts(versions.writer, versions.plant_id)
    if not versions.has_verifier(artifacts.prefix):
        versions.register_verifier(artifacts.prefix, artifacts.verify)
    store = EvolutionStore(versions.writer, versions.plant_id)
    store.registry = registry
    config = skill_config(settings)
    skill_generator = generator.SkillCandidateGenerator(versions, store, artifacts,
                                                       min_successes=config["min_successes"])
    policy = validation.SkillValidationPolicy(artifacts, registry=registry)
    registry.register(CandidateGenerator, skill_generator)
    registry.register(ValidationPolicy, policy)

    def mine_on_feedback(context, event_ticket, feedback):
        """B32 record_feedback 成功反馈后触发一次挖掘（节流：同类已有待发布候选就跳过）。"""
        category = str(feedback.get("event_type") or "").strip()
        event_type = category.split("|", 1)[0]
        base = versions.active()
        if not event_type or generator.has_pending_candidate(store, event_type, base.version_manifest_id):
            return
        # 优先用 B47 登记的真实执行记录；钩子缺失才回退合成 fixtures，并标 coverage_gap
        source_records, coverage_gap = miner.execution_records(registry)
        records = miner.merge_feedback(source_records, store.feedback_rows(category))
        patterns = [replace(pattern, coverage_gap=coverage_gap) for pattern in miner.mine_patterns(
            records, min_successes=config["min_successes"], event_type=event_type)]
        if not patterns:
            return
        # 反馈人的 context 只用来记原始 actor；写候选产物用 B43 系统 actor（需要写产物的角色）。
        feedback_actor = context.actor.actor_id if context.actor else None
        miner_context = context.model_copy(
            update={"actor": Actor(actor_id=MINER_ACTOR_ID, roles=(Role.operator, Role.reviewer))})
        for pattern in patterns:
            skill_generator.propose({"pattern": pattern, "run_id": context.run_id or context.trace_id,
                                     "source": "feedback", "event_ticket": event_ticket,
                                     "feedback_actor": feedback_actor}, base, miner_context)

    registry.register_hook("evolution.skill_mining", mine_on_feedback)

    def skill_publish_check(_context, candidate):
        """B32 发布前（propose_publish 与 PublishHandler.apply）对 Skill 候选评估一次。

        真实反馈链路没有单独的评估入口，缺 skill_evaluated 记录时就在这里用候选已有的回测
        报告判定并落一条记录；不要求候选处于 proposed（提议发布前的快回测已把它置 validated）。
        判定按 skill 体格式分流：B43 procedure 体做完整评估，B32 investigation 体跳过样本重放。
        """
        if candidate.kind != CandidateKind.skill:
            return
        if not candidate.backtest_report_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "Skill 候选发布前缺少回测报告")
        try:
            report = store.report(candidate.backtest_report_id)
        except PlantkgError:
            raise PlantkgError(ErrorCode.PRECONDITION, "Skill 候选的回测报告不存在") from None
        evaluation = _latest_evaluation(store, candidate.candidate_id)
        if (evaluation is not None and evaluation.get("verdict") != "validated"
                and evaluation.get("format") is None):
            # 旧门的评估记录没有 format：当时按 procedure 口径拒了 investigation 体，
            # 按当前格式口径重判一次并落新记录，不把既有候选永久卡住。
            evaluation = None
        if evaluation is not None and evaluation.get("verdict") != "validated":
            raise PlantkgError(ErrorCode.PRECONDITION,
                               f"Skill 候选发布前须先通过 B43 评估（{evaluation.get('verdict')}）")
        result = policy.evaluate(candidate, report)
        if evaluation is None:
            validation.record_evaluation(
                store, candidate_id=candidate.candidate_id, report=report, result=result,
                run_id=getattr(_context, "run_id", None) or getattr(_context, "trace_id", None))
        if result["verdict"] != "validated":
            raise PlantkgError(ErrorCode.PRECONDITION, "Skill 候选未通过 B43 评估：" + "；".join(result["reasons"]))

    registry.register_hook("evolution.skill_publish_check", skill_publish_check)
    routes.register_rest(registry, versions, store)


def _latest_evaluation(store: EvolutionStore, candidate_id: str) -> Mapping[str, Any] | None:
    """候选最近一次 B43 评估记录；最近一次 rejected 时门拒绝，没有记录时门就地评估并补写。"""
    for event in reversed(store.timeline()):
        if event.get("event") == "skill_evaluated" and event.get("candidate_id") == candidate_id:
            return event
    return None
