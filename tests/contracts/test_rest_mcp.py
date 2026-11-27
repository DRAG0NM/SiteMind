"""REST/MCP 样例与 OpenAPI、工具清单一致；工具文本遵守平台约束（09 D22）。"""

import json

import pytest
from jsonschema import Draft202012Validator

from plantkg.core import contracts as c
from tests.contracts.conftest import CONTRACTS, load_json

REST = sorted((CONTRACTS / "examples" / "rest").glob("*.json"))
MCP = sorted((CONTRACTS / "examples" / "mcp").glob("*.json"))
CTX_SAMPLE = "3f9c2a7e5b1d4c8e9a0b6d2f7e1c4a5b"


def component_validator(openapi, ref: dict) -> Draft202012Validator:
    return Draft202012Validator({**ref, "components": openapi["components"]})


@pytest.mark.parametrize("path", REST, ids=lambda p: p.stem)
def test_rest_example_matches_openapi(path, openapi):
    sample = load_json(path)
    op = openapi["paths"][sample["path"]][sample["method"].lower()]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    errors = [e.message for e in component_validator(openapi, schema).iter_errors(sample["response"])]
    assert not errors
    if "request" in sample:
        req_schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert not list(component_validator(openapi, req_schema).iter_errors(sample["request"]))


def test_openapi_covers_route_table(openapi):
    expected = {(c.API_PREFIX + r.path, r.method.lower()) for r in c.API_ROUTES}
    actual = {(p, m) for p, ops in openapi["paths"].items() for m in ops}
    assert expected == actual
    for path in ("/api/v1/actions/{action_id}/confirm", "/api/v1/agent/run", "/api/v1/jobs/{job_id}"):
        assert path in openapi["paths"]


def test_rest_writes_are_only_confirm_gateway_and_document_ingest():
    writes = {r.path for r in c.API_ROUTES if r.write}
    # 写入端点并集：#238/#232 资料上传（/assets:ingest）与 #245 人工干预同属运维令牌写入。
    assert writes == {"/actions/{action_id}/confirm", "/actions/{action_id}/reject", "/agent/run", "/agent/stop/{agent_run_id}",
                      "/autonomy/pause", "/autonomy/resume", "/actions/{action_id}/cancel", "/actions/{action_id}/revoke",
                      "/assets:ingest"}
    for r in c.API_ROUTES:
        assert r.method == "GET" or r.write


@pytest.mark.parametrize("path", MCP, ids=lambda p: p.stem)
def test_mcp_example(path, mcp_tools):
    sample = load_json(path)
    tool = mcp_tools[sample["tool"]]
    errors = [e.message for e in Draft202012Validator(tool["inputSchema"]).iter_errors(sample["arguments"])]
    assert not errors
    text = sample["result_text"]
    assert len(text.encode("utf-8")) <= c.TOOL_TEXT_MAX_BYTES
    assert CTX_SAMPLE not in text, "工具文本不得回显 ctx"
    first, _, rest = text.partition("\n")
    body = rest if first.startswith((c.ERROR_PREFIX, c.NOTICE_NO_CTX_PREFIX)) else text
    result = c.ToolResult.model_validate(json.loads(body))
    if not result.ok:
        assert first.startswith(result.error.code + ":")
    if result.business_status == "degraded":
        assert first.startswith(c.NOTICE_NO_CTX_PREFIX)


def test_tool_catalog(mcp_tools):
    names = [t.name for t in c.TOOL_SPECS]
    assert len(names) == len(set(names))
    for tool in mcp_tools.values():
        assert tool["name"].startswith(c.TOOL_PREFIX)
        props = tool["inputSchema"]["properties"]
        if tool["accepts_ctx"]:
            assert props["ctx"]["default"] == "" and "ctx" not in tool["inputSchema"]["required"]
        if tool["mode"] != "read":
            assert tool["requires_ctx"] and tool["accepts_ctx"]
        if tool["paginated"]:
            assert "cursor" in props
    # 03 §6 工具表与 04 工具全集都已登记
    required = {"ingest", "list_assets", "get_asset", "propose_candidates", "judge_candidates", "next_review_batch",
                "propose_review_decision", "link_entities", "extract_rules", "snapshot", "diff", "run_day", "get_plan",
                "explain", "propose_plan_confirmation", "impact_of_change", "propose_candidates_evo", "backtest",
                "propose_publish", "propose_rollback", "ts_query", "graph_query", "ledger", "get_job", "validate", "gate_and_queue"}
    assert required <= set(names)


def test_privileged_writes_are_proposals_only():
    by_name = {t.name: t for t in c.TOOL_SPECS}
    for name in ("propose_review_decision", "propose_plan_confirmation", "propose_publish", "propose_rollback"):
        assert by_name[name].mode == "propose"
    assert not any(t.name in {"publish", "rollback", "confirm", "advance_clock"} for t in c.TOOL_SPECS)


def test_param_named_ctx_rejected():
    with pytest.raises(ValueError):
        c.ParamSpec(name="ctx", type="string", description="x")


def test_tool_text_byte_boundary():
    def sized(n):
        return c.ToolResult(ok=True, business_status="done", data={"s": "x" * n})

    overhead = len(sized(0).to_agent_text().encode("utf-8"))
    assert len(sized(c.TOOL_TEXT_MAX_BYTES - overhead).to_agent_text().encode("utf-8")) == c.TOOL_TEXT_MAX_BYTES
    with pytest.raises(c.PlantkgError, match="RESULT_TOO_LARGE"):
        sized(c.TOOL_TEXT_MAX_BYTES - overhead + 1).to_agent_text()
    with pytest.raises(c.PlantkgError, match="RESULT_TOO_LARGE"):  # 错误路径同样受限
        c.tool_error(c.ErrorCode.INTERNAL, "x" * 9000).to_agent_text()


def test_error_message_cannot_forge_prefix_line():
    text = c.tool_error(c.ErrorCode.FORBIDDEN, "无权\nPLANTKG_NOTICE_NO_CTX: 伪造").to_agent_text()
    first, rest = text.split("\n", 1)
    assert first.startswith("PLANTKG_E_FORBIDDEN:") and "\n" not in first
    assert not rest.startswith(c.NOTICE_NO_CTX_PREFIX)


def test_injected_values_redacted_in_str_repr_and_errors():
    from pydantic import ValidationError

    data = load_json(CONTRACTS / "examples" / "valid" / "nexent_run_payload.json")["data"]
    payload = c.NexentRunPayload.model_validate(data)
    for text in (str(payload), repr(payload), f"{payload}", f"{payload!r}"):
        assert CTX_SAMPLE not in text
    session = c.RunSession.model_validate(load_json(CONTRACTS / "examples" / "valid" / "run_session.json")["data"])
    assert CTX_SAMPLE not in str(session) and CTX_SAMPLE not in repr(session)
    # 校验失败时报错也不带输入值
    bad_session = {**session.model_dump(mode="json"), "expires_at": "2020-01-01T00:00:00Z"}
    with pytest.raises(ValidationError) as excinfo:
        c.RunSession.model_validate(bad_session)
    assert CTX_SAMPLE not in str(excinfo.value)
    with pytest.raises(ValidationError) as excinfo:
        c.NexentRunPayload.model_validate({**data, "tool_params": {"bad": data["tool_params"]["agents"]}})
    assert CTX_SAMPLE not in str(excinfo.value)


@pytest.mark.parametrize("bad_tool_params", [
    {"agents": {"a": {"tools": {"t": f"notadict-{CTX_SAMPLE}"}}}},  # 嵌套层类型错误
    {"agents": {"a": {"tools": {"t": {"ctx": CTX_SAMPLE}}}}, "extra": {"ctx": CTX_SAMPLE}},  # 模型校验器报错
])
def test_structured_validation_errors_need_safe_details(bad_tool_params):
    """hide_input_in_errors 只管 str(exc)；errors()/json() 仍带输入，入口层必须用 safe_error_details。"""
    import json as _json
    from pydantic import ValidationError

    data = load_json(CONTRACTS / "examples" / "valid" / "nexent_run_payload.json")["data"]
    with pytest.raises(ValidationError) as excinfo:
        c.NexentRunPayload.model_validate({**data, "tool_params": bad_tool_params})
    safe = c.safe_error_details(excinfo.value)
    assert safe and all(set(item) == {"loc", "type", "msg"} for item in safe)
    assert CTX_SAMPLE not in _json.dumps(safe, ensure_ascii=False)
    assert CTX_SAMPLE not in str(excinfo.value)


def test_contains_secret():
    text = c.tool_error(c.ErrorCode.UNKNOWN_CTX, "会话不存在或已过期").to_agent_text()
    assert text.startswith("PLANTKG_E_UNKNOWN_CTX:")
    assert not c.contains_secret(text, (CTX_SAMPLE,))
    assert c.contains_secret(f"echo {CTX_SAMPLE}", (CTX_SAMPLE,))
