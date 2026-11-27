from datetime import datetime, timedelta, timezone

from plantkg.core.security import SessionCtxResolver
from plantkg.core.sessions import MemoryRunSessionStore
from tests.versions.conftest import PLANT, REVIEWER


def test_session_binding_is_server_owned_and_removed_on_expire():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sessions = MemoryRunSessionStore("manufacturing", clock=lambda: now)
    session = sessions.register(REVIEWER, PLANT, now, [], 60, "agent-1", "trace-1", run_id="run-1", version_manifest_id="manifest-1")
    resolver = SessionCtxResolver(sessions, clock=lambda: now + timedelta(seconds=1))
    ctx = resolver.resolve(session.ctx)
    assert (ctx.agent_run_id, ctx.run_id, ctx.version_manifest_id) == ("agent-1", "run-1", "manifest-1")
    sessions.expire(session.ctx)
    assert resolver.resolve(session.ctx) is None


def test_fake_store_implements_binding_contract():
    from plantkg.core.ports import RunSessionStore
    from plantkg.testing.fakes import InMemoryRunSessionStore

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store: RunSessionStore = InMemoryRunSessionStore(clock=lambda: now)
    session = store.register(REVIEWER, PLANT, now, [], 60, "agent-1", "trace-1",
                             run_id="run-1", version_manifest_id="manifest-1")
    ctx = SessionCtxResolver(store, clock=lambda: now).resolve(session.ctx)
    assert (ctx.run_id, ctx.version_manifest_id) == ("run-1", "manifest-1")
    store.expire(session.ctx)
    assert store.resolve_binding(session.ctx) is None
