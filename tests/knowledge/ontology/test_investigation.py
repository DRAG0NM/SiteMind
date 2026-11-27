import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, FrozenSplit, Interval, PlantkgError, Role, RunMode
from plantkg.core.ports import AgentRunReader, AssetContentReader, AssetRepository, Gateway, JobRunner, VersionStore
from plantkg.knowledge.rules.investigation import _parse
from plantkg.knowledge.rules.compile import approve_rule
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.store import CandidateStore


def test_exception_investigation_runs_as_job_and_stays_unpublished(tmp_path):
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry = services.registry
        at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        ctx = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-job", as_of=at)
        document = tmp_path / "manual.md"
        document.write_text("主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时，按泄漏处理。", encoding="utf-8")
        asset = registry.resolve(AssetRepository).ingest(ctx, str(document), "process_doc", at, nature="synthetic")
        standard_path = tmp_path / "standard.md"
        standard_path.write_text("模拟标准原文：应检查管路泄漏并进行维护。", encoding="utf-8")
        standard = registry.resolve(AssetRepository).ingest(ctx, str(standard_path), "standard", at, nature="synthetic")
        answer = {"decision": "add_node", "rationale": "核对标准", "citations": [asset.asset_version_id, standard.asset_version_id],
                  "standard_ref": standard.asset_version_id, "standard_excerpt": "应检查管路泄漏并进行维护"}
        assert _parse(json.dumps(answer), asset.asset_version_id, ctx, registry.resolve(AssetRepository),
                      registry.resolve(AssetContentReader))["decision"] == "add_node"
        answer["standard_excerpt"] = "凭空捏造的标准内容"
        with pytest.raises(PlantkgError):
            _parse(json.dumps(answer), asset.asset_version_id, ctx, registry.resolve(AssetRepository), registry.resolve(AssetContentReader))
        extracted = registry.tools["extract_rules"].handler(ctx, asset_version_id=asset.asset_version_id)
        candidate = extracted.data["candidate_ids"][0]
        assert extracted.data["routing"][0]["stage"] == "agent"

        class FakeGateway:
            async def run(self, actor, query, agent_name, as_of=None, **options):
                assert candidate in query and actor == ctx.actor and as_of == at
                yield SimpleNamespace(agent_run_id="agent-test")

        class FakeReader:
            def get(self, agent_run_id):
                return SimpleNamespace(status="finished", final_answer=json.dumps(
                    {"decision": "pending", "rationale": "等待标准核对", "citations": [asset.asset_version_id]}))

        registry.ports[Gateway] = FakeGateway()
        registry.ports[AgentRunReader] = FakeReader()
        result = registry.tools["investigate_onboarding"].handler(ctx, candidate_id=candidate)
        job = registry.resolve(JobRunner).get(result.job_id, wait_seconds=10)
        assert job.status == "succeeded", job.error
        items = registry.tools["onboarding_investigations"].handler(ctx, candidate_id=candidate).data["items"]
        assert items[0]["decision"] == "pending" and items[0]["agent_run_id"] == "agent-test"
        with pytest.raises(PlantkgError):
            registry.tools["propose_publish"].handler(ctx, candidate_id=candidate, rationale="未复核")

        versions = registry.resolve(VersionStore)
        versions.register_verifier("test-score:", lambda component: "complete")
        manifest = versions.prepare({"rules": {"ref": "test-score:rules", "content_hash": "sha256:" + "0" * 64}}, "test")
        versions.check_integrity(manifest.version_manifest_id)
        versions.activate(None, manifest.version_manifest_id, Actor(actor_id="reviewer", roles=(Role.reviewer,)), "test")
        score_ctx = ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id})
        split = FrozenSplit(split_id="scoring", purpose="scoring", days=("2026-07-01",),
                            frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content_hash="sha256:" + "0" * 64)
        fallback_job = registry.tools["score_review_candidate"].handler(score_ctx, candidate_id=candidate, alternative={"value": 1})
        assert registry.resolve(JobRunner).get(fallback_job.job_id, wait_seconds=10).status == "succeeded"
        assert registry.tools["next_review_batch"].handler(score_ctx).data["items"][0]["score"]["status"] == "uncalibrated"
        registry.register_hook("knowledge.counterfactual.inputs", lambda context, item, alt: {
            "split": split, "probability_b": 0.2, "calibration_version": "synthetic-calibration", "mip_rel_gap": 0.01,
            "day_inputs": lambda day, cutoff: {"context": context.model_copy(update={"as_of": cutoff}),
                "horizon": Interval(start=datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
                                    end=datetime(2026, 7, 1, 16, tzinfo=timezone.utc)),
                "solver_budget": SimpleNamespace(mip_rel_gap=0.01)}})
        registry.register_hook("knowledge.counterfactual.tariff", lambda **kwargs: {
            "status_a": "optimal", "status_b": "optimal", "cost_a_under_b": 120, "cost_b_under_b": 100,
            "objective_b": 100, "violates_b": False})
        score_job = registry.tools["score_review_candidate"].handler(score_ctx, candidate_id=candidate, alternative={"value": 1})
        scored = registry.resolve(JobRunner).get(score_job.job_id, wait_seconds=10)
        assert scored.status == "succeeded", scored.error
        review = registry.tools["next_review_batch"].handler(score_ctx).data["items"]
        assert review[0]["score"]["value"] == 4
        assert registry.tools["next_review_batch"].handler(ctx).data["items"][0]["score"]["status"] == "uncalibrated"
        def infeasible(**kwargs):
            raise PlantkgError(ErrorCode.INFEASIBLE, "测试不可解")
        registry.register_hook("knowledge.counterfactual.tariff", infeasible)
        failed_comparison = registry.tools["score_review_candidate"].handler(score_ctx, candidate_id=candidate, alternative={"value": 2})
        assert registry.resolve(JobRunner).get(failed_comparison.job_id, wait_seconds=10).status == "succeeded"
        assert registry.tools["next_review_batch"].handler(score_ctx).data["items"][0]["score"]["status"] == "incomparable"
    finally:
        services.close()


def test_evaluation_mode_investigation_skips_live_gateway(tmp_path):
    """评测模式即使装了 Gateway 也不真实调 Nexent，结果来自离线替身（#213）。"""
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry = services.registry
        at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        ctx = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-eval", as_of=at,
                      run_mode=RunMode.evaluation)
        document = tmp_path / "manual.md"
        document.write_text("主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时，按泄漏处理。", encoding="utf-8")
        asset = registry.resolve(AssetRepository).ingest(ctx, str(document), "process_doc", at, nature="synthetic")
        candidate = registry.tools["extract_rules"].handler(ctx, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]

        calls = []

        class FakeGateway:
            async def run(self, actor, query, agent_name, as_of=None, **options):
                calls.append(query)
                yield SimpleNamespace(agent_run_id="agent-live")

        class FakeReader:
            def get(self, agent_run_id):
                return SimpleNamespace(status="finished", final_answer="{}")

        registry.ports[Gateway] = FakeGateway()
        registry.ports[AgentRunReader] = FakeReader()
        result = registry.tools["investigate_onboarding"].handler(ctx, candidate_id=candidate)
        job = registry.resolve(JobRunner).get(result.job_id, wait_seconds=10)
        assert job.status == "succeeded", job.error
        item = registry.tools["onboarding_investigations"].handler(ctx, candidate_id=candidate).data["items"][0]
        assert item["engine"] == "deterministic" and item["agent_run_id"] is None
        assert calls == []
    finally:
        services.close()


def test_conflicting_rule_investigation_pins_old_and_new_sources(tmp_path):
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry = services.registry
        at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        ctx = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="conflict", as_of=at)
        assets = registry.resolve(AssetRepository)
        def ingest(name, text, kind="process_doc"):
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")
            return assets.ingest(ctx, str(path), kind, at, nature="synthetic")
        old = ingest("old.md", "主管压力 10 分钟内下降超过 0.02 MPa 时检查。")
        new = ingest("new.md", "主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时，按泄漏处理。\n生效日期：2026-06-01。")
        standard = ingest("standard.md", "模拟标准：应检查管道泄漏并维护。", "standard")
        old_id = registry.tools["extract_rules"].handler(ctx, asset_version_id=old.asset_version_id).data["candidate_ids"][0]
        versions = registry.resolve(VersionStore)
        previous = approve_rule(CandidateStore(versions.writer, settings.plant_id).get(old_id),
                    Actor(actor_id="reviewer", roles=(Role.reviewer,)), subject_id=f"entity://{settings.plant_id}/air_header/AIR-HDR",
                    effective_from=datetime(2026, 5, 1, tzinfo=timezone.utc), nature="synthetic", condition={"standard_alignment": {
                        "asset_version_id": standard.asset_version_id, "locator": "模拟第一条"}})
        artifacts = RuleArtifacts(versions.writer, settings.plant_id)
        component = artifacts.prepare([previous], Actor(actor_id="reviewer", roles=(Role.reviewer,)))
        manifest = versions.prepare({"rules": component}, "模拟旧规程")
        versions.check_integrity(manifest.version_manifest_id)
        versions.activate(None, manifest.version_manifest_id, Actor(actor_id="reviewer", roles=(Role.reviewer,)), "模拟旧规程")
        bound = ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id})
        result = registry.tools["extract_rules"].handler(bound, asset_version_id=new.asset_version_id)
        candidate_id = result.data["candidate_ids"][0]
        assert "conflicting_rule" in result.data["routing"][0]["reasons"]
        fallback = registry.tools["investigate_onboarding"].handler(bound, candidate_id=candidate_id)
        fallback_job = registry.resolve(JobRunner).get(fallback.job_id, wait_seconds=10)
        assert fallback_job.status == "succeeded", fallback_job.error
        offline = registry.tools["onboarding_investigations"].handler(bound, candidate_id=candidate_id).data["items"][0]
        assert offline["decision"] == "replace" and offline["engine"] == "deterministic" and offline["nature"] == "synthetic"

        class FakeGateway:
            async def run(self, actor, query, agent_name, as_of=None, **options):
                assert old.asset_version_id in query and new.asset_version_id in query
                assert "应检查管道泄漏并维护" in query and "effective_from" in query
                yield SimpleNamespace(agent_run_id="agent-conflict")

        class FakeReader:
            def get(self, agent_run_id):
                answer = {"decision": "replace", "rationale": "新手册替代旧规程",
                          "citations": [old.asset_version_id, new.asset_version_id, standard.asset_version_id],
                          "standard_ref": standard.asset_version_id, "standard_excerpt": "应检查管道泄漏并维护"}
                return SimpleNamespace(status="finished", final_answer=json.dumps(answer, ensure_ascii=False))

        registry.ports[Gateway] = FakeGateway()
        registry.ports[AgentRunReader] = FakeReader()
        job = registry.tools["investigate_onboarding"].handler(bound, candidate_id=candidate_id)
        finished = registry.resolve(JobRunner).get(job.job_id, wait_seconds=10)
        assert finished.status == "succeeded", finished.error
        item = registry.tools["onboarding_investigations"].handler(bound, candidate_id=candidate_id).data["items"][-1]
        assert item["decision"] == "replace" and item["manifest_id"] == manifest.version_manifest_id
        assert versions.active().version_manifest_id == manifest.version_manifest_id
    finally:
        services.close()
