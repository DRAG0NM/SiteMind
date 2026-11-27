"""B30 发布期电价类别取自厂区配置（#241 / #205-2）：第一条电价规则也校验，不再从旧规则推断。

默认装配（含 B32 演化）下由 B30 候选路径拦截；禁用演化时走 B30 兜底发布路径，两条路径都要生效。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, Locator, PlantkgError, Role, Rule
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.store import CandidateStore

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))
TARIFF_DOC = ROOT / "data/docs/energy/tariff-summary-2025.md"
CATEGORY = "two_part_industrial_ge315kva"


def _activate_baseline(registry, plant_id):
    """发布要求已有活动版本：先激活一条最小基线规则。"""
    versions = registry.resolve(VersionStore)
    rule = Rule(rule_id="baseline", kind="operation_window", subject_ids=(f"entity://{plant_id}/air_compressor/AC-1",),
                quantity="operation_start", unit="h", value=None, bounds={"lower": 0, "upper": 1},
                effective_from=AT - timedelta(days=1), available_at=AT - timedelta(days=1),
                source_asset_version=f"asset://{plant_id}/00000000/process/rule", locator=Locator(line_start=1),
                nature="synthetic", review_status="approved", reviewer="reviewer")
    component = RuleArtifacts(versions.writer, plant_id).prepare([rule], REVIEWER)
    manifest = versions.prepare({"rules": component}, "基线规则")
    versions.check_integrity(manifest.version_manifest_id)
    try:
        current = versions.active().version_manifest_id
    except PlantkgError:
        current = None
    versions.activate(current, manifest.version_manifest_id, REVIEWER, "基线规则")


def _calendar(registry, context, tmp_path):
    """节假日历取自电价 fixture，覆盖 2026 年的春节、五一、国庆。"""
    fixture = yaml.safe_load((ROOT / "fixtures/packs/tariff/jiangsu_2025_426.yaml").read_text(encoding="utf-8"))["rules"][0]
    days = fixture["periods"][-1]["dates"]
    path = tmp_path / "calendar.md"
    path.write_text("模拟节日日历\n春节：" + "、".join(d for d in days if d.startswith("2026-02"))
                    + "\n五一：" + "、".join(d for d in days if d.startswith("2026-05"))
                    + "\n国庆：" + "、".join(d for d in days if "-10-" in d)
                    + "\n登记至：2026-12-31\n", encoding="utf-8")
    return registry.resolve(AssetRepository).ingest(context, str(path), "policy", AT, nature="synthetic")


def _context(settings, trace_id):
    return Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                   actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id=trace_id, as_of=AT)


def _tariff_candidate(registry, context, tmp_path):
    asset = registry.resolve(AssetRepository).ingest(context, str(TARIFF_DOC), "policy", AT, nature="derived")
    return registry.tools["extract_rules"].handler(context, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]


def _prepare(registry, context, candidate, calendar, category, subject_id):
    return registry.tools["prepare_tariff_review"].handler(
        context, candidate_id=candidate, calendar_asset_version=calendar.asset_version_id,
        subject_id=subject_id, category=category, effective_from="2025-06-01T00:00:00Z").data["revision"]


def _review(registry, context, candidate_id, revision):
    decision = registry.tools["propose_review_decision"].handler(
        context, review_id=candidate_id, decision="revise", revision=revision, rationale="测试复核")
    registry.resolve(PendingActionStore).confirm(decision.data["action_id"], REVIEWER, "确认复核", 0,
                                                 "review-" + candidate_id + "-" + revision["tariff_condition"]["category"])


def test_first_tariff_rule_is_checked_against_site_config(tmp_path):
    """默认装配（含演化）：以前没有旧电价规则可比，错误类别会被放行；现在按厂区配置拒绝。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry, settings = services.registry, services.settings
        _activate_baseline(registry, settings.plant_id)
        context = _context(settings, "b30-tariff-category")
        calendar = _calendar(registry, context, tmp_path)
        candidate = _tariff_candidate(registry, context, tmp_path)
        _review(registry, context, candidate,
                _prepare(registry, context, candidate, calendar, "single_lt100kva",
                         f"entity://{settings.plant_id}/grid_connection/MAIN"))
        with pytest.raises(PlantkgError) as error:
            registry.tools["propose_publish"].handler(context, candidate_id=candidate, rationale="发布电价规则")
        assert error.value.code == ErrorCode.PRECONDITION
        assert "consumer_mismatch" in str(error.value) and "与厂区配置" in str(error.value)
    finally:
        services.close()


def test_tariff_category_checked_on_b30_fallback_and_correct_category_publishes(tmp_path):
    """禁用演化走 B30 兜底发布：错误类别拒绝并记 consumer_mismatch；改成厂区类别后发布成功。"""
    packs = [name for name in load_settings(profile="mock").packs if name != "evolution"]
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True, "packs": packs}))
    try:
        registry, settings = services.registry, services.settings
        _activate_baseline(registry, settings.plant_id)
        context = _context(settings, "b30-tariff-category-fallback")
        calendar = _calendar(registry, context, tmp_path)
        candidate = _tariff_candidate(registry, context, tmp_path)
        subject = f"entity://{settings.plant_id}/grid_connection/MAIN"
        _review(registry, context, candidate, _prepare(registry, context, candidate, calendar, "single_lt100kva", subject))
        with pytest.raises(PlantkgError) as error:
            registry.tools["propose_publish"].handler(context, candidate_id=candidate, rationale="发布电价规则")
        assert error.value.code == ErrorCode.PRECONDITION
        assert "consumer_mismatch" in str(error.value) and "与厂区配置" in str(error.value)
        versions = registry.resolve(VersionStore)
        route = CandidateStore(versions.writer, settings.plant_id).route(candidate)
        assert "consumer_mismatch" in route["reasons"]

        _review(registry, context, candidate, _prepare(registry, context, candidate, calendar, CATEGORY, subject))
        publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate, rationale="发布电价规则")
        assert publish.data["status"] == "proposed"
        registry.resolve(PendingActionStore).confirm(publish.data["action_id"], REVIEWER, "确认发布", 0,
                                                     "publish-" + candidate)
        active = versions.active()
        rules = [Rule.model_validate(value) for value in RuleArtifacts(versions.writer, settings.plant_id).load(
            active.components["rules"])["rules"]]
        assert any(rule.kind == "tariff_tou" and rule.condition.get("category") == CATEGORY for rule in rules)
    finally:
        services.close()


def _tariff_rule(plant_id, category):
    return Rule(rule_id="tariff-hook-check", kind="tariff_tou",
                subject_ids=(f"entity://{plant_id}/grid_connection/MAIN",),
                condition={"category": category, "timezone": "Asia/Shanghai", "periods": []},
                quantity="tou_factor", unit="1", value=1.0,
                effective_from=AT - timedelta(days=1), available_at=AT - timedelta(days=1),
                source_asset_version=f"asset://{plant_id}/00000000/policy/tariff",
                locator=Locator(line_start=1), nature="synthetic", review_status="approved", reviewer="reviewer")


def test_consumer_mismatch_hook_checks_tariff_category_against_site_config():
    """B32 原生候选走 knowledge.rules.consumer_mismatch 钩子；该钩子也按厂区配置校验电价类别。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry, settings = services.registry, services.settings
        check = registry.hooks_for("knowledge.rules.consumer_mismatch")[-1]
        assert "与厂区配置" in check([_tariff_rule(settings.plant_id, "single_lt100kva")])
        assert check([_tariff_rule(settings.plant_id, CATEGORY)]) is None
    finally:
        services.close()
