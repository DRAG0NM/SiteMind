"""离线演示 B30 资料 → 调查 → 人工确认 → 版本 → 专家行为。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Role
from plantkg.core.ports import AssetRepository, KnowledgePublisher, OntologyBuilder, PendingActionStore, VersionStore
from plantkg.experts.equipment.expert import EquipmentExpert
from plantkg.experts.equipment.source import apply_published_rule
from plantkg.experts.hvac.expert import HvacExpert
from plantkg.experts.hvac.install import _knowledge_expert
from plantkg.knowledge.rules.compile import approve_rule, compile_reviewed_rule
from plantkg.knowledge.rules.onboarding import ExceptionAgent, Source, batch_metrics
from plantkg.knowledge.rules.review import ReviewItem
from plantkg.knowledge.rules.publisher import RuleArtifacts, candidate_for
from plantkg.knowledge.rules.store import CandidateStore

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)


def main():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        registry, plant = services.registry, settings.plant_id
        versions, assets = registry.resolve(VersionStore), registry.resolve(AssetRepository)
        reviewer = Actor(actor_id="demo-reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="demo-operator", roles=(Role.operator,))
        ctx = Context(plant_id=plant, industry_pack=settings.industry_pack, actor=operator, channel="offline",
                      trace_id="b30-demo", as_of=AT)
        paths = [ROOT / "data/docs/equipment/compressor-station-procedure-2024.md",
                 ROOT / "data/docs/equipment/compressor-maintenance-manual.md",
                 ROOT / "plantkg/knowledge/rules/fixtures/office-temperature.md",
                 ROOT / "plantkg/knowledge/rules/fixtures/tsg31-2025-excerpt.md"]
        dates = [datetime(2024, 6, 1, tzinfo=timezone.utc), datetime(2025, 11, 1, tzinfo=timezone.utc),
                 datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc)]
        docs = [assets.ingest(ctx, str(path), "process_doc", when, nature="derived" if i == 3 else "synthetic")
                for i, (path, when) in enumerate(zip(paths, dates))]
        store = CandidateStore(versions.writer, plant)
        extracted = []
        for doc in docs[:3]:
            result = registry.tools["extract_rules"].handler(ctx, asset_version_id=doc.asset_version_id)
            extracted.append(store.get(result.data["candidate_ids"][0]))
        old, new, temperature = extracted
        entity = f"entity://{plant}/air_header/AIR-HDR"
        old_rule = approve_rule(old, reviewer, subject_id=entity, effective_from=dates[0], nature="synthetic")
        artifacts = RuleArtifacts(versions.writer, plant)
        initial_ref = artifacts.prepare([old_rule], reviewer)
        base = versions.prepare({"rules": initial_ref}, "演示起始知识")
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, "演示起始知识")
        print("[synthetic] 旧规程", old.draft["source_text"], "→", old_rule.value, "bar/h")
        print("[synthetic] 新手册", new.draft["source_text"], "→", new.draft["value"], "bar/h")

        registry.tools["extract_rules"].handler(ctx, asset_version_id=docs[1].asset_version_id)
        routing = store.route(new.candidate_id)
        item = ReviewItem(new, tuple(routing["reasons"]), None, 0.0)
        sources = [Source(docs[0].asset_version_id, "2.1", old.draft["source_text"], dates[0].isoformat()),
                   Source(docs[1].asset_version_id, "5.1", new.draft["source_text"], dates[1].isoformat()),
                   Source(docs[3].asset_version_id, "6.4/6.5.1", paths[3].read_text(encoding="utf-8"), dates[3].isoformat(), kind="standard")]
        response = json.dumps({"decision": "replace", "rationale": "新手册替代旧规程；标准要求检查泄漏，未规定本手册的数值。"
                               "标准适用性演示假设主管公称直径为 80 mm（configured）。", "citations": [d.asset_version_id for d in (docs[0], docs[1], docs[3])]}, ensure_ascii=False)
        started = time.perf_counter()
        investigation = ExceptionAgent(lambda _: response).investigate(item, sources, as_of=AT)
        print("例外", item.reasons, "判别后端", (routing["judge"] or {}).get("backend", "不可用"),
              "→ 离线 Agent 替身建议", investigation.decision, investigation.rationale)
        print("来源", ", ".join(investigation.citations))
        elapsed = time.perf_counter() - started
        print("[simulated] 批次指标（无模型调用、模拟复核）", batch_metrics([item], review_minutes=elapsed / 60, tokens=0, elapsed_seconds=elapsed))

        for c in (new, temperature):
            suggestion = registry.tools["propose_review_decision"].handler(ctx, review_id=c.candidate_id,
                                                                           decision="accept", rationale="演示复核原文")
            registry.resolve(PendingActionStore).confirm(suggestion.data["action_id"], reviewer, "演示复核确认", 0,
                                                         "review-" + c.candidate_id)
        actions = registry.resolve(PendingActionStore)
        leak_rule = compile_reviewed_rule(store, actions, new.candidate_id, reviewer, subject_id=entity,
                                          effective_from=dates[1], nature="synthetic")
        temp_rule = compile_reviewed_rule(store, actions, temperature.candidate_id, reviewer,
                                          subject_id=f"entity://{plant}/zone/admin", effective_from=dates[2], nature="synthetic")
        builder = registry.resolve(OntologyBuilder)
        ont = builder.propose_candidates(ctx, [f"entity://{plant}/air_compressor/AC-2"], "concept")[0]
        builder.review(ont.candidate_id, ont.options[0], reviewer)
        graph = builder.prepare_graph(ctx, reviewer)
        ref = artifacts.prepare([leak_rule, temp_rule], reviewer, ontology=graph)
        candidate = candidate_for(ref, base.version_manifest_id, "b30-demo", ontology=graph)
        action = registry.resolve(KnowledgePublisher).propose_publish(candidate)
        print("发布提议", action.action_id, "确认前版本", versions.active().version_manifest_id)
        registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "演示发布确认", 0, "publish-b30-demo")
        active = versions.active()
        print("确认后版本", active.version_manifest_id, "本体", graph.ref)

        hvac = HvacExpert()
        event = {"event_type": "load_spike", "entity_id": f"entity://{plant}/zone/admin"}
        state = {"at": AT, "plant_id": plant, "zones": {"admin": {"temperature": 24.8, "power_kw": 20}}}
        before, _ = _knowledge_expert(registry, hvac, base.version_manifest_id, AT, AT)
        after, refs = _knowledge_expert(registry, hvac, active.version_manifest_id, AT, AT)
        print("暖通推迟供冷动作：发布前", len(before.propose(event, state).actions), "发布后", len(after.propose(event, state).actions), refs)
        samples = [{"at": AT + timedelta(minutes=i), "pressure_bar": 7 - 0.6 * i / 10,
                    "current_a": 5, "oil_c": 65, "on": True} for i in range(11)]
        equipment = {"ticket_id": "demo-leak", "trigger_at": AT + timedelta(minutes=10),
                     "entity_id": f"entity://{plant}/air_compressor/AC-2", "ids": {"air_system": entity},
                     "readings": {"samples": samples}, "baseline": {"running_ratio": 1, "current_mean_a": 5}}
        apply_published_rule(registry, active.version_manifest_id, AT + timedelta(minutes=10), AT, equipment)
        proposal = EquipmentExpert().propose({"event_type": "compressor_leak", "entity_id": equipment["entity_id"]}, equipment)
        print("[simulated] 第二幕泄漏研判", proposal.diagnosis, "依据", proposal.evidence_refs)
    finally:
        services.close()


if __name__ == "__main__":
    main()
