from datetime import datetime, timezone

import pytest

from plantkg.core.contracts import Actor, Context
from plantkg.core.versions import SqliteVersionStore
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

PLANT = "demo-electronics"
REVIEWER = Actor(actor_id="u-reviewer", roles=("reviewer",))
VIEWER = Actor(actor_id="u-viewer", roles=("viewer",))
AS_OF = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)


def context(**update):
    base = Context(plant_id=PLANT, industry_pack="manufacturing", actor=REVIEWER, channel="gateway", agent_run_id="ar-1",
                   trace_id="tr-1", as_of=AS_OF)
    return base.model_copy(update=update)


def component(ref, n=0):
    return {"ref": ref, "content_hash": "sha256:" + f"{n:064x}"}


@pytest.fixture
def writer(tmp_path):
    w = SQLiteWriter(tmp_path / "catalog.db")
    MigrationRunner(w).apply()
    yield w
    w.close()


@pytest.fixture
def statuses():
    """可变的校验结果：ref -> 状态，默认 complete。"""
    return {}


@pytest.fixture
def versions(writer, statuses):
    store = SqliteVersionStore(writer, PLANT)
    store.register_verifier("t:", lambda c: statuses.get(c.ref, "complete"))
    return store
