"""对抗性审查（2026-10-30）发现问题的回归测试。"""

import io
import logging
from datetime import datetime

import pytest

from plantkg.api import create_app
from plantkg.core import contracts as c
from plantkg.core.config import ConfigError, configure_logging, load_settings
from plantkg.core.registry import PackManifest, Registry, RegistryError
from plantkg.core.security import leaks_secret

CTX = "3f9c2a7e5b1d4c8e9a0b6d2f7e1c4a5b"
KEY = "sk-REALKEY0123456789abcdef"


@pytest.mark.parametrize("key, value", [
    ("nexent", "{version: x, enabled: false, base_url: u, api_key_env: EVIL, eval_api_key_env: E2}"),
    ("judge", "{backend: jev, jev: {api_key_env: EVIL}}"),
    ("Security.default_ctx.enabled", "true"),
    (" plant_id ", "other"),
    ("profile", "dev"),
    ("brand_new_section", "{x: 1}"),
])
def test_override_cannot_reach_protected_or_new_keys(key, value):
    with pytest.raises(ConfigError):
        load_settings(profile="prod", overrides={key: value})


def test_explicit_profile_wins():
    assert load_settings(profile="mock", overrides={"server.port": "8712"}).profile == "mock"


def test_registry_rejects_downgraded_or_mismatched_specs():
    registry = Registry()
    ingest = next(s for s in c.TOOL_SPECS if s.name == "ingest")
    with pytest.raises(RegistryError, match="契约一致"):
        registry.register_tool("ingest", lambda ctx, **_: None, ingest.model_copy(update={"mode": "read"}))
    stray = ingest.model_copy(update={"name": "other_name"})
    with pytest.raises(RegistryError):
        registry.register_tool("zzz_undeclared", lambda ctx, **_: None, stray)


def test_extension_tool_must_be_declared_by_its_pack():
    registry = Registry()
    spec = c.ToolSpec(name="diagnose_x", plane="knowledge", provider="B45", mode="read", description="x")
    with pytest.raises(RegistryError, match="provides.tools"):
        registry.register_tool("diagnose_x", lambda ctx, **_: None, spec)
    registry._loading_pack = PackManifest(name="hvac", schema_version=1, provides={"tools": ["diagnose_x"]})
    registry.register_tool("diagnose_x", lambda ctx, **_: None, spec)
    assert "diagnose_x" in registry.tools


def test_rest_serialization_failure_returns_error_body(services):
    from starlette.testclient import TestClient

    services.registry.rest[("GET", "/versions")] = lambda *_: {"at": datetime.now(), "x": float("nan")}
    with TestClient(create_app(services)) as client:
        resp = client.get("/api/v1/versions")
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "PLANTKG_E_INTERNAL"


@pytest.mark.parametrize("text", [CTX.upper(), f"{CTX[:16]} {CTX[16:]}", f"{CTX[:16]}-{CTX[16:]}", f"key={KEY}"])
def test_outbound_check_catches_disguised_secrets(text):
    assert leaks_secret(f"result: {text}", (CTX, KEY))


def test_guard_blocks_uppercase_echo(services):
    from plantkg.core.ports import RunSessionStore

    store = services.registry.resolve(RunSessionStore)
    s = store.register(c.Actor(actor_id="u", roles=("viewer",)), "demo-electronics", datetime.fromisoformat("2026-06-30T10:00:00+00:00"),
                       (), 3600, "ar-1", "tr-1")

    def leaky(context, **_):
        raise c.PlantkgError(c.ErrorCode.FORBIDDEN, s.ctx.upper())

    text = services.guard.call(c.TOOL_SPECS[0], leaky, s.ctx, {"source_uri": "x"})
    assert text.startswith("PLANTKG_E_INTERNAL:") and s.ctx.upper() not in text


def test_logs_redact_real_secret_values(settings, monkeypatch):
    monkeypatch.setenv("NEXENT_API_KEY", "nexent-key-value-1234")
    configure_logging(settings)
    stream = io.StringIO()
    logging.getLogger("plantkg").handlers[0].stream = stream
    logging.getLogger("plantkg.t").info("got %s and %s", "nexent-key-value-1234", CTX.upper())
    out = stream.getvalue()
    assert "nexent-key-value-1234" not in out and CTX.upper() not in out
