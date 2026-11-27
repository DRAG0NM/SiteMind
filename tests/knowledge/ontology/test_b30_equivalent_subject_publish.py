"""B30 发布保留按归一主体比较（#241 / #205 追加-2）：grid_connection/MAIN 与 grid/GRID 视为同一接入点。

走默认装配的 propose_publish 端到端：先后发布两条等价主体的电价规则，激活清单只留后一条，
with_published_tariff 不再报重叠。第二份资料在原文末尾追加一行普通说明：AssetRepository 按内容哈希去重，
同一份文本会得到同一候选，拿不到第二份候选。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Interval, Locator, PlantkgError, Role, Rule
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.experts.energy.planner import load_scenario_packs, with_published_tariff
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))
CATEGORY = "two_part_industrial_ge315kva"
TARIFF_DOC = ROOT / "data/docs/energy/tariff-summary-2025.md"


def _activate_rules(registry, rules, label):
    versions = registry.resolve(VersionStore)
    component = RuleArtifacts(versions.writer, versions.plant_id).prepare(list(rules), REVIEWER)
    manifest = versions.prepare({"rules": component}, label)
    versions.check_integrity(manifest.version_manifest_id)
    try:
        current = versions.active().version_manifest_id
    except PlantkgError:
        current = None
    versions.activate(current, manifest.version_manifest_id, REVIEWER, label)


def _baseline_rule(plant_id):
    return Rule(rule_id="baseline", kind="operation_window", subject_ids=(f"entity://{plant_id}/air_compressor/AC-1",),
                quantity="operation_start", unit="h", value=None, bounds={"lower": 0, "upper": 1},
                effective_from=AT - timedelta(days=1), available_at=AT - timedelta(days=1),
                source_asset_version=f"asset://{plant_id}/00000000/process/rule", locator=Locator(line_start=1),
                nature="synthetic", review_status="approved", reviewer="reviewer")


def _calendar(registry, context, tmp_path):
    fixture = yaml.safe_load((ROOT / "fixtures/packs/tariff/jiangsu_2025_426.yaml").read_text(encoding="utf-8"))["rules"][0]
    days = fixture["periods"][-1]["dates"]
    path = tmp_path / "calendar.md"
    path.write_text("模拟节日日历\n春节：" + "、".join(d for d in days if d.startswith("2026-02"))
                    + "\n五一：" + "、".join(d for d in days if d.startswith("2026-05"))
                    + "\n国庆：" + "、".join(d for d in days if "-10-" in d)
                    + "\n登记至：2026-12-31\n", encoding="utf-8")
    return registry.resolve(AssetRepository).ingest(context, str(path), "policy", AT, nature="synthetic")


def _publish_tariff(registry, context, calendar, candidate_id, subject_id, label):
    prepared = registry.tools["prepare_tariff_review"].handler(
        context, candidate_id=candidate_id, calendar_asset_version=calendar.asset_version_id,
        subject_id=subject_id, category=CATEGORY, effective_from="2025-06-01T00:00:00Z")
    actions = registry.resolve(PendingActionStore)
    decision = registry.tools["propose_review_decision"].handler(
        context, review_id=candidate_id, decision="revise", revision=prepared.data["revision"], rationale="测试复核")
    actions.confirm(decision.data["action_id"], REVIEWER, "确认复核", 0, "review-" + label)
    publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="发布电价规则")
    actions.confirm(publish.data["action_id"], REVIEWER, "确认发布", 0, "publish-" + label)


def _extract(registry, context, path):
    asset = registry.resolve(AssetRepository).ingest(context, str(path), "policy", AT, nature="derived")
    return asset, registry.tools["extract_rules"].handler(
        context, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]


def test_equivalent_tariff_subjects_leave_one_rule_and_load_once(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry, settings = services.registry, services.settings
        _activate_rules(registry, [_baseline_rule(settings.plant_id)], "基线规则")
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-equivalent", as_of=AT)
        calendar = _calendar(registry, context, tmp_path)

        first_asset, first = _extract(registry, context, TARIFF_DOC)
        _publish_tariff(registry, context, calendar, first,
                        f"entity://{settings.plant_id}/grid_connection/MAIN", "first")

        second_path = tmp_path / "tariff-summary-2025-note.md"
        second_path.write_text(TARIFF_DOC.read_text(encoding="utf-8")
                               + "\n补充说明：以上时段与浮动比例同样适用于 315 千伏安及以上两部制用户。\n",
                               encoding="utf-8")
        second_asset, second = _extract(registry, context, second_path)
        assert second_asset.asset_version_id != first_asset.asset_version_id
        assert second != first
        _publish_tariff(registry, context, calendar, second,
                        f"entity://{settings.plant_id}/grid/GRID", "second")

        versions = registry.resolve(VersionStore)
        published = [Rule.model_validate(value) for value in RuleArtifacts(
            versions.writer, settings.plant_id).load(versions.active().components["rules"])["rules"]]
        tariffs = [rule for rule in published if rule.kind == "tariff_tou"]
        assert len(tariffs) == 1
        assert tariffs[0].subject_ids == (f"entity://{settings.plant_id}/grid/GRID",)

        # 同一归一口径下配置规则被替换，定价不再报“已发布电价规则存在重叠”
        scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
        slot = Interval(start=scenario.instant("08:00"), end=scenario.instant("08:00") + timedelta(minutes=15))
        replaced = with_published_tariff(load_scenario_packs(scenario), published, slot)
        assert len([rule for rule in replaced.rules
                    if rule.kind == "tariff_tou" and rule.condition.get("category") == CATEGORY]) == 1
    finally:
        services.close()
