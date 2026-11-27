from datetime import datetime, timezone

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, PlantkgError, Role
from plantkg.core.ports import AssetRepository, GraphStore, OntologyBuilder, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.compile import compile_reviewed_rule
from plantkg.knowledge.rules.store import CandidateStore
from plantkg.knowledge.ontology.shapes import validate_turtle
from plantkg.knowledge.rules.onboarding import TemplateStore


def test_reviewed_ontology_graph_becomes_visible_only_after_publish(tmp_path):
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry, plant = services.registry, settings.plant_id
        versions = registry.resolve(VersionStore)
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        ctx = Context(plant_id=plant, industry_pack=settings.industry_pack, actor=operator, channel="offline", trace_id="b30-graph", as_of=now)
        document = tmp_path / "temperature.txt"
        document.write_text("行政楼温度24±1 ℃。", encoding="utf-8")  # synthetic 厂区规范
        asset = registry.resolve(AssetRepository).ingest(ctx, str(document), "process_doc", now, nature="synthetic")
        extracted = registry.tools["extract_rules"].handler(ctx, asset_version_id=asset.asset_version_id)
        candidate_id = extracted.data["candidate_ids"][0]
        suggested = registry.tools["propose_review_decision"].handler(ctx, review_id=candidate_id,
                                                                     decision="accept", rationale="核对原文")
        assert registry.tools["next_review_batch"].handler(ctx).data["items"]
        registry.resolve(PendingActionStore).confirm(suggested.data["action_id"], reviewer, "确认复核", 0, "b30-review")
        assert registry.tools["next_review_batch"].handler(ctx).data["items"] == []
        learned = TemplateStore(versions.writer, plant).load()
        assert "temperature-template-v1" in learned.types and learned.calibration[0]["candidate_id"] == candidate_id
        rule = compile_reviewed_rule(CandidateStore(versions.writer, plant), registry.resolve(PendingActionStore),
                                     candidate_id, reviewer, subject_id=f"entity://{plant}/zone/admin", effective_from=now, nature="synthetic")
        artifacts = RuleArtifacts(versions.writer, plant)
        initial = artifacts.prepare([rule], reviewer)
        base = versions.prepare({"rules": initial}, "initial")
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, "initial")
        builder = registry.resolve(OntologyBuilder)
        candidate = builder.propose_candidates(ctx, [f"entity://{plant}/air_compressor/AC-2"], "concept")[0]
        review = registry.tools["propose_review_decision"].handler(ctx, review_id=candidate.candidate_id,
                                                                  decision="accept", rationale="核对设备类型",
                                                                  revision={"choice": candidate.options[0]})
        registry.resolve(PendingActionStore).confirm(review.data["action_id"], reviewer, "确认本体复核", 0, "b30-ontology-review")
        pending_result = registry.tools["propose_publish"].handler(ctx, candidate_id=candidate.candidate_id, rationale="发布本体")
        pending = registry.resolve(PendingActionStore).get(pending_result.data["action_id"])
        graph_ref = pending.payload["candidate"]["changes"]["ontology"]
        with versions.writer.read_only() as con:
            graph_name = con.execute("SELECT name FROM storage_artifacts WHERE plant_id=? AND reference=?", (plant, graph_ref["ref"])).fetchone()[0]
        graph = registry.resolve(GraphStore)
        old_context = ctx.model_copy(update={"version_manifest_id": base.version_manifest_id})
        with pytest.raises(PlantkgError):
            graph.read_named_graph(old_context, graph_name, "triples", {})
        registry.resolve(PendingActionStore).confirm(pending.action_id, reviewer, "确认发布", 0, "b30-confirm-graph")
        active = versions.active()
        new_context = ctx.model_copy(update={"version_manifest_id": active.version_manifest_id})
        triples, _ = graph.read_named_graph(new_context, graph_name, "triples", {})
        assert any("Compressor" in row["object"] for row in triples)
        assert registry.tools["graph_query"].handler(new_context, query_id="triples").data["items"]
        assert registry.tools["lookup_rules"].handler(new_context, kind="zone_temperature_limit").data["items"]
        assert registry.tools["validate"].handler(new_context).data["issues"] == []
        assert builder.validate(new_context, graph_name) == []
        assert validate_turtle(f"<entity://{plant}/air_compressor/AC-2> a <https://brickschema.org/schema/Brick#Compressor> .")
        with pytest.raises(PlantkgError):
            graph.read_named_graph(old_context, graph_name, "triples", {})
    finally:
        services.close()
