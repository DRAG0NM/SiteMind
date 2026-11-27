"""Continue a synthetic execution in a fresh interpreter for restart testing."""
import json
import sys
from pathlib import Path

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import Context, Plan
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences, SqliteVersionStore
from plantkg.decision.gate.service import GateService
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.series import ParquetSeriesStore
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.record.executor import RecordExecutor
from plantkg.world.replay.providers import install
from tests.versions.conftest import PLANT


def main():
    db, root, payload = map(Path, sys.argv[1:])
    data = json.loads(payload.read_text(encoding="utf-8"))
    plan, context = Plan.model_validate(data["plan"]), Context.model_validate(data["context"])
    writer = SQLiteWriter(db)
    try:
        assert not MigrationRunner(writer).apply().applied
        versions = SqliteVersionStore(writer, PLANT)
        refs = ManifestReferences()
        refs.bind(versions)
        series = ParquetSeriesStore(root / "record-series", writer, PLANT, refs.series)
        artifacts = IntegrationArtifacts(writer, series, PLANT, root / "record-artifacts")
        refs.bind_source_maps(artifacts)
        runs = SqliteRunStore(writer, PLANT, versions)
        clock, query = install(Registry(), writer, runs, series)
        gate = GateService(writer, PLANT, SqlitePendingActionStore(writer, PLANT, {}), runs, versions,
                           run_clock=clock.current_for_run)
        executor = RecordExecutor(writer, clock, gate, query, runs)
        assert executor.execute(plan, context, 0, data["key"]).event_seq == 1
        for index in range(40, 96):
            assert executor.execute(plan, context, index, f"synthetic-executor-{index}").event_seq == index + 1
    finally:
        writer.close()


if __name__ == "__main__":
    main()
