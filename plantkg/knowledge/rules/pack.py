"""经领域包装配 B30 的规则抽取与发布兜底。"""
from datetime import datetime, timezone
from pathlib import Path

import yaml

from plantkg.core.contracts import Actor, ComponentRef, Context, ErrorCode, ManifestComponent, PlantkgError, Role, Rule, tool_error
from plantkg.core.ports import AssetContentReader, AssetRepository, GraphStore, KnowledgePublisher, OntologyBuilder, PendingActionStore, RuleExtractor, VersionStore
from plantkg.knowledge.ontology import OntologyWorkbench
from plantkg.knowledge.rules.extract import DocumentRuleExtractor
from plantkg.knowledge.rules.publisher import FallbackPublisher, PublishHandler, RuleArtifacts
from plantkg.knowledge.rules.normalise import consumer_mismatch, same_access, zone_hook_for
from plantkg.knowledge.rules.runtime import matches_combo_advice, matches_combo_rule, rules_for_manifest
from plantkg.knowledge.rules.store import CandidateStore
from plantkg.knowledge.rules.tools import install_tools
from plantkg.knowledge.rules.onboarding import TemplateStore
from plantkg.knowledge.rules.scenario import scenario_plugin
from plantkg.knowledge.rules.investigation import install_investigation
from plantkg.knowledge.rules.score_job import install_score_job
from plantkg.knowledge.rules.batch_metrics import install_metrics
from plantkg.knowledge.rules.tariff_review import install_tariff_review
from plantkg.knowledge.rules.onboarding_flow import install_flow

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SITE = ROOT / "configs" / "sites" / "factory" / "site.yaml"


def configured_tariff_category(settings):
    """厂区配置里的电价类别；没有配置时返回 None，发布期据此拒绝电价规则（#205-2）。"""
    knowledge = settings.section("knowledge") if settings is not None else None
    site = knowledge.get("site") if isinstance(knowledge, dict) else None
    path = ROOT / site if site else DEFAULT_SITE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"厂区配置不可读：{path}") from exc
    category = (data.get("tariff") or {}).get("category")
    return category.strip() if isinstance(category, str) and category.strip() else None


def install(registry, settings=None):
    registry.register(RuleExtractor, DocumentRuleExtractor())
    if VersionStore not in registry.ports or PendingActionStore not in registry.ports:
        def unavailable(*args, **kwargs):
            return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "资料复核需要启用存储")
        for name in ("extract_rules", "judge_candidates", "link_entities", "next_review_batch", "propose_candidates",
                     "propose_review_decision", "propose_publish", "lookup_rules", "graph_query", "validate", "snapshot", "diff",
                     "investigate_onboarding", "onboarding_investigations", "score_review_candidate",
                     "record_onboarding_batch", "prepare_tariff_review", "process_onboarding"):
            registry.register_tool(name, unavailable, placeholder=True)
        return
    versions = registry.resolve(VersionStore)
    actions = registry.resolve(PendingActionStore)
    tariff_category = configured_tariff_category(settings)
    if GraphStore in registry.ports:
        builder = OntologyWorkbench(registry.resolve(GraphStore), versions)
        registry.register(OntologyBuilder, builder)
        registry.register_hook("knowledge.ontology.propose_document", builder.propose_document_candidate)
    artifacts = RuleArtifacts(versions.writer, versions.plant_id)
    versions.register_verifier("b30-rules:", artifacts.verify)
    registry.register_hook("knowledge.rules.artifacts", lambda writer, plant_id: RuleArtifacts(writer, plant_id))

    def copy_trial_components(components, trial_versions, candidate_changes=False):
        target = RuleArtifacts(trial_versions.writer, trial_versions.plant_id)
        if not trial_versions.has_verifier("b30-rules:"):
            trial_versions.register_verifier("b30-rules:", target.verify)
        copy_artifact = registry.hooks_for("evolution.copy_artifacts")
        copied = {}
        for key, ref in components.items():
            if ref.ref.startswith("b30-rules:"):
                body = artifacts.load(ref)
                copied[key] = target.prepare([Rule.model_validate(rule) for rule in body["rules"]],
                                             Actor(actor_id="b32-trial", roles=(Role.reviewer,)),
                                             ontology=ComponentRef.model_validate(body["ontology"]) if body.get("ontology") else None)
            elif candidate_changes and key == ManifestComponent.rules:
                raise PlantkgError(ErrorCode.PRECONDITION, "规则候选缺少可复制的 B30 规则产物")
            elif copy_artifact and (copied_ref := copy_artifact[-1](ref, trial_versions)) is not None:
                copied[key] = copied_ref
            else:
                copied[key] = ref  # 场景知识/参数等冻结引用：trial 清单按原摘要校验
        return copied

    registry.register_hook("evolution.copy_trial_components", copy_trial_components)
    registry.register_hook("knowledge.b30.rules", lambda manifest_id, at, as_of: rules_for_manifest(versions, artifacts, manifest_id, at, as_of))
    registry.register_hook("knowledge.rules.matches_combo", matches_combo_rule)
    registry.register_hook("knowledge.rules.matches_combo_advice", matches_combo_advice)

    def consumer_check(rules):
        entity_type = (registry.hooks_for("router.entity_type") or (None,))[-1]
        for rule in rules:
            mismatch = consumer_mismatch(rule, entity_type, zone_hook_for(rule, registry), tariff_category)
            if mismatch is not None:
                return mismatch
        return None

    # B32 的发布前校验经这个钩子复用同一实现（跨平面只交钩子，#143/#144）
    registry.register_hook("knowledge.rules.consumer_mismatch", consumer_check)
    # B32 纠错候选保留既有规则时按同一消费端主体归一去重（#252）：跨平面只交钩子，不在 evolution 里另写一份归一
    registry.register_hook("knowledge.rules.same_access", same_access)
    context = Context(plant_id=versions.plant_id, industry_pack=settings.industry_pack,
                      actor=Actor(actor_id="b30-service", roles=(Role.operator,)), channel="offline",
                      trace_id="b30-publish", as_of=datetime.now(timezone.utc))
    registry.register(KnowledgePublisher, FallbackPublisher(versions, actions, artifacts, context), default=True)
    registry.register_action_handler("publish", PublishHandler(versions, artifacts), default=True)
    if AssetRepository in registry.ports and AssetContentReader in registry.ports:
        candidates = CandidateStore(versions.writer, versions.plant_id)
        templates = TemplateStore(versions.writer, versions.plant_id)
        install_tools(registry, candidates, templates, artifacts, tariff_category)
        install_investigation(registry, candidates, settings, artifacts)
        install_score_job(registry, candidates, settings)
        install_metrics(registry, candidates, templates)
        install_tariff_review(registry, candidates)
        install_flow(registry)
        registry.register_hook("scenario_plugin", scenario_plugin(registry, artifacts))
