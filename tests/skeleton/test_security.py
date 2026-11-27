"""ctx 失败关闭：无 ctx、未知 ctx、伪造 ctx、过期 ctx（ADR-0002），以及 MCP 端到端。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.bootstrap import build_services
from plantkg.core import contracts as c
from plantkg.core.config import load_settings
from plantkg.core.ports import RunSessionStore
from plantkg.mcp_server import create_mcp_server

READER = c.Actor(actor_id="u-1", roles=("viewer", "reviewer"))
AS_OF = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)


def call(services, tool, ctx="", **params):
    binding = services.registry.tools[tool]
    return services.guard.call(binding.spec, binding.handler, ctx, params)


def session(services, ttl_s=3600):
    store = services.registry.resolve(RunSessionStore)
    return store.register(READER, "demo-electronics", AS_OF, (), ttl_s, "ar-test-1", "tr-test-1")


def test_write_without_ctx_rejected(services):
    assert call(services, "run_day", plan_date="2026-07-01").startswith("PLANTKG_E_NO_CTX:")


@pytest.mark.parametrize("ctx", ["0" * 32, "f" * 32, "not-a-ctx", "x" * 400])
def test_unknown_or_forged_ctx_rejected(services, ctx):
    for tool, params in (("run_day", {"plan_date": "2026-07-01"}), ("get_plan", {"plan_id": "plan-20260701"})):
        text = call(services, tool, ctx=ctx, **params)
        assert text.startswith("PLANTKG_E_UNKNOWN_CTX:") and ctx not in text


def test_expired_ctx_rejected(services):
    s = session(services, ttl_s=1)
    services.resolver._clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=5)
    assert call(services, "run_day", ctx=s.ctx, plan_date="2026-07-01").startswith("PLANTKG_E_CTX_EXPIRED:")


def test_read_without_ctx_is_degraded(services):
    text = call(services, "get_plan", plan_id="plan-20260701")
    assert text.startswith(c.NOTICE_NO_CTX_PREFIX)
    result = c.ToolResult.model_validate_json(text.split("\n", 1)[1])
    assert result.business_status == "degraded" and result.data["plan_id"] == "plan-20260701"


def test_valid_ctx_write_goes_to_job_without_echo(services):
    s = session(services)
    text = call(services, "run_day", ctx=s.ctx, plan_date="2026-07-01")
    result = c.ToolResult.model_validate_json(text)
    assert result.business_status == "done" and result.job_id and s.ctx not in text  # Fake 队列即时完成，40 秒内直接返回结果
    assert result.data["result_ref"].startswith("plan://")
    again = c.ToolResult.model_validate_json(call(services, "run_day", ctx=s.ctx, plan_date="2026-07-01"))
    assert again.job_id == result.job_id  # 同参数幂等
    done = c.ToolResult.model_validate_json(call(services, "get_job", ctx=s.ctx, job_id=result.job_id))
    assert done.business_status == "done" and done.data["result_ref"].startswith("plan://")


def test_handler_echoing_ctx_is_blocked(services):
    s = session(services)
    leaky = lambda context, **_: c.ToolResult(ok=True, business_status="done", data={"note": s.ctx})  # noqa: E731
    text = services.guard.call(c.TOOL_SPECS[0], leaky, s.ctx, {})
    assert text.startswith("PLANTKG_E_INTERNAL:") and s.ctx not in text


def test_handler_exception_is_contained(services):
    def boom(context, **_):
        raise RuntimeError("secret detail")

    text = services.guard.call(c.TOOL_SPECS[0], boom, "", {"source_uri": "x"})
    assert "secret detail" not in text


def test_default_ctx_only_when_enabled_in_mock(config_dir, edit_config):
    edit_config("defaults.yaml", lambda d: d["security"]["default_ctx"].update(enabled=True))
    services = build_services(load_settings(profile="mock", config_dir=config_dir))
    result = c.ToolResult.model_validate_json(call(services, "run_day", plan_date="2026-07-01"))
    assert result.business_status == "done"


def test_mcp_tools_declare_optional_ctx(services):
    from fastmcp import Client

    async def run():
        async with Client(create_mcp_server(services)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            schema = tools["plantkg_run_day"].input_schema
            assert "ctx" in schema["properties"] and "ctx" not in schema.get("required", [])
            assert schema["required"] == ["plan_date"]
            read = await client.call_tool("plantkg_get_plan", {"plan_id": "plan-20260701"})
            write = await client.call_tool("plantkg_run_day", {"plan_date": "2026-07-01"}, raise_on_error=False)
            return read.content[0].text, write.content[0].text

    read_text, write_text = asyncio.run(run())
    assert read_text.startswith(c.NOTICE_NO_CTX_PREFIX)
    assert write_text.startswith("PLANTKG_E_NO_CTX:")
