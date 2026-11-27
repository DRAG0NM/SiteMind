from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.core.registry import Registry
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.replay.providers import install
from plantkg.world.replay.evaluation import EvaluationTruth
from plantkg.world.replay.series import online_record_hash
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, context

def test_real_sqlite_parquet_manifest_and_provider_pagination(tmp_path, writer, versions):
    refs = ManifestReferences()
    refs.bind(versions)
    series = ParquetSeriesStore(tmp_path / "series", writer, PLANT, refs.series)
    records = tuple(SourceRecord(source_id="online", source_version="v1", content_hash="sha256:" + "a" * 64,
        locator={"line_start": i + 1}, nature="configured", event_time=AS_OF + timedelta(minutes=15*i),
        available_at=AS_OF if i < 3 else AS_OF + timedelta(hours=1), source_tz="UTC", source_unit="kW",
        quantity="power", unit="kW", value=float(i)) for i in range(4))
    truth = records[0].model_copy(update={"value": 9999.0, "locator": records[0].locator.model_copy(update={"line_start": 999})})
    missing = SourceRecord(source_id="online", source_version="v1", content_hash="sha256:" + "a" * 64,
        locator={"line_start": 50}, nature="derived", event_time=AS_OF + timedelta(minutes=45), available_at=AS_OF,
        source_tz="UTC", source_unit="W", quantity="power", unit="kW", value=None, quality={"flag": "missing"},
        transform_steps=({"op": "unit_w_to_kw", "params": {}, "version": "1"},))
    records += (missing, truth)
    ref = series.write_partition(SourceBundle(bundle_id="online", source_id="online", source_version="v1", mapping_version="m1", records=records, note="private"))
    with writer.read_only() as con:
        checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
    versions.register_verifier("series:v1:", lambda component: "complete")
    manifest = versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum}}, "test")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    runs = SqliteRunStore(writer, PLANT, versions)
    run = runs.create_run(context(), "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)), inputs={"online_source_ids": ["online"], "online_record_hashes": [online_record_hash(r) for r in records[:-1]]})
    ctx = context(run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    registry = Registry()
    clock, query = install(registry, writer, runs, series)
    clock.initialize(ctx)
    window = Interval(start=AS_OF, end=AS_OF + timedelta(hours=2))
    first = query.query(ctx, "power", window, page_size=2)
    assert [r.value for r in first.records] == [0, 1]
    assert first.note is None and first.next_cursor
    second = query.query(ctx, "power", window, first.next_cursor, page_size=2)
    assert [r.value for r in second.records] == [2, None]
    assert second.records[1].quality.flag == "missing" and second.records[1].source_unit == "W"
    assert second.records[1].nature == "derived" and second.records[1].transform_steps[0].op == "unit_w_to_kw"
    with pytest.raises(PlantkgError) as bad:
        query.query(ctx, "other", window, first.next_cursor, page_size=2)
    assert bad.value.code == ErrorCode.INVALID_REQUEST
    clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))
    with pytest.raises(PlantkgError) as bad_cutoff:
        query.query(ctx.model_copy(update={"as_of": AS_OF + timedelta(minutes=15)}), "power", window, first.next_cursor, page_size=2)
    assert bad_cutoff.value.code == ErrorCode.INVALID_REQUEST
    result = registry.tools["ts_query"].handler(ctx, "power", window.start.isoformat(), window.end.isoformat(), page_size=2)
    assert result.data["records"][0]["value"] == 0
    assert "private" not in result.to_agent_text()
    assert "9999" not in result.to_agent_text()
    replacement = versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum},
                                    "rules": {"ref": "t:new-rules", "content_hash": "sha256:" + "d" * 64}}, "new active")
    versions.check_integrity(replacement.version_manifest_id)
    versions.activate(manifest.version_manifest_id, replacement.version_manifest_id, REVIEWER, "switch")
    assert query.query(ctx, "power", window, page_size=2).records == first.records
    view = registry.rest[("GET", "/runs/{run_id}/view")](ctx, {"run_id": run.run_id}, {"at": AS_OF.isoformat()})
    assert view.run_id == run.run_id and view.event_seq == 0
    with pytest.raises(PlantkgError):
        query.query(ctx.model_copy(update={"run_id": None}), "power", window)
    restricted = runs.create_run(ctx, "evaluation", run.clock, inputs={"online_source_ids": ["other"], "online_record_hashes": []})
    restricted_ctx = ctx.model_copy(update={"run_id": restricted.run_id})
    clock.initialize(restricted_ctx)
    with pytest.raises(PlantkgError) as bad_run:
        query.query(restricted_ctx, "power", window, first.next_cursor, page_size=2)
    assert bad_run.value.code == ErrorCode.INVALID_REQUEST
    with pytest.raises(PlantkgError) as denied:
        query.query(restricted_ctx, "power", window)
    assert denied.value.code == ErrorCode.NOT_VISIBLE
    assert "online" not in str(denied.value)
    assert not any("truth" in name or "evaluation" in name for name in registry.tools)
    evaluation = runs.create_run(ctx, "evaluation", run.clock, inputs={"evaluation_source_ids": ["online"]})
    eval_ctx = ctx.model_copy(update={"run_id": evaluation.run_id})
    assert any(record.value == 9999 for record in EvaluationTruth(runs, series).query(eval_ctx, "power", window).records)
    with pytest.raises(PlantkgError):
        EvaluationTruth(runs, series).query(ctx, "power", window)


def test_actual_rest_and_mcp_with_server_bound_session():
    import asyncio
    from fastmcp import Client
    from starlette.testclient import TestClient
    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.core.ports import StateProvider
    from plantkg.mcp_server import create_mcp_server

    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    versions, runs, series = services.version_run.versions, services.version_run.runs, services.storage.series
    record = SourceRecord(source_id="online", source_version="v1", content_hash="sha256:" + "b" * 64,
        locator={"line_start": 1}, nature="configured", event_time=AS_OF, available_at=AS_OF,
        source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=0)
    truth = record.model_copy(update={"value": 9999.0, "locator": record.locator.model_copy(update={"line_start": 999})})
    ref = series.write_partition(SourceBundle(bundle_id="online", source_id="online", source_version="v1", mapping_version="m", records=(record, truth), note="private-evaluation-metadata"))
    with services.storage.writer.read_only() as con:
        checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
    manifest = versions.prepare({"data_sources": {"ref": ref, "content_hash": "sha256:" + checksum}}, "test")
    versions.check_integrity(manifest.version_manifest_id)
    versions.activate(None, manifest.version_manifest_id, REVIEWER, "test")
    ctx = context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id)
    run = runs.create_run(ctx, "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)), inputs={"online_source_ids": ["online"], "online_record_hashes": [online_record_hash(record)]})
    ctx = ctx.model_copy(update={"run_id": run.run_id})
    assert services.registry.resolve(StateProvider).current(ctx)[1:] == (0, AS_OF)
    session = services.version_run.sessions.register(REVIEWER, settings.plant_id, AS_OF, [], 60, "agent-1", "trace-1",
        run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
    async def call():
        async with Client(create_mcp_server(services)) as client:
            arguments = {"quantity": "power", "start": AS_OF.isoformat(), "end": (AS_OF + timedelta(hours=1)).isoformat()}
            without = await client.call_tool("plantkg_ts_query", arguments)
            with_ctx = await client.call_tool("plantkg_ts_query", arguments | {"ctx": session.ctx})
            ledger = await client.call_tool("plantkg_ledger", {"run_id": run.run_id, "kind": "execution", "ctx": session.ctx})
            return without.content[0].text, with_ctx.content[0].text, ledger.content[0].text
    without, with_ctx, ledger = asyncio.run(call())
    assert "PLANTKG_E_PRECONDITION" in without
    assert '"value":0.0' in with_ctx and session.ctx not in with_ctx
    assert "9999" not in with_ctx and "private-evaluation-metadata" not in with_ctx
    assert '"items":[]' in ledger
    with TestClient(create_app(services)) as client:
        ledger_response = client.get(f"/api/v1/runs/{run.run_id}/ledger", params={"kind": "execution"})
        assert ledger_response.status_code == 200 and ledger_response.json()["items"] == []
        missing_plan = client.get(f"/api/v1/runs/{run.run_id}/ledger", params={"kind": "plan"})
        assert missing_plan.status_code == 200 and missing_plan.json()["items"] == []  # B19 计划账本 provider：无记录即空页
        response = client.get(f"/api/v1/runs/{run.run_id}/view", params={"at": AS_OF.isoformat()})
        assert response.status_code == 200 and response.json()["run_id"] == run.run_id
        assert "9999" not in response.text and "private-evaluation-metadata" not in response.text
        future = client.get(f"/api/v1/runs/{run.run_id}/view", params={"at": (AS_OF + timedelta(minutes=15)).isoformat()})
        assert future.status_code == 404
