from datetime import datetime, timezone
from pathlib import Path

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, ComponentRef, Context, Role, Rule
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.knowledge.documents import AssetCandidateStore, load_library, mine, publish
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.experts.equipment.expert import EquipmentExpert
from plantkg.experts.equipment.source import apply_published_rule


def test_reviewed_workorder_candidate_publishes_with_original_citations(tmp_path):
    root = Path(__file__).resolve().parents[3]
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry = services.registry
        at = datetime(2026, 7, 1, 9, tzinfo=timezone.utc)
        ctx = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="workorder", as_of=at)
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        library = load_library(root, indexes_path=tmp_path / "no-upload.json")
        mined = mine(library, as_of=at, created_at=at)
        stored = publish(mined, store=AssetCandidateStore(registry.resolve(AssetRepository), ctx, tmp_path / "candidates"))
        asset_id = stored.records[0].candidate_asset_version_id
        versions = registry.resolve(VersionStore)
        artifacts = RuleArtifacts(versions.writer, settings.plant_id)
        initial = artifacts.prepare([], reviewer, ontology=ComponentRef(ref="graph:v1:test", content_hash="sha256:" + "0" * 64))
        base = versions.prepare({"rules": initial}, "模拟初始知识")
        versions.check_integrity(base.version_manifest_id)
        versions.activate(None, base.version_manifest_id, reviewer, "模拟初始知识")
        candidate = registry.tools["extract_rules"].handler(ctx, asset_version_id=asset_id).data["candidate_ids"][0]
        revision = {"subject_id": f"entity://{settings.plant_id}/air_compressor/AC-2",
                    "effective_from": at.isoformat(), "workorder_condition": {
                        "event_type": "compressor_leak", "observation": "主管压力异常",
                        "recommendation": "按维护手册检查并复核相关工单"}}
        suggestion = registry.tools["propose_review_decision"].handler(ctx, review_id=candidate, decision="revise",
                                                                       revision=revision, rationale="模拟核对工单")
        actions = registry.resolve(PendingActionStore)
        actions.confirm(suggestion.data["action_id"], reviewer, "模拟人工复核", 0, "workorder-review")
        proposal = registry.tools["propose_publish"].handler(ctx, candidate_id=candidate, rationale="发布工单建议")
        actions.confirm(proposal.data["action_id"], reviewer, "模拟人工确认", 0, "workorder-publish")
        rule = Rule.model_validate(artifacts.load(versions.active().components["rules"])["rules"][0])
        assert rule.kind == "maintenance_advisory" and rule.condition["observation"] == "主管压力异常"
        assert {item["order_id"] for item in rule.condition["workorder_citations"]} == set(stored.records[0].cluster.citations)
        entity = revision["subject_id"]
        event = {"event_type": "compressor_leak", "entity_id": entity, "summary": "主管压力异常", "ticket_id": "workorder-test"}
        before, after = {}, {}
        apply_published_rule(registry, base.version_manifest_id, at, at, before, entity)
        apply_published_rule(registry, versions.active().version_manifest_id, at, at, after, entity)
        expert = EquipmentExpert()
        assert f"rule:{rule.rule_id}" not in expert.propose(event, before).evidence_refs
        proposal = expert.propose(event, after)
        assert f"rule:{rule.rule_id}" in proposal.evidence_refs and "复核相关工单" in proposal.diagnosis
    finally:
        services.close()
