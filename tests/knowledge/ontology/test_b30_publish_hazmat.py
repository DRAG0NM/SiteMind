"""B30 发布期消费端校验的端到端回归：走 propose_publish，断言热模型没有的区域被拒（#205 追加第 3 条）。

直接调 consumer_mismatch 的用例覆盖不到 tools.py 漏传 zone_of：漏传时 zone:hazmat 会直接取区域名并放行。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ErrorCode, Locator, PlantkgError, Role, Rule
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts

AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
REVIEWER = Actor(actor_id="reviewer", roles=(Role.reviewer,))


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


def test_hazmat_temperature_rule_is_rejected_by_propose_publish(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        settings = services.settings
        _activate_baseline(registry, settings.plant_id)
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, channel="offline",
                          actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="b30-hazmat", as_of=AT)
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
