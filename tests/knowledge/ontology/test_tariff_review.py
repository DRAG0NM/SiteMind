from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, ComponentRef, Context, PlantkgError, Role, Rule
from plantkg.core.ports import AssetRepository, JobRunner, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.packs.tariff import parse_schedule


def test_reviewed_tariff_requires_calendar_and_valid_schedule(tmp_path):
    root = Path(__file__).resolve().parents[3]
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry = services.registry
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        ctx = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, actor=operator,
                      channel="offline", trace_id="tariff-review", as_of=now)
        assets = registry.resolve(AssetRepository)
        source = assets.ingest(ctx, str(root / "data/docs/energy/tariff-summary-2025.md"), "policy", now, nature="derived")
        calendar_path = tmp_path / "calendar.md"
        fixture = yaml.safe_load((root / "fixtures/packs/tariff/jiangsu_2025_426.yaml").read_text(encoding="utf-8"))["rules"][0]
        days = fixture["periods"][-1]["dates"]
        calendar_path.write_text("模拟节日日历\n春节：" + "、".join(d for d in days if d.startswith("2026-02"))
                                 + "\n五一：" + "、".join(d for d in days if d.startswith("2026-05"))
                                 + "\n国庆：" + "、".join(d for d in days if "-10-" in d)
                                 + "\n登记至：2026-12-31\n", encoding="utf-8")
        calendar = assets.ingest(ctx, str(calendar_path), "policy", now, nature="synthetic")
        versions = registry.resolve(VersionStore)
        artifacts = RuleArtifacts(versions.writer, settings.plant_id)
        initial = artifacts.prepare([], reviewer, ontology=ComponentRef(ref="graph:v1:test", content_hash="sha256:" + "0" * 64))
        base = versions.prepare({"rules": initial}, "test baseline")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "test baseline")
        candidate = registry.tools["extract_rules"].handler(ctx, asset_version_id=source.asset_version_id).data["candidate_ids"][0]
        score_ctx = ctx.model_copy(update={"version_manifest_id": base.version_manifest_id})
        score_job = registry.tools["score_review_candidate"].handler(
            score_ctx, candidate_id=candidate, alternative={"synthetic_demo": True, "peak_ratio": 1.1})
        score_result = registry.resolve(JobRunner).get(score_job.job_id, wait_seconds=30)
        assert score_result.status == "succeeded", score_result.error
        scored = registry.tools["next_review_batch"].handler(score_ctx).data["items"][0]["score"]
        assert scored["status"] == "scored" and scored["provenance"]["nature"] == "synthetic"
        assert scored["provenance"]["calibration_version"].startswith("synthetic:")
        prepared = registry.tools["prepare_tariff_review"].handler(
            ctx, candidate_id=candidate, calendar_asset_version=calendar.asset_version_id,
            subject_id=f"entity://{settings.plant_id}/grid_connection/MAIN", category=fixture["category"],
            effective_from="2025-06-01T00:00:00Z")
        revision = prepared.data["revision"]
        condition = revision["tariff_condition"]
        decision = registry.tools["propose_review_decision"].handler(ctx, review_id=candidate, decision="revise",
                                                                    revision=revision, rationale="模拟人工核对")
        registry.resolve(PendingActionStore).confirm(decision.data["action_id"], reviewer, "测试复核", 0, "tariff-review")
        proposal = registry.tools["propose_publish"].handler(ctx, candidate_id=candidate, rationale="测试发布")
        registry.resolve(PendingActionStore).confirm(proposal.data["action_id"], reviewer, "测试确认发布", 0, "tariff-publish")
        active = versions.active()
        rule = Rule.model_validate(artifacts.load(active.components["rules"])["rules"][0])
        assert rule.kind == "tariff_tou" and rule.condition["calendar_refs"] == [calendar.asset_version_id]
        assert not isinstance(parse_schedule(rule), tuple)
        assert rule.available_at == now

        bad_path = tmp_path / "bad-policy.md"
        bad_path.write_text((root / "data/docs/energy/tariff-summary-2025.md").read_text(encoding="utf-8") + "\n新版候选",
                            encoding="utf-8")
        bad_asset = assets.ingest(ctx, str(bad_path), "policy", now, nature="synthetic")
        bad_id = registry.tools["extract_rules"].handler(ctx, asset_version_id=bad_asset.asset_version_id).data["candidate_ids"][0]
        bad_revision = revision | {"tariff_condition": condition | {"periods": []}}
        bad = registry.tools["propose_review_decision"].handler(ctx, review_id=bad_id, decision="revise",
                                                               revision=bad_revision, rationale="测试缺段")
        registry.resolve(PendingActionStore).confirm(bad.data["action_id"], reviewer, "测试复核", 0, "tariff-bad-review")
        with pytest.raises(PlantkgError):
            registry.tools["propose_publish"].handler(ctx, candidate_id=bad_id, rationale="测试缺段")
    finally:
        services.close()
