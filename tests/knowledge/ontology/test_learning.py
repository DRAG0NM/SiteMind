from datetime import datetime, timezone
from pathlib import Path
import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, PlantkgError, Role
from plantkg.core.ports import AssetRepository, JobRunner, PendingActionStore, VersionStore
from plantkg.knowledge.rules.store import CandidateStore
from plantkg.knowledge.rules.onboarding import TemplateStore


def test_reviewed_example_reduces_exception_rate_on_same_type(tmp_path):
    settings = load_settings(profile="mock", overrides={"storage.enabled": True, "judge.backend": "fallback"})
    services = build_services(settings)
    try:
        registry = services.registry
        at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="learn", as_of=at)
        assets = registry.resolve(AssetRepository)
        standard_path = Path(__file__).resolve().parents[3] / "plantkg/knowledge/rules/fixtures/tsg31-2025-excerpt.md"
        standard = assets.ingest(context, str(standard_path), "process_doc", at, nature="derived")
        sources = []
        for i in range(2):
            path = tmp_path / f"manual{i}.md"
            path.write_text(f"第{i + 1}批资料\n主管压力 10 分钟内下降超过 0.05 MPa、且空压机仍在持续供气时，按泄漏处理。", encoding="utf-8")
            sources.append(assets.ingest(context, str(path), "process_doc", at, nature="synthetic"))
        first = registry.tools["process_onboarding"].handler(context, asset_version_id=sources[0].asset_version_id)
        assert first.data["routing"][0]["stage"] == "agent"
        job_id = first.data["investigation_jobs"][0]["job_id"]
        assert registry.resolve(JobRunner).get(job_id, wait_seconds=10).status == "succeeded"
        assert registry.tools["onboarding_investigations"].handler(context, candidate_id=first.data["candidate_ids"][0]).data["items"][0]["engine"] == "deterministic"
        suggestion = registry.tools["propose_review_decision"].handler(
            context, review_id=first.data["candidate_ids"][0], decision="accept", rationale="核对来源与图谱",
            revision={"subject_id": f"entity://{settings.plant_id}/air_header/AIR-HDR", "alias": "主管压力",
                      "standard_ref": standard.asset_version_id, "standard_locator": "第一条",
                      "effective_from": "2026-07-01T00:00:00Z"})
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        registry.resolve(PendingActionStore).confirm(suggestion.data["action_id"], reviewer, "确认复核", 0, "learn-first")
        versions = registry.resolve(VersionStore)
        candidate_store = CandidateStore(versions.writer, settings.plant_id)
        original_decision = candidate_store.decision(first.data["candidate_ids"][0])
        review_ctx = context.model_copy(update={"actor": reviewer})
        metrics = registry.tools["record_onboarding_batch"].handler(
            review_ctx, batch_id="first", candidate_ids=first.data["candidate_ids"],
            review_minutes=2.0, tokens=150, elapsed_seconds=120.0)
        assert metrics.data["agent_fraction"] == 1.0 and metrics.data["tokens"] == 150
        assert metrics.data["standard_alignment_rate"] == 1.0
        registry.tools["record_onboarding_batch"].handler(
            review_ctx, batch_id="first", candidate_ids=first.data["candidate_ids"],
            review_minutes=2.0, tokens=150, elapsed_seconds=120.0)
        with pytest.raises(PlantkgError):
            registry.tools["record_onboarding_batch"].handler(
                review_ctx, batch_id="first", candidate_ids=first.data["candidate_ids"],
                review_minutes=2.0, tokens=151, elapsed_seconds=120.0)
        second = registry.tools["process_onboarding"].handler(context, asset_version_id=sources[1].asset_version_id)
        assert second.data["routing"][0]["stage"] == "workflow"
        assert second.data["investigation_jobs"] == []
        correction = registry.tools["propose_review_decision"].handler(
            context, review_id=first.data["candidate_ids"][0], decision="revise", rationale="补充别名",
            revision={"subject_id": f"entity://{settings.plant_id}/air_header/AIR-HDR", "alias": "压缩空气主管压力",
                      "standard_ref": standard.asset_version_id, "standard_locator": "第一条",
                      "effective_from": "2026-07-01T00:00:00Z"})
        registry.resolve(PendingActionStore).confirm(correction.data["action_id"], reviewer, "更正复核", 0, "learn-correction")
        candidate_store.decide(first.data["candidate_ids"][0], original_decision)
        saved = candidate_store.decision(first.data["candidate_ids"][0])
        assert saved["decision"] == "revise" and saved["action_id"] == correction.data["action_id"]
        samples = TemplateStore(versions.writer, settings.plant_id).load().examples
        assert {sample["alias"] for sample in samples} >= {"主管压力", "压缩空气主管压力"}
        with versions.writer.read_only() as con:
            assert con.execute("SELECT count(*) FROM b30_decision_history WHERE plant_id=? AND candidate_id=?",
                               (settings.plant_id, first.data["candidate_ids"][0])).fetchone()[0] == 2
    finally:
        services.close()
