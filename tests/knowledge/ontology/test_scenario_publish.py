import pytest
import yaml
from datetime import timedelta
from pathlib import Path

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Interval, Role, Rule
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.experts.energy.planner import load_scenario_packs, published_tariff_price, with_published_tariff
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
from plantkg.world.scenario.script import Scenario, load_scenario

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("group", ["independent", "coordinated"])
def test_uploaded_document_forks_running_scenario_only_after_confirmation(group):
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = Scenario.model_validate({"scenario_id": "b30-test", "title": "B30 测试", "date": "2026-07-01",
                                            "seed": 7, "initial": {"soc": 0.5}})
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation", group=group)
        old_run = runner.start()
        upload = runtime.hooks_for("scenario.document_upload")[-1]
        upload(runner, runner.now, {"document": "《空压机运行维护手册》"})
        assert runner.run_info.run_id == old_run.run_id
        record = runner.trace.notes[-1]
        cid = record["candidate_ids"][0]
        context = runner._context(old_run.version_manifest_id)
        entity = world.ids()["air_system"]
        suggestion = registry.tools["propose_review_decision"].handler(context, review_id=cid, decision="accept", rationale="复核手册",
                                                                       revision={"subject_id": entity, "effective_from": "2025-11-01T00:00:00Z"})
        reviewer = Actor(actor_id="demo-reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        actions.confirm(suggestion.data["action_id"], reviewer, "确认复核", 0, "b30-scenario-review")
        versions = registry.resolve(VersionStore)
        publish = registry.tools["propose_publish"].handler(context, candidate_id=cid, rationale="发布手册")
        assert publish.ok, publish.to_agent_text()
        runtime.hooks_for("scenario.knowledge_step")[-1](runner, runner.now)
        assert runner.run_info.run_id == old_run.run_id
        actions.confirm(publish.data["action_id"], reviewer, "确认发布", 0, "b30-scenario-publish")
        runtime.hooks_for("scenario.knowledge_step")[-1](runner, runner.now)
        assert runner.run_info.run_id != old_run.run_id
        assert runner.run_info.version_manifest_id == versions.active().version_manifest_id
        assert "compressor_leak_rule" in runner.knowledge
    finally:
        services.close()


def test_reviewed_tariff_changes_forked_run_and_planning_prices(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = Scenario.model_validate({"scenario_id": "b30-tariff", "title": "电价变更测试", "date": "2026-07-01",
                                            "seed": 7, "initial": {"soc": 0.5}})
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        old_run = runner.start()
        ctx = runner._context(old_run.version_manifest_id)
        assets = registry.resolve(AssetRepository)
        known_at = runner.now - timedelta(days=30)
        source = assets.ingest(ctx, str(ROOT / "data/docs/energy/tariff-summary-2025.md"), "policy", known_at, nature="synthetic")
        calendar_file = tmp_path / "calendar.md"
        fixture = yaml.safe_load((ROOT / "fixtures/packs/tariff/jiangsu_2025_426.yaml").read_text(encoding="utf-8"))["rules"][0]
        dates = fixture["periods"][-1]["dates"]
        calendar_file.write_text("模拟节日日历\n春节：" + "、".join(d for d in dates if d.startswith("2026-02"))
                                 + "\n五一：" + "、".join(d for d in dates if d.startswith("2026-05"))
                                 + "\n国庆：" + "、".join(d for d in dates if "-10-" in d)
                                 + "\n登记至：2026-12-31\n", encoding="utf-8")
        calendar = assets.ingest(ctx, str(calendar_file), "policy", known_at, nature="synthetic")
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"asset_version_id": source.asset_version_id})
        cid = runner.trace.notes[-1]["candidate_ids"][0]
        prepared = registry.tools["prepare_tariff_review"].handler(
            ctx, candidate_id=cid, calendar_asset_version=calendar.asset_version_id, subject_id=fixture["grid_entity"],
            category=fixture["category"], effective_from="2025-06-01T00:00:00Z")
        revision = prepared.data["revision"] | {"flat_factor": 0.98}  # synthetic：制造可见价格变化；下调不触发 5% 成本退化
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        decision = registry.tools["propose_review_decision"].handler(ctx, review_id=cid, decision="revise",
                                                                      revision=revision, rationale="模拟复核电价")
        actions.confirm(decision.data["action_id"], reviewer, "模拟复核", 0, "b30-tariff-review")
        proposal = registry.tools["propose_publish"].handler(ctx, candidate_id=cid, rationale="发布模拟电价")
        assert proposal.ok, proposal.to_agent_text()
        runner.step()
        old_price = runner.trace.ledger[-1].entry.price_cny_per_kwh
        old_snapshot = runner.snapshot()
        assert runner.run_info.run_id == old_run.run_id
        actions.confirm(proposal.data["action_id"], reviewer, "模拟确认", 0, "b30-tariff-publish")
        runner.step()
        assert runner.run_info.run_id != old_run.run_id
        versions = registry.resolve(VersionStore)
        active = versions.active()
        rule = Rule.model_validate(RuleArtifacts(versions.writer, versions.plant_id).load(active.components["rules"])["rules"][0])
        slot_start = runner.now.replace(minute=runner.now.minute - runner.now.minute % 15, second=0, microsecond=0)
        slot = Interval(start=slot_start, end=slot_start + timedelta(minutes=15))
        expected = published_tariff_price(scenario, [rule], slot)
        assert runner.trace.ledger[-1].entry.price_cny_per_kwh == pytest.approx(expected)
        assert expected != pytest.approx(old_price)
        new_snapshot = runner.snapshot()
        frozen_at = runner.now
        assert world.frozen_inputs[new_snapshot.snapshot_id][frozen_at].price_cny_per_kwh == pytest.approx(expected)
        assert world.frozen_inputs[old_snapshot.snapshot_id][frozen_at].price_cny_per_kwh == pytest.approx(old_price)
        packs = with_published_tariff(load_scenario_packs(scenario), [rule], slot)
        assert rule in packs.rules and all(r.rule_id == rule.rule_id for r in packs.rules if r.kind == "tariff_tou")
    finally:
        services.close()


def test_each_rule_from_one_upload_triggers_its_own_confirmed_fork(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = Scenario.model_validate({"scenario_id": "b30-two-rules", "title": "多规则资料测试", "date": "2026-07-01",
                                            "seed": 7, "initial": {"soc": 0.5}})
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        first_run = runner.start()
        context = runner._context(first_run.version_manifest_id)
        document = tmp_path / "combined.md"
        document.write_text("办公室温度24±1 ℃。\n主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时按泄漏处理。", encoding="utf-8")
        asset = registry.resolve(AssetRepository).ingest(context, str(document), "process_doc", runner.now, nature="synthetic")
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"asset_version_id": asset.asset_version_id})
        candidate_ids = runner.trace.notes[-1]["candidate_ids"]
        assert len(candidate_ids) == 2
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        subjects = [world.ids()["air_system"], f"entity://{services.settings.plant_id}/zone/admin"]
        for index, (candidate_id, subject) in enumerate(zip(candidate_ids, subjects)):
            context = runner._context(runner.run_info.version_manifest_id)
            proposal = registry.tools["propose_review_decision"].handler(
                context, review_id=candidate_id, decision="accept", rationale="模拟复核",
                revision={"subject_id": subject, "effective_from": runner.now.isoformat()})
            actions.confirm(proposal.data["action_id"], reviewer, "模拟确认", 0, f"multi-review-{index}")
            publish_action = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="模拟发布")
            assert publish_action.ok, publish_action.to_agent_text()
            before = runner.run_info.run_id
            actions.confirm(publish_action.data["action_id"], reviewer, "模拟确认", 0, f"multi-publish-{index}")
            runtime.hooks_for("scenario.knowledge_step")[-1](runner, runner.now)
            assert runner.run_info.run_id != before
        assert len(runner.trace.runs) == 3
    finally:
        services.close()


def test_published_leak_rule_does_not_suppress_severe_alarm_actions():
    """发布泄漏规则后，第二幕真实告警仍须给出隔离与维修动作（评审 #49 回归）。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        first_run = runner.start()
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"document": "《空压机运行维护手册》"})
        cid = runner.trace.notes[-1]["candidate_ids"][0]
        context = runner._context(first_run.version_manifest_id)
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        decision = registry.tools["propose_review_decision"].handler(
            context, review_id=cid, decision="accept", rationale="复核手册",
            revision={"subject_id": world.ids()["air_system"], "effective_from": "2025-11-01T00:00:00Z"})
        actions.confirm(decision.data["action_id"], reviewer, "确认复核", 0, "b30-leak-review")
        publish = registry.tools["propose_publish"].handler(context, candidate_id=cid, rationale="发布手册")
        assert publish.ok, publish.to_agent_text()
        actions.confirm(publish.data["action_id"], reviewer, "确认发布", 0, "b30-leak-publish")
        runtime.hooks_for("scenario.knowledge_step")[-1](runner, runner.now)
        trace = runner.run_until(scenario.instant("10:41"))
        proposals = [p for p in trace.proposals
                     if p.expert == "equipment" and p.binding.trigger_at == scenario.instant("10:30")]
        assert proposals, "第二幕泄漏没有产生设备专家提案"
        proposal = proposals[0]
        commands = [action.command for action in proposal.actions]
        assert "isolate_leak" in commands and "repair" in commands
        assert "手册规定窗口内压降未超过已发布阈值" in (proposal.diagnosis or "")
    finally:
        services.close()


def test_publish_without_evolution_pack_uses_b30_fallback():
    """禁用 evolution 时 B30 本体发布仍走兜底发布器（PR #120 复审 6：UnboundLocalError）。"""
    packs = [name for name in load_settings(profile="mock").packs if name != "evolution"]
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True, "packs": packs}))
    try:
        registry = services.registry
        scenario = Scenario.model_validate({"scenario_id": "b30-no-evolution", "title": "禁用进化", "date": "2026-07-01",
                                            "seed": 7, "initial": {"soc": 0.5}})
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        runner.start()
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"document": "《空压机运行维护手册》"})
        candidate_id = runner.trace.notes[-1]["candidate_ids"][0]
        context = runner._context(runner.run_info.version_manifest_id)
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        decision = registry.tools["propose_review_decision"].handler(
            context, review_id=candidate_id, decision="accept", rationale="复核手册",
            revision={"subject_id": world.ids()["air_system"], "effective_from": "2025-11-01T00:00:00Z"})
        actions.confirm(decision.data["action_id"], reviewer, "确认复核", 0, "b30-no-evolution-review")
        publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="发布手册")
        assert publish.ok, publish.to_agent_text()
        assert publish.data["status"] == "proposed"
        assert actions.get(publish.data["action_id"]).payload["fallback_no_backtest"] is True
    finally:
        services.close()


def test_manual_rule_publish_passes_real_fast_backtest():
    """P1-2：手册规则发布经默认装配的真 evaluator；读数可疑走慢通道的目标事件按已发布规则覆盖计入目标指标。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        first_run = runner.start()
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"document": "《空压机运行维护手册》"})
        candidate_id = runner.trace.notes[-1]["candidate_ids"][0]
        context = runner._context(first_run.version_manifest_id)
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        decision = registry.tools["propose_review_decision"].handler(
            context, review_id=candidate_id, decision="accept", rationale="复核手册",
            revision={"subject_id": world.ids()["air_system"], "effective_from": "2025-11-01T00:00:00Z"})
        actions.confirm(decision.data["action_id"], reviewer, "确认复核", 0, "b30-real-review")
        publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="发布手册")
        assert publish.ok, publish.to_agent_text()
        action = actions.get(publish.data["action_id"])
        report = action.payload["backtest_report"]
        assert report["verdict"] == "pass", report["failures"]
        assert report["scope"] == "fast_window"
        assert report["metrics"]["coverage"]["baseline_reused"] == 1.0  # 上传时已预热基准
        assert report["metrics"]["candidate"]["target"] >= 1.0
        assert report["metrics"]["base"]["target"] == 0.0
    finally:
        services.close()


def test_publish_fork_retry_after_interruption_does_not_duplicate():
    """P2-8（#206 第 5 条）：分叉后中断，重试发布不重复 fork。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        scenario = Scenario.model_validate({"scenario_id": "b32-fork-retry", "title": "分叉重试测试",
                                            "date": "2026-07-01", "seed": 7, "initial": {"soc": 0.5}})
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, db, registry=runtime, mode="evaluation")
        runner.start()
        calls = {"fail": True}

        def flaky(_run_id, _manifest_id, _candidate):
            if calls["fail"]:
                raise RuntimeError("模拟分叉后进程中断")

        registry.register_hook("evolution.fork_published", flaky)
        runtime.hooks_for("scenario.document_upload")[-1](runner, runner.now, {"document": "《空压机运行维护手册》"})
        candidate_id = runner.trace.notes[-1]["candidate_ids"][0]
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={"run_id": runner.run_info.run_id})
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        actions = registry.resolve(PendingActionStore)
        decision = registry.tools["propose_review_decision"].handler(
            context, review_id=candidate_id, decision="accept", rationale="复核手册",
            revision={"subject_id": world.ids()["air_system"], "effective_from": "2025-11-01T00:00:00Z"})
        actions.confirm(decision.data["action_id"], reviewer, "确认复核", 0, "b32-fork-review")
        publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="发布手册")
        assert publish.ok, publish.to_agent_text()
        action_id = publish.data["action_id"]
        with pytest.raises(RuntimeError):
            actions.confirm(action_id, reviewer, "确认发布", actions.get(action_id).revision, "b32-fork-retry")
        assert len(runner.trace.runs) == 2, "中断发生在分叉之后：应已有一次 fork"
        calls["fail"] = False
        actions.confirm(action_id, reviewer, "确认发布", actions.get(action_id).revision, "b32-fork-retry")
        assert len(runner.trace.runs) == 2, "重试不得重复 fork"
    finally:
        services.close()
