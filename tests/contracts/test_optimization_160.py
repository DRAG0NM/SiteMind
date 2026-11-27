"""#160：契约工具目录、OpenAPI 必填查询参数、端口扩展与健康口径。

全部经默认装配（build_services + defaults.yaml 的 packs）走一遍：工具清单取自真实注册表，
HTTP 走 create_app 的真实路由，健康取自 /health 同一个渲染函数。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from plantkg.api import _health, create_app
from plantkg.bootstrap import build_services, check_tool_catalog
from plantkg.core import contracts as c
from plantkg.core import ports
from plantkg.core.config import load_settings
from plantkg.core.contracts import ToolSpec
from plantkg.core.registry import Registry, RegistryError, ToolBinding
from tests.contracts.conftest import CONTRACTS, load_json

TOOLS = {spec.name for spec in c.TOOL_SPECS}
EXPOSED = {spec.name for spec in c.TOOL_SPECS if spec.exposed_to_agent}


@pytest.fixture(scope="module")
def assembled(tmp_path_factory):
    root = tmp_path_factory.mktemp("opt160")
    settings = load_settings(overrides={
        "storage.enabled": True,
        "storage.sqlite": str(root / "catalog.db"),
        "storage.graph": str(root / "graph"),
        "storage.series": str(root / "series"),
    })
    services = build_services(settings)
    try:
        yield services
    finally:
        services.close()


def test_runtime_exposed_tools_are_in_contract(assembled):
    runtime = {name for name, binding in assembled.registry.tools.items() if binding.spec.exposed_to_agent}
    assert runtime <= TOOLS, f"运行时暴露工具未进契约目录：{sorted(runtime - TOOLS)}"
    # 六个 B25/B30 扩展工具原由 pack.yaml 声明，v0.8 起必须出现在契约目录
    assert {"trace_impact", "find_sources", "safety_tags", "priority_rules", "playbooks", "prepare_tariff_review"} <= TOOLS


def test_contract_tools_resolve_or_are_optional(assembled):
    missing = EXPOSED - set(assembled.registry.tools) - {spec.name for spec in c.TOOL_SPECS if spec.optional}
    assert not missing, f"契约工具在默认装配里既未登记也未标 optional：{sorted(missing)}"


def test_deleting_a_contract_tool_breaks_the_check(assembled, monkeypatch):
    monkeypatch.setattr(c, "TOOL_SPECS", tuple(spec for spec in c.TOOL_SPECS if spec.name != "trace_impact"))
    with pytest.raises(RegistryError, match="未在契约 TOOL_SPECS 中声明"):
        check_tool_catalog(assembled.registry)


def test_undeclared_exposed_tool_breaks_the_check(assembled):
    ghost = ToolSpec(name="ghost_tool", plane="knowledge", provider="B99", mode="read", description="未声明")
    assembled.registry.tools["ghost_tool"] = ToolBinding(ghost, lambda **_: None)
    try:
        with pytest.raises(RegistryError, match="未在契约 TOOL_SPECS 中声明"):
            check_tool_catalog(assembled.registry)
    finally:
        assembled.registry.tools.pop("ghost_tool", None)


def test_contract_tools_must_be_registered(assembled):
    with pytest.raises(RegistryError, match="未在本次装配中登记"):
        check_tool_catalog(Registry(), require_all=True)


def test_openapi_marks_required_query():
    openapi = load_json(CONTRACTS / "openapi" / "plantkg.openapi.json")
    view = {p["name"]: p for p in openapi["paths"]["/api/v1/runs/{run_id}/view"]["get"]["parameters"]}
    ledger = {p["name"]: p for p in openapi["paths"]["/api/v1/runs/{run_id}/ledger"]["get"]["parameters"]}
    assert view["at"]["required"] is True
    assert ledger["kind"]["required"] is True
    assert ledger["cursor"]["required"] is False and ledger["page_size"]["required"] is False


def test_missing_required_query_is_400(assembled):
    client = TestClient(create_app(assembled))
    try:
        for url in ("/api/v1/runs/nope/ledger", "/api/v1/runs/nope/view"):
            response = client.get(url)
            assert response.status_code == 400, (url, response.text)
            assert response.json()["error"]["code"] == c.ErrorCode.INVALID_REQUEST.value
    finally:
        client.close()


def test_health_fakes_only_cover_test_doubles(assembled):
    deps = _health(assembled).dependencies
    faked = {name for name, value in deps.items() if value == "fake"}
    for real_default in ("trial", "propose_publish", "KnowledgePublisher", "action_handler publish", "action_handler review_decision"):
        assert f"fake.{real_default}" not in faked, f"真实默认可替换实现被标成 Fake：{real_default}"
    assert "fake.run_day" in faked, "plantkg/testing 的 Fake 仍应标 fake"
    assert not any(name.startswith("placeholder.") for name in deps), f"存储装配下不应有未实现占位：{sorted(deps)}"


def test_model_builder_port_covers_planner_extension():
    port = ports.ModelBuilder
    assert {"pv_available", "total_load", "add_var"} <= set(dir(port))
    declared = inspect.signature(port.add_var).parameters
    assert {"index", "scalar", "action"} <= set(declared)
    published = load_json(CONTRACTS / "ports.json")["ports"]["ModelBuilder"]
    assert {"pv_available", "total_load"} <= set(published["methods"])
    assert {"index", "scalar", "action"} <= set(published["methods"]["add_var"]["params"])
    # 契约只追加：实现里的参数一个不少
    from plantkg.decision.planner.builder import SharedModelBuilder

    implemented = inspect.signature(SharedModelBuilder.add_var).parameters
    assert set(declared) <= set(implemented)


def test_route_spec_rejects_required_query_outside_query():
    with pytest.raises(ValidationError):
        c.RouteSpec(method="GET", path="/x", summary="x", provider="B07", response="StateSnapshot", required_query=("at",))
