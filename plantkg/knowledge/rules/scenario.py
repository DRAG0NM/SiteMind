"""场景接缝：上传只生成候选；确认发布后下一分钟分叉到新清单。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError
from plantkg.core.ports import AssetRepository, VersionStore
from plantkg.knowledge.rules.runtime import all_rules_for_manifest, rules_for_manifest

ROOT = Path(__file__).resolve().parents[3]
DEMO_DOCUMENTS = {
    "《空压机运行维护手册》": [("data/docs/equipment/compressor-maintenance-manual.md", "2025-11-01T00:00:00Z", "synthetic", "process_doc", "compressor_leak_rule")],
    "各专家手册、规程与江苏分时电价政策": [
        ("data/docs/energy/bess-operation-manual.md", "2026-01-01T00:00:00Z", "synthetic", "process_doc", "energy_manual"),
        ("data/docs/elevator/elevator-safety-procedure.md", "2025-01-01T00:00:00Z", "synthetic", "process_doc", "elevator_manual"),
        ("plantkg/knowledge/rules/fixtures/elevator-dispatch.md", "2026-01-01T00:00:00Z", "synthetic", "process_doc", "elevator_manual"),
        ("data/docs/hvac/hvac-design-parameters.md", "2025-03-01T00:00:00Z", "synthetic", "process_doc", "hvac_manual"),
        ("data/docs/energy/tariff-summary-2025.md", "2025-04-28T16:00:00Z", "derived", "policy", "tariff_policy"),
    ],
}


def scenario_plugin(service_registry, artifacts):
    pending: dict[int, dict[str, dict]] = {}
    policy_hashes: dict[str, str] = {}
    verifier_installed = False
    versions = service_registry.resolve(VersionStore)
    assets = service_registry.resolve(AssetRepository)

    def stores(runner):
        current = runner.db.versions
        rule_artifacts = type(artifacts)(runner.db.writer, runner.db.plant_id)
        if "b30-rules:" not in current._verifiers:
            current.register_verifier("b30-rules:", rule_artifacts.verify)
        if runner.db.plant_id != versions.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "场景与知识服务的厂区不一致")
        return current, rule_artifacts

    def install(scenario_registry):
        prices = {}
        tariff_by_manifest = {}
        def check(runner):
            nonlocal verifier_installed
            current_versions, _ = stores(runner)
            body = runner.policy.model_dump(mode="json")
            policy_hashes["scenario-policy:" + runner.policy.policy_version] = _hash(body)
            if not verifier_installed:
                def verify_knowledge(component):
                    value = component.ref.removeprefix("scenario-knowledge:")
                    tags = [] if value == "empty" else value.split("+")
                    return "complete" if component.content_hash == "sha256:" + _hash({"tags": tags}) else "corrupt"

                def verify_policy(component):
                    expected = policy_hashes.get(component.ref)
                    return "complete" if expected and component.content_hash == "sha256:" + expected else "unverifiable"

                for version_store in (versions, current_versions):
                    if "scenario-knowledge:" not in version_store._verifiers:
                        version_store.register_verifier("scenario-knowledge:", verify_knowledge)
                    if "scenario-policy:" not in version_store._verifiers:
                        version_store.register_verifier("scenario-policy:", verify_policy)
                verifier_installed = True

        def upload(runner, at, payload):
            check(runner)
            context = runner._context(runner.run_info.version_manifest_id).model_copy(update={"as_of": at})
            asset_id = payload.get("asset_version_id")
            documents = [(asset_id, None)] if asset_id else []
            if not documents:
                specs = DEMO_DOCUMENTS.get(payload.get("document"))
                if specs is None:
                    runner.note("knowledge_pending", at, {"reason": "缺少已接入资料版本", "document": payload.get("document", "")})
                    return
                for path, available_at, nature, asset_type, tag in specs:
                    asset = assets.ingest(context, str(ROOT / path), asset_type,
                                          datetime.fromisoformat(available_at.replace("Z", "+00:00")), nature=nature)
                    documents.append((asset.asset_version_id, tag))
            for asset_id, tag in documents:
                result = service_registry.tools["process_onboarding"].handler(context, asset_version_id=asset_id)
                entries = pending.setdefault(id(runner), {})
                entry = entries.setdefault(asset_id, {"tag": tag, "candidate_ids": set()})
                entry["candidate_ids"].update(result.data["candidate_ids"])
                runner.note("knowledge_pending", at, {"asset_version_id": asset_id,
                                                        "candidate_ids": result.data["candidate_ids"],
                                                        "investigation_jobs": result.data["investigation_jobs"],
                                                        "reason": "候选等待复核和发布确认"})

        def step(runner, at):
            waiting = pending.get(id(runner))
            if not waiting:
                return
            check(runner)
            current_versions, current_artifacts = stores(runner)
            manifest = current_versions.active()
            if manifest.version_manifest_id == runner.run_info.version_manifest_id:
                return
            rules = rules_for_manifest(current_versions, current_artifacts, manifest.version_manifest_id, at, at)
            published = {asset_id: entry["candidate_ids"].intersection(
                         rule.rule_id for rule in rules if rule.source_asset_version == asset_id)
                         for asset_id, entry in waiting.items()}
            published = {asset_id: ids for asset_id, ids in published.items() if ids}
            if not published:
                return
            tags = set(runner.knowledge)
            tags.update(waiting[asset]["tag"] for asset in published if waiting[asset]["tag"])
            runner.fork(at, manifest.version_manifest_id, tags, "复核员确认发布资料规则")
            for asset, ids in published.items():
                waiting[asset]["candidate_ids"].difference_update(ids)
                if not waiting[asset]["candidate_ids"]:
                    del waiting[asset]

        def minute_inputs(runner, at, inputs, *, as_of=None):
            if runner.db.plant_id != versions.plant_id:
                return inputs
            current_versions, current_artifacts = stores(runner)
            slot = at.replace(minute=at.minute - at.minute % 15, second=0, microsecond=0)
            key = (runner.run_info.version_manifest_id, slot, as_of)
            if key not in prices:
                if key[0] not in tariff_by_manifest:
                    tariff_by_manifest[key[0]] = tuple(rule for rule in all_rules_for_manifest(current_versions, current_artifacts, key[0])
                                                        if rule.kind == "tariff_tou" and rule.compilable)
                tariff_rules = [rule for rule in tariff_by_manifest[key[0]]
                                if rule.is_visible(as_of or at) and rule.applies_to(at)]
                if not tariff_rules:
                    return inputs
                hooks = service_registry.hooks_for("knowledge.tariff.slot_price")
                if not hooks:
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "已发布电价缺少计价器")
                price = hooks[-1](runner.world.scenario, tariff_rules,
                                  Interval(start=slot, end=slot + timedelta(minutes=15)))
                if price is None:
                    return inputs
                prices[key] = price
            return replace(inputs, price_cny_per_kwh=prices[key])

        scenario_registry.register_hook("scenario.started", check)  # 起始清单发布时登记场景校验器（P1-B2）
        scenario_registry.register_hook("scenario.document_upload", upload)
        scenario_registry.register_hook("scenario.knowledge_step", step)
        scenario_registry.register_hook("scenario.minute_inputs", minute_inputs)
    return install


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
