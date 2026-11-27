"""Catalog snapshot, replay cutoff, and evidence subset acceptance."""
from datetime import timedelta

from plantkg.core.contracts import Actor, Context, EvidenceEvent, EvidenceRef, EvidenceEventType, ResultStatus
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, catalog_metrics_provider, day_exports, open_db
from plantkg.world.scenario.export import chain, export_day

from .conftest import local, scenario


def metrics(day):
    return {m.metric: m for m in day.metrics if m.group == "knowledge"}


def test_catalog_snapshot_and_partial_replay(tmp_path):
    world = build_world(scenario(days=2))
    db = open_db(tmp_path / "catalog.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=local("00:10", day=2))
        context = Context(plant_id=db.plant_id, industry_pack="manufacturing", actor=Actor(actor_id="test", roles=("operator",)),
                          channel="offline", trace_id="test", as_of=local("10:00", day=2))
        catalog = SQLiteAssetCatalog(db.writer, db.plant_id, tmp_path / "blobs", base_dir=tmp_path)
        assets = []
        for i in range(4):
            path = tmp_path / f"{i}.txt"
            path.write_text(f"synthetic {i}", encoding="utf-8")
            assets.append(catalog.ingest(context, str(path), "policy", local("08:00"), nature="synthetic"))
        for index, stage in enumerate(("identified", "in_graph", "referenced"), 1):
            catalog.record_activation(context, assets[index].asset_version_id, stage, ref="rule:test", available_at=local("09:00"))
        catalog.record_activation(context, assets[3].asset_version_id, "referenced", ref="rule:test", available_at=local("09:00"))
        catalog.record_activation(context, assets[0].asset_version_id, "referenced", ref="rule:future", available_at=local("00:00", day=2))
        sink = SqliteEvidenceSink(db.writer, db.plant_id)
        sink.append(EvidenceEvent(event_id="ev-one", seq=0, occurred_at=local("09:01"), run_id=trace.run_id,
                                  trace_id="test", event_type=EvidenceEventType.retrieval, result_status=ResultStatus.ok,
                                  sources=(EvidenceRef(asset_version_id=assets[3].asset_version_id),)))
        runs = chain(db, trace.run_id)
        ev, subset = day_exports(db, runs, local("00:00", day=2))
        first = export_day(db, trace.run_id, "2026-07-01", evidence=ev, assets=subset,
                           asset_metrics_provider=catalog_metrics_provider(db))
        second = export_day(db, trace.run_id, "2026-07-02", asset_metrics_provider=catalog_metrics_provider(db))
        m, m2 = metrics(first), metrics(second)
        assert m["asset_total"].value == 4 and m["asset_activated"].value == 1
        assert m["asset_state_counts"].counts.model_dump() == dict(dormant=1, identified=1, in_graph=1, referenced=1)
        assert m["asset_activation_rate"].value == .25 and len(first.assets) == 1
        assert m["asset_total"].as_of == local("00:00", day=2) - timedelta(microseconds=1)
        assert m2["asset_activated"].value == 2 and m2["asset_total"].as_of == local("00:00", day=2)  # last persisted 15-minute slot
        assert "非完整日" in m2["asset_total"].note and "synthetic:4" in m2["asset_total"].note
        assert catalog.activation_summary(context.model_copy(update={"as_of": m["asset_total"].as_of}))["total"] == 4
        # A changed source creates a distinct historical version, not a physical-device count.
        (tmp_path / "1.txt").write_text("synthetic revised", encoding="utf-8")
        revised = catalog.ingest(context, str(tmp_path / "1.txt"), "policy", local("09:30"), nature="synthetic")
        assert revised.parent_version_id == assets[1].asset_version_id
        assert metrics(export_day(db, trace.run_id, "2026-07-01", asset_metrics_provider=catalog_metrics_provider(db)))["asset_total"].value == 5
        missing = metrics(export_day(db, trace.run_id, "2026-07-01"))
        assert missing["asset_total"].value is None and "未装配" in missing["asset_total"].note
    finally:
        db.close()


def test_empty_and_unavailable_catalog(tmp_path):
    world = build_world(scenario())
    db = open_db(tmp_path / "empty.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db).run(until=local("00:15"))
        provider = catalog_metrics_provider(db)
        empty = metrics(export_day(db, trace.run_id, asset_metrics_provider=provider))
        assert empty["asset_total"].value == 0 and empty["asset_activation_rate"].value is None
        assert sum(empty["asset_state_counts"].counts.model_dump().values()) == 0
        db.writer.submit(lambda con: con.execute("DROP TABLE asset_activations"))
        missing = metrics(export_day(db, trace.run_id, asset_metrics_provider=provider))
        assert missing["asset_total"].value is None and "不可读" in missing["asset_total"].note
    finally:
        db.close()
