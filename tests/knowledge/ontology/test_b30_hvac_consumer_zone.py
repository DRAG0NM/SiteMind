"""暖通消费端区域钩子（#241，原 #225）：暖通不再往 router.entity_zone 追加过滤。

暖通安装时登记自己的 hvac.consumer_zone，router.entity_zone 回到图谱原义；默认装配与调换包装载顺序
（hvac 在 global_graph 之前）都要能解析。发布期选哪个钩子由规则类型决定，端到端用例照公开口验证。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, Locator, PlantkgError, Rule, Role
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts

AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))


def _settings_with_hvac_before_graph():
    """把暖通放到 global_graph 之前：安装时取不到图谱钩子，必须调用时再取。"""
    packs = list(load_settings(profile="mock").packs)
    packs.remove("hvac")
    packs.insert(packs.index("global_graph"), "hvac")
    return load_settings(profile="mock", overrides={"storage.enabled": True, "packs": packs})


def _assert_zone_hooks(registry):
    graph_zone = registry.hooks_for("router.entity_zone")[-1]
    hvac_zone = registry.hooks_for("hvac.consumer_zone")[-1]
    # 图谱原义：设备节点按所在区域解析；区域主体本身不是图谱节点
    assert graph_zone("CR-AHU1") == "cleanroom"
    assert graph_zone("HZ-EXF1") == "hazmat"
    assert graph_zone("zone:hazmat") is None
    # 热模型区域过滤只在暖通自己的钩子上：hazmat 没热模型解析不到，热模型有的区域照常
    assert hvac_zone("CR-AHU1") == "cleanroom"
    assert hvac_zone("HZ-EXF1") is None
    assert hvac_zone("zone:admin") == "admin"


def test_zone_hooks_in_default_assembly():
    """默认装配（图谱钩子在前）下两个钩子各按自己的语义解析。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        _assert_zone_hooks(services.registry)
    finally:
        services.close()


def test_zone_hooks_with_hvac_loaded_before_global_graph():
    """调换包装载顺序后语义不变：暖通钩子在调用时取图谱钩子，不依赖安装时的缓存。"""
    services = build_services(_settings_with_hvac_before_graph())
    try:
        _assert_zone_hooks(services.registry)
    finally:
        services.close()


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


def _review(registry, context, candidate_id, subject_id):
    revision = {"subject_id": subject_id, "effective_from": AT.isoformat()}
    decision = registry.tools["propose_review_decision"].handler(
        context, review_id=candidate_id, decision="revise", revision=revision, rationale="测试复核")
    key = "review-" + candidate_id + "-" + json.dumps(revision, sort_keys=True, ensure_ascii=False)
    registry.resolve(PendingActionStore).confirm(decision.data["action_id"], REVIEWER, "确认复核", 0, key)


def _publish(registry, context, candidate_id):
    publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="测试发布")
    registry.resolve(PendingActionStore).confirm(publish.data["action_id"], REVIEWER, "确认发布", 0,
                                                 f"publish-{candidate_id}")


def test_swapped_pack_order_still_rejects_hazmat_temperature_rule(tmp_path):
    """调换包装载顺序后，hazmat 温度规则仍在 propose_publish 被 consumer_mismatch 拦下，admin 区域照常发布。"""
    services = build_services(_settings_with_hvac_before_graph())
    try:
        registry = services.registry
        settings = services.settings
        _activate_baseline(registry, settings.plant_id)
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-hvac-zone", as_of=AT)
        document = tmp_path / "comfort.md"
        document.write_text("办公室温度24±1 ℃。", encoding="utf-8")
        asset = registry.resolve(AssetRepository).ingest(context, str(document), "process_doc", AT, nature="synthetic")
        candidate = registry.tools["extract_rules"].handler(
            context, asset_version_id=asset.asset_version_id).data["candidate_ids"][0]

        # 图谱写得出 hazmat、热模型没有：发布期拒绝，而不是发布成功但静默不生效
        _review(registry, context, candidate, f"entity://{settings.plant_id}/zone/hazmat")
        with pytest.raises(PlantkgError) as error:
            registry.tools["propose_publish"].handler(context, candidate_id=candidate, rationale="发布温度规则")
        assert error.value.code == ErrorCode.PRECONDITION and "consumer_mismatch" in str(error.value)
        assert "不能映射到区域" in str(error.value)

        # 热模型里有的区域照常发布，确认校验没有把整类温度规则一起拒掉
        _review(registry, context, candidate, f"entity://{settings.plant_id}/zone/admin")
        _publish(registry, context, candidate)
        assert registry.resolve(VersionStore).active().components.get("rules") is not None
    finally:
        services.close()
