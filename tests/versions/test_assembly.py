from plantkg.bootstrap import build_services
from plantkg.core.actions import ACTOR_HEADER, TOKEN_HEADER
from plantkg.core.config import load_settings
from plantkg.core.contracts import ToolResult
from plantkg.core.ports import EvidenceSink, JobRunner, PendingActionStore, RunSessionStore, RunStore, VersionStore
from tests.versions.conftest import REVIEWER, context


def test_storage_enabled_installs_real_ports_and_confirm_endpoint():
    from starlette.testclient import TestClient
    from plantkg.api import create_app

    settings = load_settings(profile="mock", overrides={"storage.enabled": True, "actions.reviewers": ["u-reviewer"]})
    services = build_services(settings)
    registry = services.registry
    for port in (VersionStore, RunStore, EvidenceSink, JobRunner, PendingActionStore, RunSessionStore):
        assert port not in registry.fallbacks
    applied = []
    # 默认装配已登记 B30 default + B32 真实 publish 处理器；这里直接替换成探针，验证确认端点路由。
    registry.action_handlers["publish"] = type("H", (), {"kind": "publish", "apply": lambda self, a, u: applied.append(u.actor_id) or ToolResult(ok=True, business_status="done")})()
    store = registry.resolve(PendingActionStore)
    action = store.propose("publish", {"target_ref": "cand-1"}, context(plant_id=settings.plant_id, idempotency_key="p-1"))
    token = services.version_run.operator.token
    with TestClient(create_app(services)) as client:
        assert client.get("/api/v1/actions").json()["items"][0]["action_id"] == action.action_id
        assert client.get("/api/v1/versions").json()["items"] == []
        url = f"/api/v1/actions/{action.action_id}/confirm"
        body = {"reason": "确认", "expected_revision": 0, "idempotency_key": "d-1"}
        assert client.post(url, json=body).status_code == 403
        assert client.post(url, json=body, headers={TOKEN_HEADER: "wrong", ACTOR_HEADER: "u-reviewer"}).status_code == 403
        assert client.post(url, json=body, headers={TOKEN_HEADER: token, ACTOR_HEADER: "intruder"}).status_code == 403
        ok = client.post(url, json=body, headers={TOKEN_HEADER: token, ACTOR_HEADER: "u-reviewer"})
        assert ok.status_code == 200 and ok.json()["status"] == "confirmed" and applied == ["u-reviewer"]
        assert client.post(url, json=body, headers={TOKEN_HEADER: token, ACTOR_HEADER: "u-reviewer"}).json()["revision"] == 1
        assert client.post(url, json={"reason": ""}, headers={TOKEN_HEADER: token, ACTOR_HEADER: "u-reviewer"}).status_code == 400
        assert client.get("/api/v1/jobs/job-missing?wait=1").status_code == 404
        assert client.get("/api/v1/runs").json()["items"] == []


def test_needs_repair_action_is_listed_by_status_after_persist_failure():
    """执行已生效、终态决定落库失败的动作标 needs_repair，能经 /api/v1/actions 按状态查到（#204）。"""
    from starlette.testclient import TestClient
    from plantkg.api import create_app

    settings = load_settings(profile="mock", overrides={"storage.enabled": True, "actions.reviewers": ["u-reviewer"]})
    services = build_services(settings)
    try:
        registry = services.registry
        # B30/B32 已登记真实 publish 执行函数：这里直接替换探针，避免重复登记（#204 用例适配）
        registry.action_handlers["publish"] = type("H", (), {
            "kind": "publish", "apply": lambda self, a, u: ToolResult(ok=True, business_status="done")})()
        store = registry.resolve(PendingActionStore)
        action = store.propose("publish", {"target_ref": "cand-repair"},
                               context(plant_id=settings.plant_id, idempotency_key="repair-p-1"))
        services.storage.writer.submit(lambda con: con.execute(
            "CREATE TRIGGER fail_repair_persist BEFORE UPDATE ON pending_actions WHEN NEW.status='confirmed' "
            "BEGIN SELECT RAISE(ABORT,'injected persistence failure'); END"))
        headers = {TOKEN_HEADER: services.version_run.operator.token, ACTOR_HEADER: "u-reviewer"}
        with TestClient(create_app(services)) as client:
            refused = client.post(f"/api/v1/actions/{action.action_id}/confirm", headers=headers,
                                  json={"reason": "确认", "expected_revision": 0, "idempotency_key": "repair-d-1"})
            assert refused.status_code == 500
            listed = client.get("/api/v1/actions", params={"status": "needs_repair"}).json()["items"]
            assert [item["action_id"] for item in listed] == [action.action_id]
            assert client.get("/api/v1/actions", params={"status": "needs_repair", "kind": "publish"}).json()["items"]
            assert client.get("/api/v1/actions", params={"status": "needs_repair", "kind": "work_order"}).json()["items"] == []
    finally:
        services.close()


def test_storage_disabled_keeps_fakes():
    services = build_services(load_settings(profile="mock"))
    try:
        assert RunSessionStore in services.registry.fallbacks and services.version_run is None
    finally:
        services.close()


def test_manifest_published_through_services_is_resolved_for_runs():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.registry.resolve(VersionStore)
        graph = services.storage.graph
        ref = graph.prepare("plant", "<urn:a> <urn:b> <urn:c> .")
        with services.storage.writer.read_only() as con:
            checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
        m = versions.prepare({"ontology": {"ref": ref, "content_hash": "sha256:" + checksum}}, "本体")
        versions.check_integrity(m.version_manifest_id)
        versions.activate(None, m.version_manifest_id, REVIEWER, "发布")
        ctx = context(plant_id=settings.plant_id, version_manifest_id=m.version_manifest_id)
        assert len(graph.read_named_graph(ctx, "plant", "triples", {})[0]) == 1
    finally:
        services.close()
