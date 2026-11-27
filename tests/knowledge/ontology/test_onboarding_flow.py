"""B30 入库流程：缺依赖工具时抛业务错误，不让 KeyError 漏给调用方（#205）。"""
from datetime import datetime, timezone

import pytest

from plantkg.core.contracts import Actor, Context, ErrorCode, PlantkgError, Role, ToolResult
from plantkg.core.registry import Registry
from plantkg.knowledge.rules.onboarding_flow import install_flow


def _context():
    return Context(plant_id="demo-electronics", industry_pack="manufacturing", channel="offline",
                   actor=Actor(actor_id="operator", roles=(Role.operator,)), trace_id="flow",
                   as_of=datetime(2026, 7, 1, tzinfo=timezone.utc))


def test_process_onboarding_missing_tool_raises_dependency_unavailable():
    registry = Registry()
    install_flow(registry)
    process = registry.tools["process_onboarding"].handler
    with pytest.raises(PlantkgError) as missing_extract:
        process(_context(), asset_version_id="asset-1")
    assert missing_extract.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "extract_rules" in missing_extract.value.message

    registry.register_tool("extract_rules", lambda context, asset_version_id: ToolResult(
        ok=True, business_status="done", data={"routing": [{"stage": "agent", "candidate_id": "cand-1"}]}))
    with pytest.raises(PlantkgError) as missing_investigate:
        process(_context(), asset_version_id="asset-1")
    assert missing_investigate.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "investigate_onboarding" in missing_investigate.value.message
