from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError
from plantkg.core.runs import SqliteRunStore
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, component, context


@pytest.fixture
def runs(writer, versions):
    manifest = versions.prepare({"rules": component("t:rules@v1")}, "test")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    return SqliteRunStore(writer, PLANT, versions)


@pytest.mark.parametrize("approved_at", ["2026-06-30T18:00:00+08:00", "2026-06-30T10:00:00Z"])
def test_approval_accepts_equivalent_instants(runs, approved_at):
    clock = Interval(start=AS_OF, end=AS_OF + timedelta(hours=1))
    run = runs.create_run(context(), "replay_day", clock, inputs={"online_approved_as_of": approved_at})
    assert runs.run_inputs(run.run_id)["inputs"]["online_approved_as_of"] == approved_at


@pytest.mark.parametrize("approved_at", ["2026-06-30T18:00:01+08:00", "2026-06-30T10:00:00", "invalid", 42])
def test_approval_rejects_different_or_invalid_instants(runs, approved_at):
    clock = Interval(start=AS_OF, end=AS_OF + timedelta(hours=1))
    with pytest.raises(PlantkgError) as exc:
        runs.create_run(context(), "replay_day", clock, inputs={"online_approved_as_of": approved_at})
    assert exc.value.code == ErrorCode.PRECONDITION


def test_approval_rejects_cutoff_after_replay_start(runs):
    clock = Interval(start=AS_OF - timedelta(minutes=1), end=AS_OF + timedelta(hours=1))
    with pytest.raises(PlantkgError) as exc:
        runs.create_run(context(), "replay_day", clock, inputs={"online_approved_as_of": "2026-06-30T18:00:00+08:00"})
    assert exc.value.code == ErrorCode.PRECONDITION
