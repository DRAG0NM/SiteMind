"""B30 游标绑定（#241 / #205-4）：游标绑定清单与筛选条件，旧格式直接拒绝。

全部经默认装配（build_services 的真实 Sqlite 存储）走一遍。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, Locator, PlantkgError, Role, Rule
from plantkg.core.ports import AssetRepository, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts

AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))
STALE = "游标已失效，请从第一页重新取"


def _setup():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                      actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-cursor", as_of=AT)
    return services, settings, context


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
    return manifest.version_manifest_id


def _rule(rule_id, kind, subject, **updates):
    base = {"rule_id": rule_id, "kind": kind, "subject_ids": (subject,), "quantity": "temperature", "unit": "degC",
            "value": None, "bounds": {"lower": 23.0, "upper": 25.0},
            "effective_from": AT - timedelta(days=1), "available_at": AT - timedelta(days=1),
            "source_asset_version": "asset://demo-electronics/00000000/process/rule", "locator": Locator(line_start=1),
            "nature": "synthetic", "review_status": "approved", "reviewer": "reviewer"}
    return Rule(**{**base, **updates})


def _seed_candidates(registry, context, tmp_path, count):
    candidate_ids = []
    for index in range(count):
        path = tmp_path / f"leak-{index}.md"
        path.write_text(f"主管压力 10 分钟内下降超过 0.{(index + 1) * 3:02d} MPa、且空压机仍在持续供气时，按泄漏处理。",
                        encoding="utf-8")
        asset = registry.resolve(AssetRepository).ingest(context, str(path), "process_doc", AT, nature="synthetic")
        candidate_ids.extend(registry.tools["extract_rules"].handler(
            context, asset_version_id=asset.asset_version_id).data["candidate_ids"])
    return candidate_ids


def _legacy_review_cursor():
    return base64.urlsafe_b64encode(json.dumps([-3, -0.0, "rule-legacy"]).encode("utf-8")).decode("ascii").rstrip("=")


def test_old_format_cursors_are_rejected(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        _seed_candidates(registry, context, tmp_path, 3)
        manifest_id = _activate_rules(registry, [_rule("temp-admin", "zone_temperature_limit",
                                                      f"entity://{settings.plant_id}/zone/admin")], "基线规则")
        bound = context.model_copy(update={"version_manifest_id": manifest_id})
        for cursor in ("5", _legacy_review_cursor()):
            with pytest.raises(PlantkgError) as error:
                registry.tools["next_review_batch"].handler(bound, cursor=cursor)
            assert error.value.code == ErrorCode.INVALID_REQUEST and STALE in str(error.value)
            with pytest.raises(PlantkgError) as error:
                registry.tools["lookup_rules"].handler(bound, kind="zone_temperature_limit", cursor=cursor)
            assert error.value.code == ErrorCode.INVALID_REQUEST and STALE in str(error.value)
    finally:
        services.close()


def test_review_cursor_binds_manifest_and_ignores_page_size(tmp_path):
    services, settings, context = _setup()
    try:
        registry = services.registry
        candidate_ids = _seed_candidates(registry, context, tmp_path, 5)
        manifest_id = _activate_rules(registry, [_rule("temp-admin", "zone_temperature_limit",
                                                      f"entity://{settings.plant_id}/zone/admin")], "基线规则")
        bound = context.model_copy(update={"version_manifest_id": manifest_id})
        first = registry.tools["next_review_batch"].handler(bound, page_size=2)
        assert len(first.data["items"]) == 2 and first.page.next_cursor
        # 同一清单下换 page_size 翻页仍有效：游标不绑定分页大小
        second = registry.tools["next_review_batch"].handler(bound, cursor=first.page.next_cursor, page_size=3)
        seen = {item["candidate_id"] for item in first.data["items"]} | {item["candidate_id"] for item in second.data["items"]}
        assert seen == set(candidate_ids) and second.page.next_cursor is None

        # 清单变化后旧游标失效
        newer = _activate_rules(registry, [_rule("temp-cleanroom", "zone_temperature_limit",
                                                 f"entity://{settings.plant_id}/zone/cleanroom")], "新清单")
        assert newer != manifest_id
        changed = bound.model_copy(update={"version_manifest_id": newer})
        with pytest.raises(PlantkgError) as error:
            registry.tools["next_review_batch"].handler(changed, cursor=first.page.next_cursor)
        assert error.value.code == ErrorCode.INVALID_REQUEST and STALE in str(error.value)
    finally:
        services.close()


def test_lookup_cursor_binds_filters_and_pages_remaining_rules():
    services, settings, context = _setup()
    try:
        registry = services.registry
        subject = f"entity://{settings.plant_id}/zone/admin"
        rules = [_rule(f"temp-{index}", "zone_temperature_limit", subject) for index in range(4)]
        rules += [_rule(f"window-{index}", "operation_window", f"entity://{settings.plant_id}/air_compressor/AC-{index + 1}",
                        quantity="operation_start", unit="h", bounds={"lower": 0, "upper": 1}) for index in range(2)]
        manifest_id = _activate_rules(registry, rules, "查询规则")
        bound = context.model_copy(update={"version_manifest_id": manifest_id})
        first = registry.tools["lookup_rules"].handler(bound, kind="zone_temperature_limit", page_size=2)
        assert len(first.data["items"]) == 2 and first.page.next_cursor
        second = registry.tools["lookup_rules"].handler(
            bound, kind="zone_temperature_limit", cursor=first.page.next_cursor, page_size=2)
        first_ids = {item["rule_id"] for item in first.data["items"]}
        second_ids = {item["rule_id"] for item in second.data["items"]}
        assert len(second.data["items"]) == 2 and first_ids.isdisjoint(second_ids)

        # 筛选条件变化后旧游标失效
        for filters in ({"kind": "operation_window"}, {"kind": "zone_temperature_limit", "subject_id": subject}):
            with pytest.raises(PlantkgError) as error:
                registry.tools["lookup_rules"].handler(bound, cursor=first.page.next_cursor, **filters)
            assert error.value.code == ErrorCode.INVALID_REQUEST and STALE in str(error.value)
    finally:
        services.close()
