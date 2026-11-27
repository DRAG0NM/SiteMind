"""B35 回答卡冒烟：ask_operator 登记 question 待确认动作，HTTP 确认把答案（reason）回写。

装配走默认包（build_services + 真实 Sqlite store + 统一确认端点）：
主 Agent 提问 → /actions?kind=question 可见 → POST /actions/{id}/confirm（reason=答案）→
router.store.questions 里该问题变成 answered 且答案一致。
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

from plantkg.agent.router import ROUTER_HOOK
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.ports import PendingActionStore
from tests.agent.router.helpers import make_event

PLANT = "demo-electronics"
HEADERS = {"x-plantkg-operator-token": "", "x-plantkg-actor": "u-reviewer"}


def entity(kind: str, code: str) -> str:
    return f"entity://{PLANT}/{kind}/{code}"


def test_question_pending_action_visible_and_http_confirm_answers(tmp_path):
    services = build_services(load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series"),
        "actions.reviewers": "[u-reviewer]"}))
    client = TestClient(create_app(services))
    try:
        registry = services.registry
        router = registry.hooks_for(ROUTER_HOOK)[0]
        # 货梯困人：安全类事件单不自动处理，事件单保持打开，主 Agent 在救援方案讨论中提问
        handled = router.handle_event(make_event("EV-B35-Q", 0, "elevator_trapped", entity("elevator_group", "EG-PROD")))
        assert handled.ticket_id

        asked = registry.tools["ask_operator"].handler(SimpleNamespace(), "轿厢里还有人吗？", handled.ticket_id)
        assert asked.ok and asked.data["status"] == "pending"
        question_id = asked.data["question_id"]
        assert asked.data["action_id"]  # 有待确认动作存储时登记；没有时只返回问题，这里装配了存储

        store = registry.resolve(PendingActionStore)
        action = store.get(asked.data["action_id"])
        assert action.kind == "question" and action.status == "proposed" and action.revision == 0
        assert action.payload["target_ref"] == question_id == action.target_ref
        assert action.payload["question_id"] == question_id
        assert action.payload["question"] == "轿厢里还有人吗？"

        page = client.get("/api/v1/actions", params={"kind": "question", "status": "proposed"}).json()
        assert any(item["action_id"] == action.action_id for item in page["items"])

        response = client.post(f"/api/v1/actions/{action.action_id}/confirm", json={
            "reason": "轿厢里有人", "expected_revision": action.revision,
            "idempotency_key": "b35-question-confirm"},
            headers=HEADERS | {"x-plantkg-operator-token": services.version_run.operator.token})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "confirmed"

        questions = router.store.questions(handled.ticket_id)
        assert questions[-1]["status"] == "answered" and questions[-1]["answer"] == "轿厢里有人"
    finally:
        client.close()
        services.close()
