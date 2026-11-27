from datetime import datetime, timezone

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import Actor, ComponentRef, Context, ErrorCode, Locator, PlantkgError, Role, Rule
from plantkg.core.contracts import Candidate, CandidateKind, CandidateStatus, ManifestComponent, PolicyConfig, ResponseBudget, ToolResult
from plantkg.core.ports import AgentResultSource, PendingActionStore, VersionStore
from plantkg.core.registry import Registry
from plantkg.core.versions import SqliteVersionStore
from plantkg.evolution import EvolutionService
from plantkg.evolution.artifacts import policy_artifacts, skill_artifacts
from plantkg.evolution.service import PublishHandler, RollbackHandler
from plantkg.evolution.store import EvolutionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


def test_correction_backtest_publish_and_rollback_are_auditable(tmp_path):
    writer = SQLiteWriter(tmp_path / "evolution.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        base = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        artifacts = RuleArtifacts(writer, "factory")
        versions.register_verifier("b30-rules:", artifacts.verify)
        rule = Rule(rule_id="corrected", source_asset_version="asset://factory/12345678/manual/1",
                    locator=Locator(line_start=3), kind="compressor_pressure_drop",
                    subject_ids=("entity://factory/air_header/AIR-HDR",), quantity="pressure_drop_rate",
                    unit="bar/h", value=-2.0, nature="synthetic", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), review_status="approved",
                    reviewer="reviewer", compiler_version="b30-1")
        ref = artifacts.prepare([rule], reviewer)
        registry = Registry()
        registry.register_hook("knowledge.rules.consumer_mismatch", lambda _rules: None)  # B30 消费端校验桩
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)

        class PoisonSource:
            def get(self, *_args, **_kwargs):
                raise AssertionError("回测不得调用模型入口")

        for kind in ("expert", "investigate", "arbitration"):
            registry.register_mode(AgentResultSource, "deterministic", PoisonSource(), key=kind)
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=operator, channel="offline",
                          trace_id="b32-smoke", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, actions, artifacts, registry, context,
                                   evaluator=lambda *_: {"scenario_id": "synthetic-leak", "baseline_score": 0.0,
                                                         "candidate_score": 1.0, "executed": True, "ledger_verified": True,
                                                         "deterministic_fallbacks": 3,
                                                         "metrics": {"target": {"base": 0.0, "candidate": 1.0},
                                                                     "cost_cny": {"base": 100.0, "candidate": 100.0}}})
        registry.register_action_handler("publish", PublishHandler(versions, artifacts, store, service))
        registry.register_action_handler("rollback", RollbackHandler(versions, store))
        candidate = service.record_correction("ticket-1", {"note": "pressure threshold corrected",
                                     "source_asset": "asset://factory/12345678/manual/1",
                                     "changes": {"rules": ref.model_dump(mode="json")}})
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "synthetic-leak", "seed": 32}])
        assert report.verdict == "pass" and report.metrics["coverage"]["deterministic_fallbacks"] > 0
        assert versions.active().version_manifest_id == base.version_manifest_id
        publish = service.propose_publish(store.candidate(candidate.candidate_id))
        actions.confirm(publish.action_id, reviewer, "回测通过后发布", 0, "publish-correction")
        published = versions.active()
        assert published.version_manifest_id != base.version_manifest_id
        assert versions.knowledge_version(published.version_manifest_id) == 2
        assert any(row["event"] == "published" and row["report_id"] == report.report_id for row in store.timeline())
        registry.action_handlers["publish"].apply(actions.get(publish.action_id), reviewer)
        assert len([row for row in store.timeline() if row["event"] == "published"]) == 1
        rollback = service.propose_rollback(base.version_manifest_id, "回归观察发现效果变差")
        actions.confirm(rollback.action_id, reviewer, "恢复旧知识版本", 0, "rollback-correction")
        assert versions.active().version_manifest_id == base.version_manifest_id
        registry.action_handlers["rollback"].apply(actions.get(rollback.action_id), reviewer)
        assert any(row["event"] == "rollback" for row in store.timeline())

        legacy_candidate = Candidate(candidate_id="b30-legacy", kind=CandidateKind.knowledge,
                                     base_manifest_id=base.version_manifest_id,
                                     changes={ManifestComponent.rules: ref}, status=CandidateStatus.proposed,
                                     trigger="人工复核资料规则", created_at=datetime.now(timezone.utc),
                                     validation_policy_version="b30-fallback-no-backtest")
        legacy_action = actions.propose("publish", {"target_ref": legacy_candidate.candidate_id,
                                     "candidate": legacy_candidate.model_dump(mode="json"),
                                     "fallback_no_backtest": True}, context)
        actions.confirm(legacy_action.action_id, reviewer, "旧动作由 B32 回测后发布", 0, "legacy-publish")
        assert store.candidate(legacy_candidate.candidate_id).status == CandidateStatus.published
        assert store.candidate(legacy_candidate.candidate_id).backtest_report_id is not None
    finally:
        writer.close()


def test_regression_candidate_is_rejected_and_cannot_be_published(tmp_path):
    writer = SQLiteWriter(tmp_path / "regression.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        base = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        artifacts = RuleArtifacts(writer, "factory")
        rule = Rule(rule_id="regression", source_asset_version="asset://factory/12345678/manual/1",
                    locator=Locator(line_start=4), kind="compressor_pressure_drop",
                    subject_ids=("entity://factory/air_header/AIR-HDR",), quantity="pressure_drop_rate",
                    unit="bar/h", value=-2.0, nature="synthetic", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), review_status="approved",
                    reviewer="reviewer", compiler_version="b30-1")
        ref = artifacts.prepare([rule], reviewer)
        registry = Registry()
        registry.register_hook("knowledge.rules.consumer_mismatch", lambda _rules: None)  # B30 消费端校验桩
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=operator, channel="offline",
                          trace_id="b32-regression", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, actions, artifacts, registry, context,
                                   evaluator=lambda *_: {"baseline_score": 1.0, "candidate_score": 0.5,
                                                         "executed": True, "ledger_verified": True})
        candidate = service.record_correction("ticket-regression", {"changes": {"rules": ref.model_dump(mode="json")}})
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "synthetic-regression", "seed": 32}])
        assert report.verdict == "fail"
        with pytest.raises(PlantkgError, match="须先通过回测"):
            service.propose_publish(store.candidate(candidate.candidate_id))
        assert versions.active().version_manifest_id == base.version_manifest_id
    finally:
        writer.close()


def test_backtest_without_an_isolated_evaluator_is_inconclusive(tmp_path):
    writer = SQLiteWriter(tmp_path / "inconclusive.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        base = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        registry = Registry()
        registry.register_hook("knowledge.rules.consumer_mismatch", lambda _rules: None)  # B30 消费端校验桩
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
        artifacts = RuleArtifacts(writer, "factory")
        store = EvolutionStore(writer, "factory")
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=operator, channel="offline",
                          trace_id="b32-inconclusive", as_of=datetime.now(timezone.utc))
        service = EvolutionService(store, versions, actions, artifacts, registry, context)
        candidate = Candidate(candidate_id="candidate-unmeasured", kind=CandidateKind.knowledge,
                              base_manifest_id=base.version_manifest_id,
                              changes={ManifestComponent.rules: ComponentRef(ref="rules:unmeasured", content_hash="sha256:" + "1" * 64)},
                              status=CandidateStatus.proposed, trigger="correction", created_at=datetime.now(timezone.utc),
                              validation_policy_version="b32-v1")
        store.put_candidate(candidate)
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "not-run", "seed": 32}])
        assert report.verdict == "inconclusive"
        assert store.candidate(candidate.candidate_id).status == CandidateStatus.proposed
        with pytest.raises(PlantkgError, match="须先通过回测"):
            service.propose_publish(store.candidate(candidate.candidate_id))
    finally:
        writer.close()


def test_correction_rule_generates_candidate_without_component_reference(tmp_path):
    writer = SQLiteWriter(tmp_path / "correction-rule.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("scenario-knowledge:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        base = versions.prepare({"rules": ComponentRef(ref="scenario-knowledge:empty", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        registry = Registry()
        registry.register_hook("knowledge.rules.consumer_mismatch", lambda _rules: None)  # B30 消费端校验桩
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
        artifacts = RuleArtifacts(writer, "factory")
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=reviewer, channel="offline",
                          trace_id="b32-correction-rule", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, actions, artifacts, registry, context)
        rule = Rule(rule_id="corrected-pressure", source_asset_version="asset://factory/12345678/manual/1",
                    locator=Locator(line_start=7), kind="compressor_pressure_drop",
                    subject_ids=("entity://factory/air_header/AIR-HDR",), quantity="pressure_drop_rate",
                    unit="bar/h", value=-1.5, nature="synthetic", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), review_status="approved",
                    reviewer=reviewer.actor_id, compiler_version="b30-1")
        candidate = service.record_correction("ticket-rule", {"note": "修正压力阈值", "rule": rule.model_dump(mode="json")})
        assert candidate.changes[ManifestComponent.rules].ref.startswith("b30-rules:")
        assert store.candidate(candidate.candidate_id).trigger.endswith("修正压力阈值")
    finally:
        writer.close()


def test_existing_candidate_id_with_different_content_is_rejected(tmp_path):
    writer = SQLiteWriter(tmp_path / "candidate-id.db")
    try:
        MigrationRunner(writer).apply()
        store = EvolutionStore(writer, "factory")
        common = {"candidate_id": "same-id", "kind": "knowledge", "base_manifest_id": "base",
                  "status": "proposed", "trigger": "correction", "created_at": datetime.now(timezone.utc),
                  "validation_policy_version": "b32-v1"}
        first = Candidate(**common, changes={ManifestComponent.rules: ComponentRef(ref="rules:old", content_hash="sha256:" + "0" * 64)})
        changed = Candidate(**common, changes={ManifestComponent.policy: ComponentRef(ref="policy:new", content_hash="sha256:" + "1" * 64)})
        store.put_candidate(first)
        with pytest.raises(PlantkgError, match="已用于其他内容"):
            store.put_candidate(changed)
    finally:
        writer.close()


def test_registered_evolution_tools_return_tool_results(tmp_path):
    writer = SQLiteWriter(tmp_path / "tools.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        initial = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(initial.version_manifest_id)
        versions.activate(None, initial.version_manifest_id, reviewer, "baseline")
        actions = SqlitePendingActionStore(writer, "factory", {})
        registry = Registry()
        registry.register(VersionStore, versions)
        registry.register(PendingActionStore, actions)
        from plantkg.evolution.service import register
        from types import SimpleNamespace
        register(registry, SimpleNamespace(industry_pack="manufacturing"))
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=Actor(actor_id="operator", roles=(Role.operator,)),
                          channel="offline", trace_id="b32-tools", as_of=datetime.now(timezone.utc))
        result = registry.tools["propose_candidates_evo"].handler(context, kind="knowledge", trigger={"event_ticket": "ticket-1"})
        assert isinstance(result, ToolResult) and not result.ok
        assert registry.tools["backtest"].spec.mode == "job"
        result = registry.tools["backtest"].handler(context, candidate_id="missing")
        assert isinstance(result, ToolResult) and not result.ok
        result = registry.tools["propose_rollback"].handler(context, target_manifest_id="missing", rationale="test")
        assert isinstance(result, ToolResult) and not result.ok
        page = registry.rest[("GET", "/evolution/timeline")](context, {}, {})
        assert page.as_of == context.as_of and isinstance(page.items, tuple)
        assert any(item.event == "manifest_activated" for item in page.items)
    finally:
        writer.close()


def test_one_sentence_correction_generates_rule_skill_and_autonomy_candidate(tmp_path):
    writer = SQLiteWriter(tmp_path / "sentence.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("scenario-knowledge:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        base = versions.prepare({"rules": ComponentRef(ref="scenario-knowledge:empty", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        artifacts = RuleArtifacts(writer, "factory")
        versions.register_verifier("b30-rules:", artifacts.verify)
        for artifact_store in (policy_artifacts(writer, "factory"), skill_artifacts(writer, "factory")):
            versions.register_verifier(artifact_store.prefix, artifact_store.verify)
        registry = Registry()
        registry.register_hook("knowledge.rules.consumer_mismatch", lambda _rules: None)  # B30 消费端校验桩
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=reviewer, channel="offline",
                          trace_id="b32-sentence", as_of=datetime(2026, 6, 30, 10, tzinfo=timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, actions, artifacts, registry, context,
                                   evaluator=lambda *_: {"scenario_id": "synthetic-sentence", "executed": True,
                                                         "ledger_verified": True, "deterministic_fallbacks": 0,
                                                         "metrics": {"target": {"base": 0.0, "candidate": 1.0},
                                                                     "cost_cny": {"base": 10.0, "candidate": 10.0}}})
        registry.register_action_handler("publish", PublishHandler(versions, artifacts, store, service))
        registry.register_action_handler("rollback", RollbackHandler(versions, store))
        candidate = service.record_correction("ticket-combo", {
            "note": "照明与空压同时异常是总配电柜接触不良", "event_type": "unknown_combo",
            "entity_id": "entity://factory/air_compressor/AC-2",
            "source_asset_version": "asset://factory/12345678/manual/1", "line_start": 5,
            "autonomy_level": "L2"})
        assert set(candidate.changes) == {ManifestComponent.rules, ManifestComponent.skills, ManifestComponent.policy}
        rule = Rule.model_validate(artifacts.load(candidate.changes[ManifestComponent.rules])["rules"][0])
        assert (rule.kind, rule.review_status, rule.reviewer) == ("unknown_combo", "approved", "reviewer")
        skill = skill_artifacts(writer, "factory").load(candidate.changes[ManifestComponent.skills])
        assert skill["event_type"] == "unknown_combo" and len(skill["steps"]) >= 3
        policy = PolicyConfig.model_validate(policy_artifacts(writer, "factory").load(
            candidate.changes[ManifestComponent.policy]))
        assert policy.autonomy_levels == {"unknown_combo": "L2"}
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "synthetic-sentence", "seed": 7}])
        assert report.verdict == "pass"
        action = service.propose_publish(store.candidate(candidate.candidate_id))
        actions.confirm(action.action_id, reviewer, "确认发布", 0, "publish-sentence")
        published = versions.active()
        assert set(published.components) >= {"rules", "skills", "policy"}
        assert versions.knowledge_version(published.version_manifest_id) == 2
        events = [row for row in store.timeline() if row["event"] == "published"]
        assert events and events[-1]["comparison"]["candidate"]["target"] == 1.0
    finally:
        writer.close()


def test_per_scenario_regression_blocks_publish(tmp_path):
    writer = SQLiteWriter(tmp_path / "scenario-regression.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))

        def evaluator(_candidate, scenario, _context):
            improved = scenario["scenario_id"] == "scenario-a"
            return {"scenario_id": scenario["scenario_id"], "executed": True, "ledger_verified": True,
                    "metrics": {"target": {"base": 0.0, "candidate": 10.0 if improved else 5.0},
                                "cost_cny": {"base": 100.0, "candidate": 100.0 if improved else 200.0}}}

        context = Context(plant_id="factory", industry_pack="manufacturing",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), channel="offline",
                          trace_id="b32-per-scenario", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, SqlitePendingActionStore(writer, "factory", {}), None,
                                   Registry(), context, evaluator=evaluator)
        backup = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(backup.version_manifest_id)
        versions.activate(None, backup.version_manifest_id, reviewer, "baseline")
        candidate = service.record_correction("ticket-regression", {
            "changes": {"rules": ComponentRef(ref="rules:x", content_hash="sha256:" + "1" * 64).model_dump(mode="json")}})
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "scenario-a", "seed": 1},
                                                           {"scenario_id": "scenario-b", "seed": 1}])
        assert report.verdict == "fail"
        assert report.metrics["scenario-b"]["candidate_cost_cny"] == 200.0
        assert any("scenario-b.cost_cny" in failure for failure in report.failures)
        assert store.candidate(candidate.candidate_id).status == CandidateStatus.rejected
        with pytest.raises(PlantkgError, match="须先通过回测"):
            service.propose_publish(store.candidate(candidate.candidate_id))
    finally:
        writer.close()


def test_cost_only_improvement_is_not_a_passing_backtest(tmp_path):
    writer = SQLiteWriter(tmp_path / "cost-only.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        base = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        context = Context(plant_id="factory", industry_pack="manufacturing",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), channel="offline",
                          trace_id="b32-cost-only", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, SqlitePendingActionStore(writer, "factory", {}), None, Registry(), context,
                                   evaluator=lambda *_: {"scenario_id": "cost-only", "executed": True, "ledger_verified": True,
                                                         "metrics": {"cost_cny": {"base": 100.0, "candidate": 50.0}}})
        candidate = service.record_correction("ticket-cost", {
            "changes": {"rules": ComponentRef(ref="rules:x", content_hash="sha256:" + "1" * 64).model_dump(mode="json")}})
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "cost-only", "seed": 1}])
        assert report.verdict == "inconclusive"
        assert store.candidate(candidate.candidate_id).status == CandidateStatus.proposed
        with pytest.raises(PlantkgError, match="须先通过回测"):
            service.propose_publish(store.candidate(candidate.candidate_id))
    finally:
        writer.close()


def test_autonomy_direction_lowers_level_against_base_policy(tmp_path):
    writer = SQLiteWriter(tmp_path / "autonomy-down.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("scenario-knowledge:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        policy_store = policy_artifacts(writer, "factory")
        versions.register_verifier("policy:", policy_store.verify)
        base_policy = PolicyConfig(policy_version="base-policy", arbitration_weights={"cny": 1.0}, priority_profiles={},
                                   response_budget=ResponseBudget(layer12_min=1, layer3_min=3),
                                   autonomy_levels={"unknown_combo": "L2"})
        policy_ref = policy_store.prepare(base_policy.model_dump(mode="json"), reviewer)
        base = versions.prepare({"rules": ComponentRef(ref="scenario-knowledge:empty", content_hash="sha256:" + "0" * 64),
                                 "policy": policy_ref.model_dump(mode="json")}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        artifacts = RuleArtifacts(writer, "factory")
        versions.register_verifier("b30-rules:", artifacts.verify)
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=reviewer, channel="offline",
                          trace_id="b32-autonomy-down", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, SqlitePendingActionStore(writer, "factory", {}), artifacts,
                                   Registry(), context)
        candidate = service.record_correction("ticket-down", {
            "note": "同类事件连续处理错误，降级", "event_type": "unknown_combo", "autonomy_direction": "down"})
        policy = PolicyConfig.model_validate(policy_artifacts(writer, "factory").load(
            candidate.changes[ManifestComponent.policy]))
        assert policy.autonomy_levels["unknown_combo"] == "L1"
    finally:
        writer.close()


def test_rule_publish_fails_closed_without_consumer_validator(tmp_path):
    """P2-4：有规则候选却缺消费端校验钩子时，发布失败关闭，不把未校验规则放出去。"""
    writer = SQLiteWriter(tmp_path / "no-validator.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("scenario-knowledge:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        base = versions.prepare({"rules": ComponentRef(ref="scenario-knowledge:empty", content_hash="sha256:" + "0" * 64)}, "baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "baseline")
        artifacts = RuleArtifacts(writer, "factory")
        versions.register_verifier("b30-rules:", artifacts.verify)
        registry = Registry()
        actions = SqlitePendingActionStore(writer, "factory", registry.action_handlers)
        context = Context(plant_id="factory", industry_pack="manufacturing", actor=reviewer, channel="offline",
                          trace_id="b32-no-validator", as_of=datetime.now(timezone.utc))
        store = EvolutionStore(writer, "factory")
        service = EvolutionService(store, versions, actions, artifacts, registry, context,
                                   evaluator=lambda *_: {"scenario_id": "no-validator", "executed": True,
                                                         "ledger_verified": True,
                                                         "metrics": {"target": {"base": 0.0, "candidate": 1.0}}})
        rule = Rule(rule_id="closed-rule", kind="compressor_pressure_drop",
                    subject_ids=("entity://factory/air_header/AIR-HDR",), quantity="pressure_drop_rate", unit="bar/h",
                    value=-1.0, config_ref="config://closed-rule", nature="synthetic", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), review_status="approved",
                    reviewer="reviewer", compiler_version="b30-1")
        candidate = service.record_correction("ticket-closed", {"rule": rule.model_dump(mode="json")})
        report = service.backtest(candidate.candidate_id, [{"scenario_id": "no-validator", "seed": 1}])
        assert report.verdict == "pass"
        with pytest.raises(PlantkgError) as error:
            service.propose_publish(store.candidate(candidate.candidate_id))
        assert error.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and "消费端校验" in error.value.message
    finally:
        writer.close()


def test_version_store_has_verifier_public_api(tmp_path):
    """C.1（#242）：校验函数登记可经公开方法查询，调用方不读 VersionStore 内部字段。"""
    writer = SQLiteWriter(tmp_path / "versions-api.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        assert versions.has_verifier("t:") is False
        versions.register_verifier("t:", lambda _: "complete")
        assert versions.has_verifier("t:") is True
        assert versions.has_verifier("other:") is False
    finally:
        writer.close()

