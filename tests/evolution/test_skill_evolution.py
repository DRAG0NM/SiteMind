"""B43 验收冒烟与审查回归：重复成功 → 候选 Skill → 评估 → 人确认发布/回滚 → 版本记录。

合成处置记录与 fixtures 数据标 synthetic；发布/回滚全部走 B32 的统一确认动作存储
（POST /actions/{id}/confirm 背后的同一实现），不另造发布通道。
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import (Actor, Candidate, CandidateKind, CandidateStatus, ComponentRef, Context,
                                    ErrorCode, ManifestComponent, PlantkgError, Role)
from plantkg.core.ports import CandidateGenerator, PendingActionStore, ValidationPolicy, VersionStore
from plantkg.core.registry import Registry
from plantkg.core.versions import SqliteVersionStore
from plantkg.evolution.artifacts import skill_artifacts
from plantkg.evolution.service import EvolutionService, PublishHandler, RollbackHandler
from plantkg.evolution.skills import history, miner
from plantkg.evolution.skills import register as skills_register
from plantkg.evolution.skills.miner import Pattern
from plantkg.evolution.skills.validation import SkillValidationPolicy, evaluate_skill_candidate
from plantkg.evolution.store import EvolutionStore
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


def _evaluator(_candidate, scenario, _context):
    return {"scenario_id": scenario.get("scenario_id", "b43-smoke"), "executed": True, "ledger_verified": True,
            "target_events": 0, "targets_unmeasurable": True,
            "metrics": {"target": {"base": 0.0, "candidate": 0.0},
                        "cost_cny": {"base": 10.0, "candidate": 10.0}},
            "constraint_violations": {"base": 0, "candidate": 0}}


def _environment(tmp_path):
    writer = SQLiteWriter(tmp_path / "skills.db")
    MigrationRunner(writer).apply()
    versions = SqliteVersionStore(writer, "factory")
    versions.register_verifier("scenario-knowledge:", lambda _component: "complete")
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    base = versions.prepare({"rules": ComponentRef(ref="scenario-knowledge:empty", content_hash="sha256:" + "0" * 64)},
                            "b43 smoke baseline")
    if base.status == "prepared":
        base = versions.check_integrity(base.version_manifest_id)
    versions.activate(None, base.version_manifest_id, reviewer, "b43 smoke baseline")
    registry = Registry()
    actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
    context = Context(plant_id="factory", industry_pack="manufacturing", actor=reviewer, channel="offline",
                      trace_id="b43-smoke", as_of=datetime(2026, 7, 1, 9, tzinfo=timezone.utc))
    store = EvolutionStore(writer, "factory")
    store.registry = registry
    artifacts = skill_artifacts(writer, "factory")
    versions.register_verifier(artifacts.prefix, artifacts.verify)
    service = EvolutionService(store, versions, actions, None, registry, context, evaluator=_evaluator)
    registry.register_action_handler("publish", PublishHandler(versions, None, store, service))
    registry.register_action_handler("rollback", RollbackHandler(versions, store))
    registry.register(VersionStore, versions)
    registry.register(PendingActionStore, actions)
    skills_register(registry, SimpleNamespace(
        industry_pack="manufacturing",
        section=lambda name: {"skill_min_successes": 3} if name == "evolution" else {}))
    return SimpleNamespace(writer=writer, versions=versions, actions=actions, reviewer=reviewer, context=context,
                           store=store, artifacts=artifacts, service=service, registry=registry,
                           generator=registry.resolve(CandidateGenerator),
                           policy=registry.resolve(ValidationPolicy), base=base)


def _body(env, candidate):
    return env.artifacts.load(candidate.changes[ManifestComponent.skills])


def _pattern(tickets, commands=(("air_compressor", "ventilate"),), event_type="compressor_pressure_drop"):
    steps = tuple({"order": index + 1, "entity_type": entity_type, "command": command,
                   "example_entity_id": f"entity://factory/{entity_type}/P{index + 1}",
                   "text": f"对 {entity_type} 执行 {command}"}
                  for index, (entity_type, command) in enumerate(commands))
    return Pattern(pattern_id="b43-pattern-test", event_type=event_type, signature=tuple(commands), steps=steps,
                   source_tickets=tuple(tickets), success_count=len(tickets), run_id=None,
                   evidence=tuple({"ticket_id": ticket,
                                   "actions": [[entity_type, command] for entity_type, command in commands]}
                                  for ticket in tickets))


def _step(command, text=None):
    return {"order": 1, "entity_type": "air_compressor", "example_entity_id": "entity://factory/air_compressor/AC-1",
            "command": command, "text": f"对 air_compressor 执行 {command}" if text is None else text}


def _skill_body(name, steps, tickets=("T-1",)):
    return {"format_version": 1, "kind": "procedure", "name": name, "version": 1,
            "event_type": "compressor_pressure_drop", "title": "测试候选", "steps": steps,
            "preconditions": [], "source_tickets": list(tickets), "success_count": 3,
            "evidence": [{"ticket_id": ticket,
                          "actions": [[step["entity_type"], step["command"]]
                                      for step in steps if step.get("command")]} for ticket in tickets],
            "run_id": "run-b43-test", "source": "test", "nature": "synthetic", "skill_md": "# test"}


def _candidate_with_body(env, body, candidate_id):
    ref = env.artifacts.prepare(body, env.reviewer)
    candidate = Candidate(candidate_id=candidate_id, kind=CandidateKind.skill,
                          base_manifest_id=env.versions.active().version_manifest_id,
                          changes={ManifestComponent.skills: ref}, status=CandidateStatus.proposed,
                          trigger="测试：手写候选", created_at=datetime.now(timezone.utc),
                          validation_policy_version="b43-v1")
    env.store.put_candidate(candidate)
    return candidate


class _VocabularyRegistry:
    """只有 B28 词表钩子的最小注册表，验证 B43 侧经钩子归一命令。"""

    def __init__(self, aliases, cut_supply):
        self.aliases, self.cut_supply = aliases, cut_supply

    def hooks_for(self, name):
        if name == "b43.action_vocabulary":
            return (lambda: {"aliases": self.aliases, "cut_supply": self.cut_supply},)
        return ()


def test_repeated_success_mines_publishes_and_rolls_back(tmp_path):
    env = _environment(tmp_path)
    try:
        all_records = miner.load_execution_records()
        v1_records = [record for record in all_records if record.get("run_id") == "run-b43-v1"]
        patterns = miner.mine_patterns(v1_records)
        assert len(patterns) == 1, "合成记录应稳定挖出一个重复成功流程"
        pattern = patterns[0]
        assert pattern.success_count >= 3 and pattern.nature == "synthetic"

        candidate = env.generator.propose({"pattern": pattern, "run_id": "run-b43-smoke-v1"},
                                          env.versions.active(), env.context)[0]
        assert candidate.kind == CandidateKind.skill
        body = _body(env, candidate)
        assert body["version"] == 1 and body["steps"] and body["skill_md"].startswith("---")
        assert body["source_tickets"] and body["nature"] == "synthetic"

        evaluated = evaluate_skill_candidate(env.service, env.policy, candidate.candidate_id)
        assert evaluated["verdict"] == "validated" and evaluated["report"].verdict == "pass"
        action = env.service.propose_publish(env.store.candidate(candidate.candidate_id))
        env.actions.confirm(action.action_id, env.reviewer, "冒烟：复核员确认发布 v1", 0, "b43-smoke-publish-v1")
        v1_manifest = env.versions.active().version_manifest_id
        assert env.versions.active().components[ManifestComponent.skills] == candidate.changes[ManifestComponent.skills]

        all_patterns = miner.mine_patterns(all_records)
        assert all_patterns[0].success_count > pattern.success_count
        candidate2 = env.generator.propose({"pattern": all_patterns[0], "run_id": "run-b43-smoke-v2"},
                                           env.versions.active(), env.context)[0]
        assert _body(env, candidate2)["version"] == 2
        evaluated2 = evaluate_skill_candidate(env.service, env.policy, candidate2.candidate_id)
        assert evaluated2["verdict"] == "validated"
        action2 = env.service.propose_publish(env.store.candidate(candidate2.candidate_id))
        env.actions.confirm(action2.action_id, env.reviewer, "冒烟：复核员确认发布 v2", 0, "b43-smoke-publish-v2")
        v2_manifest = env.versions.active().version_manifest_id
        assert v2_manifest != v1_manifest
        assert env.versions.active().components[ManifestComponent.skills] == candidate2.changes[ManifestComponent.skills]

        rollback = env.service.propose_rollback(v1_manifest, "冒烟：v2 回归后回滚")
        env.actions.confirm(rollback.action_id, env.reviewer, "冒烟：复核员确认回滚 v1", 0, "b43-smoke-rollback")
        assert env.versions.active().version_manifest_id == v1_manifest

        records = history.skill_history(env.versions, env.store, body["name"])
        assert [record.action for record in records] == ["publish", "publish", "rollback"]
        assert [record.version for record in records] == [1, 2, 1]
        assert records[-1].manifest_id == v1_manifest and records[-1].current
        assert not records[1].current
        for record in records:
            assert record.verdict == "pass" and record.actor == "reviewer"
            assert record.source_tickets and record.run_id
        summaries = history.skill_summaries(env.versions, env.store)
        assert [summary.name for summary in summaries] == [body["name"]]
        assert summaries[0].status == "active" and summaries[0].latest_version == 1
        versions_page = env.registry.rest[("GET", "/evolution/skills/{name}/history")](
            env.context, {"name": body["name"]}, {})
        assert [record.action for record in versions_page.items] == ["publish", "publish", "rollback"]
        assert versions_page.items[-1].current
    finally:
        env.writer.close()


def test_skill_with_forbidden_command_is_rejected(tmp_path):
    env = _environment(tmp_path)
    try:
        candidate = _candidate_with_body(env, _skill_body("forbidden-procedure", [_step("stop")]),
                                         "b43-forbidden-1")
        evaluated = evaluate_skill_candidate(env.service, env.policy, candidate.candidate_id)
        assert evaluated["verdict"] == "rejected"
        assert any("stop" in reason for reason in evaluated["reasons"])
        assert env.store.candidate(candidate.candidate_id).status == CandidateStatus.rejected
        with pytest.raises(PlantkgError) as error:
            env.service.propose_publish(env.store.candidate(candidate.candidate_id))
        assert error.value.code == ErrorCode.PRECONDITION
    finally:
        env.writer.close()


def test_success_feedback_triggers_mining_once(tmp_path):
    env = _environment(tmp_path)
    try:
        env.service.record_feedback("ticket-feedback-1", {"category": "compressor_pressure_drop",
                                                         "outcome": "approved"})
        created = [event for event in env.store.timeline() if event["event"] == "skill_candidate_created"]
        assert len(created) == 1, "成功反馈应触发一次挖掘并生成候选 Skill"
        env.service.record_feedback("ticket-feedback-2", {"category": "compressor_pressure_drop",
                                                         "outcome": "approved"})
        created = [event for event in env.store.timeline() if event["event"] == "skill_candidate_created"]
        assert len(created) == 1, "同类已有待发布候选时不再重复挖"
        page = env.registry.rest[("GET", "/evolution/skills")](env.context, {}, {"page_size": "5"})
        assert page.items, "只读路由应能列出候选 Skill"
    finally:
        env.writer.close()


def test_demo_runs_end_to_end(tmp_path):
    """P0-1 回归：直接跑 run_demo（命令行内部链路），不手搭服务。"""
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.evolution.skills.demo import run_demo

    settings = load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        assert services.registry.hooks_for("b43.action_vocabulary"), "装配层应登记 B28 词表钩子"
        lines = []
        result = run_demo(services, out=lines.append)
    finally:
        services.close()
    assert result["ok"] is True
    assert [record.action for record in result["history"]] == ["publish", "publish", "rollback"]
    assert any("版本记录" in line for line in lines)


def test_real_dev_assembly_publishes_mined_skill_and_rejects_stop(tmp_path):
    """P1 回归：真实 dev 全装配下，反馈挖掘 → propose_publish → 复核员确认可直接发布。

    门在缺 B43 评估记录时用候选已有的回测报告就地评估并落 skill_evaluated；含 stop 的候选
    走同一条真实入口被拒。复现 r2/real/real_gate.out.txt 的 R1–R4。
    """
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.evolution.skills.demo import _base_manifest

    settings = load_settings(profile="dev", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        registry = services.registry
        versions = registry.resolve(VersionStore)
        actions = registry.resolve(PendingActionStore)
        reviewer = Actor(actor_id="real-reviewer", roles=(Role.reviewer,))
        context = Context(plant_id=versions.plant_id, industry_pack=settings.industry_pack,
                          actor=reviewer, channel="offline", trace_id="b43-real-entry",
                          as_of=datetime(2026, 7, 1, 9, tzinfo=timezone.utc))
        if "scenario-knowledge:" not in versions._verifiers:
            versions.register_verifier("scenario-knowledge:", lambda _component: "complete")
        _base_manifest(versions, reviewer)
        store = EvolutionStore(versions.writer, versions.plant_id)
        store.registry = registry
        tools = registry.tools

        # 真实 B47 反馈入口（evolution.record_correction 钩子）→ B43 挖掘出候选
        recorded = registry.hooks_for("evolution.record_correction")[-1](
            context, "T-B43-REAL-1", {"feedback": {
                "category": "compressor_pressure_drop", "event_type": "compressor_pressure_drop",
                "outcome": "approved", "source": "correction", "system_error": False}})
        assert recorded.ok is True
        created = [event for event in store.timeline() if event.get("event") == "skill_candidate_created"]
        assert len(created) == 1, "成功反馈应经钩子挖出一个候选 Skill"
        mined = store.candidate(created[0]["candidate_id"])
        assert mined.status == CandidateStatus.proposed
        assert not [event for event in store.timeline() if event.get("event") == "skill_evaluated"], \
            "挖掘阶段不应自带评估记录，评估由发布门就地完成"

        action = tools["propose_publish"].handler(context, candidate_id=mined.candidate_id,
                                                 rationale="真实入口：挖掘后直接提议发布")
        actions.confirm(action.data["action_id"], reviewer, "复核员确认发布", 0, "b43-real-publish")
        published = store.candidate(mined.candidate_id)
        assert published.status == CandidateStatus.published
        assert versions.active().components[ManifestComponent.skills] == \
            published.changes[ManifestComponent.skills]
        evaluated = [event for event in store.timeline()
                     if event.get("event") == "skill_evaluated"
                     and event.get("candidate_id") == mined.candidate_id]
        assert len(evaluated) == 1, "提议与确认各过门一次，但只落一条评估记录"
        assert evaluated[0]["verdict"] == "validated"
        assert evaluated[0]["run_id"] == "b43-real-entry"
        assert evaluated[0]["report_id"] == published.backtest_report_id

        # 含 stop 的候选走同一条真实入口：门就地评估 → rejected 并写原因，不产生发布动作
        generator = registry.resolve(CandidateGenerator)
        forbidden = generator.propose(
            {"pattern": _pattern(("T-STOP-1", "T-STOP-2", "T-STOP-3"),
                                 commands=(("air_compressor", "stop"),), event_type="compressor_emergency"),
             "run_id": "b43-real-stop"}, versions.active(), context)[0]
        with pytest.raises(PlantkgError) as error:
            tools["propose_publish"].handler(context, candidate_id=forbidden.candidate_id,
                                             rationale="真实入口：含 stop 的候选")
        assert error.value.code == ErrorCode.PRECONDITION and "stop" in error.value.message
        rejected = [event for event in store.timeline()
                    if event.get("event") == "skill_evaluated"
                    and event.get("candidate_id") == forbidden.candidate_id]
        assert len(rejected) == 1 and rejected[0]["verdict"] == "rejected"
        assert any("stop" in reason for reason in rejected[0]["reasons"])
        assert versions.active().components[ManifestComponent.skills] == \
            published.changes[ManifestComponent.skills], "被拒候选不得改动活动清单"
    finally:
        services.close()


def test_skill_publish_requires_b43_validation(tmp_path):
    """P0-2 回归：没有 B43 validated 评估记录不得发布；PublishHandler 再校验一次。"""
    env = _environment(tmp_path)
    try:
        # 1) 有回测报告但没有 B43 评估记录：propose_publish 拒绝
        no_eval = _candidate_with_body(env, _skill_body("gate-no-eval", [_step("stop")]), "b43-gate-no-eval")
        report = env.service.fast_backtest(no_eval.candidate_id)
        assert report.verdict == "pass", "快回测本身不拦禁区命令，拦它的是 B43 评估"
        with pytest.raises(PlantkgError) as error:
            env.service.propose_publish(env.store.candidate(no_eval.candidate_id))
        assert error.value.code == ErrorCode.PRECONDITION and "评估" in error.value.message

        # 2) 评估 rejected 后绕过 propose_publish 直接确认：PublishHandler.apply 再校验一次
        rejected = _candidate_with_body(env, _skill_body("gate-rejected", [_step("stop")]), "b43-gate-rejected")
        evaluated = evaluate_skill_candidate(env.service, env.policy, rejected.candidate_id)
        assert evaluated["verdict"] == "rejected"
        forced = env.store.candidate(rejected.candidate_id).model_copy(update={"status": CandidateStatus.validated})
        env.store.update_candidate(forced)
        action = env.actions.propose("publish", {
            "target_ref": forced.candidate_id,
            "candidate": forced.model_copy(update={"publish_action_id": "pending"}).model_dump(mode="json"),
            "backtest_report": evaluated["report"].model_dump(mode="json")}, env.context)
        with pytest.raises(PlantkgError) as error:
            env.actions.confirm(action.action_id, env.reviewer, "绕过提议直接确认", 0, "b43-publish-bypass")
        assert error.value.code == ErrorCode.PRECONDITION
        assert env.versions.active().version_manifest_id == env.base.version_manifest_id
    finally:
        env.writer.close()


def test_forbidden_vocabulary_and_text_keywords(tmp_path):
    """P1-3 回归：命令按 B28 词表归一（别名 + 切断类），步骤文本含停机/断电/切断也判禁区。"""
    env = _environment(tmp_path)
    try:
        policy = SkillValidationPolicy(env.artifacts, registry=_VocabularyRegistry(
            {"switch_circuit": ("power_off",), "switch_standby": ("power_off",),
             "isolate_leak": ("stop",), "repair": ("maintenance",), "set_mode": ("zone_mode",)},
            ("power_off", "stop", "maintenance", "shed", "curtail")))

        def verdict(body, candidate_id):
            candidate = _candidate_with_body(env, body, candidate_id)
            report = env.service.fast_backtest(candidate.candidate_id)
            return policy.evaluate(env.store.candidate(candidate.candidate_id), report)

        for index, command in enumerate(("switch_circuit", "switch_standby", "isolate_leak")):
            result = verdict(_skill_body(f"vocab-{index}", [_step(command)]), f"b43-vocab-{index}")
            assert result["verdict"] == "rejected", command
            assert any(command in reason for reason in result["reasons"])
        # 设备检修（repair → maintenance）不等于切断供给：单独放行（#242 E.9）
        assert verdict(_skill_body("vocab-repair", [_step("repair")]), "b43-vocab-repair")["verdict"] == "validated"
        assert verdict(_skill_body("vocab-ok", [_step("ventilate")]), "b43-vocab-ok")["verdict"] == "validated"
        result = verdict(_skill_body("vocab-text", [_step("ventilate", text="然后对 AC-2 执行停机（stop，切断电源）")]),
                         "b43-vocab-text")
        assert result["verdict"] == "rejected"
        assert any("停机" in reason for reason in result["reasons"])
        for index, text in enumerate(("关闭空气压缩机电源", "停止机组运行", "停运空压机", "拉闸断供")):
            result = verdict(_skill_body(f"vocab-synonym-{index}", [_step("ventilate", text=text)]),
                             f"b43-vocab-synonym-{index}")
            assert result["verdict"] == "rejected", text
            assert any("text:" in reason for reason in result["reasons"]), text
    finally:
        env.writer.close()


def test_rejected_candidate_does_not_block_changed_data(tmp_path):
    """P1-4 回归：被拒候选不绑定同一内容；数据变化后生成新候选，未发布 proposed 候选占版本号。"""
    env = _environment(tmp_path)
    try:
        first = env.generator.propose({"pattern": _pattern(("T-1", "T-2", "T-3")), "run_id": "run-sticky-1"},
                                      env.versions.active(), env.context)[0]
        assert _body(env, first)["version"] == 1
        strict = SkillValidationPolicy(env.artifacts, hit_rate_threshold=1.1)
        assert evaluate_skill_candidate(env.service, strict, first.candidate_id)["verdict"] == "rejected"

        same = env.generator.propose({"pattern": _pattern(("T-1", "T-2", "T-3")), "run_id": "run-sticky-2"},
                                     env.versions.active(), env.context)[0]
        assert same.candidate_id == first.candidate_id and same.status == CandidateStatus.rejected
        assert len([event for event in env.store.timeline() if event["event"] == "skill_candidate_created"]) == 1

        changed = env.generator.propose({"pattern": _pattern(("T-1", "T-2", "T-3", "T-4", "T-5")),
                                         "run_id": "run-sticky-3"}, env.versions.active(), env.context)[0]
        assert changed.candidate_id != first.candidate_id
        assert env.store.candidate(changed.candidate_id).status == CandidateStatus.proposed
        assert _body(env, changed)["version"] == 1, "被拒候选不占版本号"

        pending_then_changed = env.generator.propose(
            {"pattern": _pattern(("T-1", "T-2", "T-3", "T-4", "T-5", "T-6")), "run_id": "run-sticky-4"},
            env.versions.active(), env.context)[0]
        assert _body(env, pending_then_changed)["version"] == 2, "未发布的 proposed 候选也占版本号"
    finally:
        env.writer.close()


def test_two_signatures_make_two_skills(tmp_path):
    """P1-5 回归：同一 event_type 下两个动作签名各生成一个候选，不互相覆盖。"""
    env = _environment(tmp_path)
    try:
        def record(ticket, command):
            return {"ticket_id": ticket, "run_id": "run-two-signatures", "event_type": "same_event",
                    "entity_type": "air_compressor", "entity_id": "entity://factory/air_compressor/AC-1",
                    "action_index": 1, "command": command, "outcome": "approved", "system_error": False}

        records = [record(f"T-A{index}", "set_mode") for index in range(1, 4)]
        records += [record(f"T-B{index}", "ventilate") for index in range(1, 4)]
        patterns = miner.mine_patterns(records)
        assert len(patterns) == 2
        candidates = [env.generator.propose({"pattern": pattern, "run_id": "run-two-signatures"},
                                            env.versions.active(), env.context)[0] for pattern in patterns]
        assert len({candidate.candidate_id for candidate in candidates}) == 2
        assert len({_body(env, candidate)["name"] for candidate in candidates}) == 2
        created = [event for event in env.store.timeline() if event["event"] == "skill_candidate_created"]
        assert len(created) == 2
    finally:
        env.writer.close()


def test_history_current_tracks_active_skills_component(tmp_path):
    """P1-6 回归：无关组件再发布换了清单 id，但活动清单里还是这一版 skill 时 current/status 仍对。"""
    env = _environment(tmp_path)
    try:
        candidate = env.generator.propose({"pattern": _pattern(("T-1", "T-2", "T-3")), "run_id": "run-history"},
                                          env.versions.active(), env.context)[0]
        evaluate_skill_candidate(env.service, env.policy, candidate.candidate_id)
        action = env.service.propose_publish(env.store.candidate(candidate.candidate_id))
        env.actions.confirm(action.action_id, env.reviewer, "发布 v1", 0, "b43-history-publish")
        v1_manifest = env.versions.active().version_manifest_id
        name = _body(env, candidate)["name"]

        components = dict(env.versions.active().components)
        components[ManifestComponent.rules] = ComponentRef(ref="scenario-knowledge:empty-next",
                                                           content_hash="sha256:" + "2" * 64)
        unrelated = env.versions.prepare(components, "B32 无关组件发布")
        if unrelated.status == "prepared":
            unrelated = env.versions.check_integrity(unrelated.version_manifest_id)
        env.versions.activate(v1_manifest, unrelated.version_manifest_id, env.reviewer, "B32 无关组件发布")

        records = history.skill_history(env.versions, env.store, name)
        assert records[-1].current and records[-1].manifest_id == v1_manifest
        summary = [item for item in history.skill_summaries(env.versions, env.store) if item.name == name][0]
        assert summary.status == "active" and summary.latest_version == 1

        rollback = env.service.propose_rollback(v1_manifest, "回滚到 skill v1")
        env.actions.confirm(rollback.action_id, env.reviewer, "回滚 v1", 0, "b43-history-rollback")
        assert history.skill_history(env.versions, env.store, name)[-1].current
    finally:
        env.writer.close()


def test_operator_feedback_mines_candidate_but_only_reviewer_publishes(tmp_path):
    """队友复审 P1 回归：operator 的成功反馈也能挖出候选；发布仍须复核员确认，operator 确认被拒。"""
    env = _environment(tmp_path)
    try:
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        operator_context = env.context.model_copy(update={"actor": operator})
        env.service.with_context(operator_context).record_feedback(
            "T-OP-FEEDBACK-1", {"category": "compressor_pressure_drop", "outcome": "approved",
                                "source": "correction", "system_error": False})
        created = [event for event in env.store.timeline() if event["event"] == "skill_candidate_created"]
        assert len(created) == 1, "operator 的成功反馈也应经 B43 系统 actor 挖出候选 Skill"
        assert created[0]["actor"] == "operator" and created[0]["event_ticket"] == "T-OP-FEEDBACK-1"
        candidate = env.store.candidate(created[0]["candidate_id"])
        body = _body(env, candidate)
        assert body["feedback_actor"] == "operator" and body["event_ticket"] == "T-OP-FEEDBACK-1"

        action = env.service.with_context(operator_context).propose_publish(candidate)
        with pytest.raises(PlantkgError) as error:
            env.actions.confirm(action.action_id, operator, "操作工确认发布", 0, "b43-op-confirm")
        assert error.value.code == ErrorCode.FORBIDDEN
        assert env.store.candidate(candidate.candidate_id).status != CandidateStatus.published

        env.actions.confirm(action.action_id, env.reviewer, "复核员确认发布", 0, "b43-reviewer-confirm")
        assert env.store.candidate(candidate.candidate_id).status == CandidateStatus.published
    finally:
        env.writer.close()


def test_b32_investigation_skill_candidate_publishes_and_rejects_stop(tmp_path):
    """队友复审 P1 回归：B32 propose_candidates_evo(kind=skill) 的 investigation 体能发布；
    含“停机”步骤的同类候选在发布门被拒。"""
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.evolution.skills.demo import _base_manifest

    settings = load_settings(profile="dev", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        registry = services.registry
        versions = registry.resolve(VersionStore)
        actions = registry.resolve(PendingActionStore)
        reviewer = Actor(actor_id="real-reviewer", roles=(Role.reviewer,))
        context = Context(plant_id=versions.plant_id, industry_pack=settings.industry_pack,
                          actor=reviewer, channel="offline", trace_id="b43-b32-investigation",
                          as_of=datetime(2026, 7, 1, 9, tzinfo=timezone.utc))
        if "scenario-knowledge:" not in versions._verifiers:
            versions.register_verifier("scenario-knowledge:", lambda _component: "complete")
        _base_manifest(versions, reviewer)
        store = EvolutionStore(versions.writer, versions.plant_id)
        store.registry = registry
        tools = registry.tools

        created = tools["propose_candidates_evo"].handler(
            context, kind="skill",
            trigger={"event_ticket": "T-B32-INVESTIGATION", "event_type": "compressor_pressure_drop",
                     "note": "既有 B32 Skill 候选", "subject_id": "entity://demo-electronics/air_header/AIR-HDR"})
        assert created.ok is True
        candidate = store.candidate(created.data["candidate_id"])
        assert candidate.kind == CandidateKind.skill
        skill = skill_artifacts(versions.writer, versions.plant_id).load(
            candidate.changes[ManifestComponent.skills])
        assert skill["kind"] == "investigation" and not skill.get("evidence")
        # 旧门（未按格式分流）写下的 rejected 记录没有 format，不把既有 investigation 候选永久卡住
        store.event({"event": "skill_evaluated", "candidate_id": candidate.candidate_id,
                     "verdict": "rejected", "reasons": ["没有样本工单，无法重放验证"],
                     "hit_rate": 0.0, "report_id": None, "run_id": None,
                     "at": datetime.now(timezone.utc).isoformat()})

        # 同一基准上先造含“停机”步骤的 investigation 候选：门跳过重放，但安全校验拒绝
        forbidden = tools["propose_candidates_evo"].handler(
            context, kind="skill",
            trigger={"event_ticket": "T-B32-STOP", "event_type": "compressor_pressure_drop",
                     "note": "含停机步骤", "steps": ["先停机并切断电源", "再检查压力"],
                     "subject_id": "entity://demo-electronics/air_header/AIR-HDR"})
        assert forbidden.ok is True
        with pytest.raises(PlantkgError) as error:
            tools["propose_publish"].handler(context, candidate_id=forbidden.data["candidate_id"],
                                             rationale="含停机步骤的 investigation 候选")
        assert error.value.code == ErrorCode.PRECONDITION and "停机" in error.value.message
        rejected = [event for event in store.timeline()
                    if event["event"] == "skill_evaluated"
                    and event.get("candidate_id") == forbidden.data["candidate_id"]]
        assert len(rejected) == 1 and rejected[0]["verdict"] == "rejected"
        assert rejected[0]["format"] == "investigation"

        action = tools["propose_publish"].handler(context, candidate_id=candidate.candidate_id,
                                                 rationale="既有 B32 Skill 候选发布")
        actions.confirm(action.data["action_id"], reviewer, "复核员确认发布", 0, "b43-b32-investigation-publish")
        assert store.candidate(candidate.candidate_id).status == CandidateStatus.published
        evaluated = [event for event in store.timeline()
                     if event["event"] == "skill_evaluated"
                     and event.get("candidate_id") == candidate.candidate_id]
        assert [event["verdict"] for event in evaluated] == ["rejected", "validated"], \
            "旧门的无 format 记录重判后应落一条 validated"
        assert evaluated[-1]["format"] == "investigation", "investigation 体跳过样本重放，事件标格式"
    finally:
        services.close()


def test_investigation_evaluation_reports_constraint_violations(tmp_path):
    """P2（#242 第 5 轮复审）：investigation 体不重放，但 reasons 与 skill_evaluated 带出违例变化。"""
    env = _environment(tmp_path)
    try:
        env.service.evaluator = lambda _candidate, _scenario, _context: {
            "scenario_id": "investigation-violations", "executed": True, "ledger_verified": True,
            "target_events": 0, "targets_unmeasurable": True,
            "metrics": {"target": {"base": 0.0, "candidate": 0.0}},
            "constraint_violations": {"base": 0, "candidate": 3}}
        body = {"format_version": 1, "kind": "investigation", "name": "investigation-violations",
                "version": 1, "event_type": "compressor_pressure_drop", "title": "诊断文本",
                "steps": ["沿图谱追溯上下游对象与受影响产线"], "source_tickets": [], "source": "test",
                "nature": "synthetic", "skill_md": "# test"}
        candidate = _candidate_with_body(env, body, "b43-investigation-violations")
        evaluated = evaluate_skill_candidate(env.service, env.policy, candidate.candidate_id)
        assert evaluated["report"].verdict == "pass"
        assert evaluated["verdict"] == "validated", "investigation 体不因未重放的违例改判"
        assert any("硬约束违例 0 → 3" in reason for reason in evaluated["reasons"])
        event = [row for row in env.store.timeline()
                 if row["event"] == "skill_evaluated" and row.get("candidate_id") == candidate.candidate_id][-1]
        assert event["constraint_violations"] == {"base": 0, "candidate": 3}
    finally:
        env.writer.close()
