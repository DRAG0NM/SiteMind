from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from plantkg.api import create_app
from plantkg.bootstrap import _seed_demo_pending_action, build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, ContextChannel, PendingActionKind
from plantkg.core.ports import PendingActionStore
from plantkg.core.versions import utcnow


def _demo_settings(tmp_path):
    return load_settings(profile="demo", overrides={
        "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"),
        "storage.series": str(tmp_path / "series"),
        "actions.token_file": str(tmp_path / "operator.token"),
    })


def _mock_storage_settings(tmp_path):
    return load_settings(profile="mock", overrides={
        "storage.enabled": True,
        "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"),
        "storage.series": str(tmp_path / "series"),
        "actions.token_file": str(tmp_path / "operator.token"),
    })


def _context(services) -> Context:
    return Context(plant_id=services.settings.plant_id, industry_pack=services.settings.industry_pack,
                   actor=Actor(actor_id="demo-tester", roles=()), channel=ContextChannel.offline,
                   trace_id="demo-seed-test", as_of=utcnow())


def test_demo_profile_opens_storage_and_registers_reviewers(tmp_path):
    settings = _demo_settings(tmp_path)
    assert settings.profile == "demo"
    assert settings.section("storage")["enabled"] is True
    assert settings.section("actions")["reviewers"]
    services = build_services(settings)
    try:
        with TestClient(create_app(services)) as client:
            health = client.get("/api/v1/health").json()
            assert health["profile"] == "demo"
            assert health["dependencies"]["storage"] == "unchecked"
    finally:
        services.close()


def test_demo_profile_confirm_route_accepts_backend_injected_token(tmp_path):
    settings = _demo_settings(tmp_path)
    services = build_services(settings)
    try:
        with TestClient(create_app(services)) as client:
            response = client.post(
                "/api/v1/actions/act-missing/confirm",
                json={"reason": "demo", "expected_revision": 1, "idempotency_key": "demo-1"},
                headers={"x-plantkg-operator-token": services.version_run.operator.token, "x-plantkg-actor": "dev-reviewer"},
            )
            assert response.status_code == 404
            assert "act-missing" in response.json()["error"]["message"]
    finally:
        services.close()


def test_demo_profile_lists_synthetic_action_and_confirms_it(tmp_path):
    """按文档启动 demo：装配写入一条 synthetic 待确认动作，actions list 能看到，确认返回 200/confirmed（#183 D1）。"""
    settings = _demo_settings(tmp_path)
    services = build_services(settings)
    services.close()
    services = build_services(settings)  # 库里已有待确认动作，重启不重复写
    try:
        with TestClient(create_app(services)) as client:
            listed = client.get("/api/v1/actions", params={"status": "proposed"}).json()
            assert len(listed["items"]) == 1
            action = listed["items"][0]
            assert action["payload"]["synthetic"] is True
            response = client.post(
                f"/api/v1/actions/{action['action_id']}/confirm",
                json={"reason": "demo", "expected_revision": action["revision"], "idempotency_key": "demo-confirm-1"},
                headers={"x-plantkg-operator-token": services.version_run.operator.token, "x-plantkg-actor": "dev-reviewer"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "confirmed"
            assert client.get("/api/v1/actions", params={"status": "proposed"}).json()["items"] == []
    finally:
        services.close()


def test_demo_profile_seed_is_fixed_and_other_actions_do_not_block_it(tmp_path):
    """#203 复审：库里已有其他待确认动作也能补种子；动作编号固定、payload 不带启动时间。"""
    services = build_services(_mock_storage_settings(tmp_path))
    try:
        store = services.registry.resolve(PendingActionStore)
        store.propose(PendingActionKind.publish, {"target_ref": "other-candidate"}, _context(services))

        _seed_demo_pending_action(services)

        proposed = store.list(status="proposed")[0]
        demo = [action for action in proposed if action.target_ref == "demo:act-183"]
        assert len(demo) == 1 and demo[0].action_id == "act-demo-183"
        assert "seeded_at" not in demo[0].payload
        _seed_demo_pending_action(services)  # 再次启动：存储层幂等插入不重复写
        assert len([action for action in store.list(status="proposed")[0]
                    if action.target_ref == "demo:act-183"]) == 1
    finally:
        services.close()


def test_demo_profile_concurrent_startups_write_one_seed(tmp_path):
    """并发启动同一库：demo 种子最终只有一条（固定幂等键 + 固定动作编号）。"""
    services = build_services(_mock_storage_settings(tmp_path))
    try:
        store = services.registry.resolve(PendingActionStore)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: _seed_demo_pending_action(services), range(4)))
        demo = [action for action in store.list()[0] if action.target_ref == "demo:act-183"]
        assert len(demo) == 1 and demo[0].action_id == "act-demo-183"
    finally:
        services.close()


def test_demo_profile_confirming_seed_then_reseeding_does_not_duplicate(tmp_path):
    """种子确认后再走一次种子逻辑：幂等键直接返回已有动作，不报错、不新增待确认项。"""
    settings = _demo_settings(tmp_path)
    services = build_services(settings)
    try:
        store = services.registry.resolve(PendingActionStore)
        seed = next(action for action in store.list(status="proposed")[0] if action.target_ref == "demo:act-183")
        with TestClient(create_app(services)) as client:  # TestClient 退出会关掉装配；断言都放在退出前
            response = client.post(
                f"/api/v1/actions/{seed.action_id}/confirm",
                json={"reason": "demo", "expected_revision": seed.revision, "idempotency_key": "demo-confirm-240"},
                headers={"x-plantkg-operator-token": services.version_run.operator.token,
                         "x-plantkg-actor": "dev-reviewer"},
            )
            assert response.status_code == 200, response.text
            _seed_demo_pending_action(services)
            assert [action for action in store.list(status="proposed")[0]
                    if action.target_ref == "demo:act-183"] == []
            assert len([action for action in store.list()[0] if action.target_ref == "demo:act-183"]) == 1
    finally:
        services.close()


def test_write_route_without_storage_returns_actionable_403():
    services = build_services(load_settings())  # mock：存储关，写路由没有提供者
    try:
        with TestClient(create_app(services)) as client:
            response = client.post("/api/v1/actions/act-1/confirm", json={})
            assert response.status_code == 403
            message = response.json()["error"]["message"]
            assert "--profile demo" in message
            assert "actions" in message
    finally:
        services.close()
