"""Mock profile 离线启动、健康检查与一条契约查询（B02 验收）。"""

import json
import os
import subprocess
import sys

from plantkg.core import contracts as c
from plantkg.api import _route_path
from tests.skeleton.conftest import ROOT


def test_cli_dry_run_offline():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for key in ("NEXENT_API_KEY", "DASHSCOPE_API_KEY"):
        env.pop(key, None)  # 不需要 Nexent 或真实模型
    out = subprocess.run([sys.executable, "-m", "plantkg", "--profile", "mock", "--dry-run"], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    c.HealthResponse.model_validate(report["health"])
    assert "plantkg_run_day" in report["tools"]
    assert report["packs"] == sorted(p.parent.name for p in (ROOT / "plantkg" / "packs").glob("*/pack.yaml"))


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    health = c.HealthResponse.model_validate(resp.json())
    assert health.profile == "mock" and health.dependencies["nexent"] == "disabled"


def test_contract_query_plan(client):
    resp = client.get("/api/v1/plans/plan-20260701")
    assert resp.status_code == 200
    plan = c.Plan.model_validate(resp.json())
    assert plan.actions and all(a.refs for a in plan.actions)


def test_not_found_uses_error_contract(client):
    resp = client.get("/api/v1/plans/plan-nope")
    assert resp.status_code == 404
    assert c.ErrorResponse.model_validate(resp.json()).error.code == "PLANTKG_E_NOT_FOUND"


def test_unwired_route_says_who_provides_it(client):
    resp = client.get("/api/v1/versions")
    assert resp.status_code == 503
    assert "B04" in resp.json()["error"]["message"]


def test_writes_denied_by_default(client):
    body = {"reason": "x", "expected_revision": 0, "idempotency_key": "k1"}
    assert client.post("/api/v1/actions/act-0001/confirm", json=body).status_code == 403
    assert client.post("/api/v1/agent/run", json={"agent_name": "a", "query": "q"}).status_code == 503


def test_every_contract_route_is_mounted(client):
    paths = {route.path for route in client.app.routes}
    for route in c.API_ROUTES:
        assert _route_path(route.path) in paths


def test_mcp_endpoint_mounted(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"accept": "application/json, text/event-stream"})
    assert resp.status_code != 404
