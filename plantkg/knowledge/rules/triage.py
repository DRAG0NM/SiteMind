"""入库流程段：判别、图谱对象、标准和活动规则冲突逐项核对。"""
from __future__ import annotations

from plantkg.core.contracts import ErrorCode, OntologyCandidate, PlantkgError, Rule
from plantkg.core.ports import AssetRepository, Judge, VersionStore
from plantkg.knowledge.ontology.builder import DEFINITIONS, OPTIONS, vector_recall
from plantkg.knowledge.rules.normalise import entity_keys, normalised_kind, normalised_view, subject_keys, unresolved_entity, views_conflict


def assess(registry, artifacts, templates, context, candidate):
    reasons = []
    if candidate.draft.get("missing"):
        reasons.append("incomplete_extraction")
    if candidate.extractor_version == "unhandled-v1":
        reasons.append("no_template")
    examples = [e for e in templates.examples if e["document_type"] == candidate.extractor_version
                and e.get("alias") and e["alias"] in str(candidate.draft.get("source_text", ""))]
    fields = examples[-1].get("field_map", {}) if examples else {}
    subject = fields.get("subject_id")
    graph_hooks = registry.hooks_for("router.entity_type")
    if not subject or unresolved_entity(graph_hooks[-1] if graph_hooks else None, subject):
        reasons.append("missing_entity")
    standard = fields.get("standard_ref")
    standard_locator = fields.get("standard_locator")
    if not standard or not standard_locator:
        reasons.append("unaligned_standard")
    else:
        try:
            registry.resolve(AssetRepository).get(context, standard)
        except PlantkgError:
            reasons.append("unaligned_standard")
    options = OPTIONS["constraint"]
    text = str(candidate.draft.get("source_text", ""))[:150]
    top, scores = vector_recall(text, options)
    q = OntologyCandidate(candidate_id=candidate.candidate_id, layer="constraint", subject=text or candidate.candidate_id,
                          options=options, recall_source="vector" if top else "rule")
    judge_state = {"content": text, "definitions": {o: DEFINITIONS[o] for o in options}, "coverage": 0.0}
    if top:
        judge_state.update(vector_top=top, vector_scores=scores)
    try:
        judged = registry.resolve(Judge).decide(judge_state, q, options)
        if not judged.gate_accept:
            reasons.append("judge_gate")
        expected = {"compressor_pressure_drop": OPTIONS["constraint"][1], "zone_temperature_limit": OPTIONS["constraint"][0],
                    "elevator_congestion_rule": OPTIONS["constraint"][3]}
        if candidate.draft.get("kind") in expected and judged.choice != expected[candidate.draft["kind"]]:
            reasons.append("judge_conflict")
        judgment = judged.model_dump(mode="json")
    except (PlantkgError, ValueError):
        reasons.append("judge_unavailable")
        judgment = None
    versions = registry.resolve(VersionStore)
    try:
        manifest = versions.get(context.version_manifest_id) if context.version_manifest_id else versions.active()
    except PlantkgError as exc:
        if exc.code != ErrorCode.PRECONDITION:
            raise
        manifest = None
    if manifest:
        component = manifest.components.get("rules")
        if component and component.ref.startswith("b30-rules:"):
            existing = [Rule.model_validate(value) for value in artifacts.load(component)["rules"]]
            kind = normalised_kind(candidate.draft)
            view = normalised_view(candidate.draft)
            candidate_keys = set(entity_keys(subject))
            for rule in existing:
                if rule.kind == kind and (not candidate_keys or candidate_keys & subject_keys(rule)) \
                        and views_conflict(normalised_view(rule), view):
                    reasons.append("conflicting_rule")
                    break
        elif component and component.ref != "scenario-knowledge:empty":
            reasons.append("rules_not_inspectable")
    return {"stage": "agent" if reasons else "workflow", "reasons": reasons, "judge": judgment,
            "subject_id": subject, "standard_ref": standard, "standard_locator": standard_locator}
