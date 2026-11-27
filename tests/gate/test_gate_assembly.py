"""Exercise the production assembly and the existing MCP guard together."""

import json

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.ports import Gate, PendingActionStore


def test_storage_installs_gate_and_guarded_proposal_tool():
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        assert services.gate is services.version_run.gate
        assert Gate not in registry.fallbacks
        assert "propose_plan_confirmation" not in registry.fallbacks
        assert "plan_confirmation" in registry.action_handlers
        assert registry.resolve(PendingActionStore) is services.version_run.actions
        binding = registry.tools["propose_plan_confirmation"]
        text = services.guard.call(binding.spec, binding.handler, "", {
            "plan_id": "missing", "expected_revision": 1, "rationale": "test",
        })
        response = json.loads(text.split("\n", 1)[1])
        assert not response["ok"]
        assert response["error"]["code"] == "PLANTKG_E_NO_CTX"
    finally:
        services.close()


def test_storage_disabled_does_not_create_gate_service():
    services = build_services(load_settings(profile="mock"))
    try:
        assert services.gate is None
    finally:
        services.close()
