"""第四轮 #242 并入的 B30 小项：同主体钩子、厂区配置读取失败、lookup_rules 游标绑定 as_of。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.knowledge.rules.pack import configured_tariff_category
from tests.knowledge.ontology.test_b30_cursor_binding import AT, STALE, _activate_rules, _rule, _setup


def test_same_access_hook_treats_equivalent_subjects_as_one_rule():
    services, settings, _context = _setup()
    try:
        same_access = services.registry.hooks_for("knowledge.rules.same_access")[-1]
        main = _rule("tariff-main", "tariff_tou", f"entity://{settings.plant_id}/grid_connection/MAIN")
        grid = _rule("tariff-grid", "tariff_tou", f"entity://{settings.plant_id}/grid/GRID")
        other = _rule("tariff-zone", "tariff_tou", f"entity://{settings.plant_id}/zone/admin")
        assert same_access(main, grid) is True and same_access(main, other) is False
    finally:
        services.close()


def test_unreadable_site_config_is_a_domain_error(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("tariff: [unclosed", encoding="utf-8")
    for site in (str(tmp_path / "missing.yaml"), str(broken)):
        settings = SimpleNamespace(section=lambda name, site=site: {"site": site})
        with pytest.raises(PlantkgError) as error:
            configured_tariff_category(settings)
        assert error.value.code == ErrorCode.DOMAIN_INVALID and "厂区配置不可读" in error.value.message


def test_lookup_rules_cursor_is_stale_after_as_of_changes():
    services, settings, context = _setup()
    try:
        registry = services.registry
        rules = [_rule(f"temp-{index}", "zone_temperature_limit", f"entity://{settings.plant_id}/zone/z{index}")
                 for index in range(12)]
        bound = context.model_copy(update={"version_manifest_id": _activate_rules(registry, rules, "游标基线")})
        lookup = registry.tools["lookup_rules"].handler
        first = lookup(bound, page_size=10)
        cursor = first.page.next_cursor
        assert cursor and len(lookup(bound, cursor=cursor, page_size=10).data["items"]) == 2
        with pytest.raises(PlantkgError) as error:
            lookup(bound.model_copy(update={"as_of": AT + timedelta(hours=1)}), cursor=cursor, page_size=10)
        assert STALE in error.value.message
    finally:
        services.close()


def test_b32_correction_with_equivalent_subject_leaves_one_tariff_rule(tmp_path):
    """C.7（#252）：默认装配下，B32 纠错候选以 grid/GRID 改写已发布的 grid_connection/MAIN 电价规则，发布后清单只留一条。"""
    from plantkg.core.contracts import Actor, Context, Role, Rule
    from plantkg.core.ports import PendingActionStore, VersionStore
    from plantkg.experts.energy.planner import load_scenario_packs, with_published_tariff
    from plantkg.knowledge.rules.publisher import RuleArtifacts
    from plantkg.world.scenario.script import load_scenario
    from tests.knowledge.ontology import test_b30_equivalent_subject_publish as base

    services = base.build_services(base.load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry, settings = services.registry, services.settings
        base._activate_rules(registry, [base._baseline_rule(settings.plant_id)], "基线规则")
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="opt4-same-access", as_of=base.AT)
        _asset, first = base._extract(registry, context, base.TARIFF_DOC)
        base._publish_tariff(registry, context, base._calendar(registry, context, tmp_path), first,
                             f"entity://{settings.plant_id}/grid_connection/MAIN", "first")
        versions = registry.resolve(VersionStore)
        artifacts = RuleArtifacts(versions.writer, settings.plant_id)
        published = [Rule.model_validate(item) for item in artifacts.load(versions.active().components["rules"])["rules"]]
        tariff = next(rule for rule in published if rule.kind == "tariff_tou")
        corrected = tariff.model_copy(update={
            "rule_id": "b32-tariff-grid", "subject_ids": (f"entity://{settings.plant_id}/grid/GRID",),
            "review_status": "approved", "reviewer": base.REVIEWER.actor_id, "rule_version": tariff.rule_version + 1})
        reviewer_ctx = context.model_copy(update={"actor": base.REVIEWER, "version_manifest_id": versions.active().version_manifest_id})
        created = registry.tools["propose_candidates_evo"].handler(
            reviewer_ctx, kind="knowledge", trigger={"event_ticket": "t-tariff-grid", "rule": corrected.model_dump(mode="json")})
        assert created.ok, created.to_agent_text()
        publish = registry.tools["propose_publish"].handler(
            reviewer_ctx, candidate_id=created.data["candidate_id"], rationale="等价主体改写电价规则")
        assert publish.ok, publish.to_agent_text()
        registry.resolve(PendingActionStore).confirm(publish.data["action_id"], base.REVIEWER, "确认发布", 0, "opt4-tariff-grid")
        active = [Rule.model_validate(item) for item in artifacts.load(versions.active().components["rules"])["rules"]]
        assert [rule.rule_id for rule in active if rule.kind == "tariff_tou"] == ["b32-tariff-grid"]
        scenario = load_scenario(base.ROOT / "configs/scenarios/factory_demo_day.yaml")
        slot = base.Interval(start=scenario.instant("08:00"), end=scenario.instant("08:00") + timedelta(minutes=15))
        replaced = with_published_tariff(load_scenario_packs(scenario), active, slot)  # 不再报“已发布电价规则存在重叠”
        assert len([rule for rule in replaced.rules if rule.kind == "tariff_tou"
                    and rule.condition.get("category") == base.CATEGORY]) == 1
    finally:
        services.close()
