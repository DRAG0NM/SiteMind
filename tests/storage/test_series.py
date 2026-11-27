from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from plantkg.core.contracts import Context, Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.series import ParquetSeriesStore
from plantkg.storage.sqlite import SQLiteWriter


def test_series_roundtrip_visibility(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = SourceRecord(source_id="source", source_version="v1", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature="configured", event_time=now, available_at=now + timedelta(hours=1), source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=None, quality={"flag": "missing"})
        bundle = SourceBundle(bundle_id="bundle", source_id="source", source_version="v1", mapping_version="m1", records=(record,), license="test")
        refs = []
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: refs)
        ref = store.write_partition(bundle)
        assert store.write_partition(bundle) == ref
        refs.append(ref)
        context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=now, version_manifest_id="v1")
        window = Interval(start=now, end=now + timedelta(hours=2))
        assert store.query(context, "power", window).records == ()
        result = store.query(context.model_copy(update={"as_of": now + timedelta(hours=1)}), "power", window)
        assert result.records == (record,) and result.license == "test"
        (tmp_path / "series" / (ref.split(":")[-1] + ".parquet")).write_bytes(b"corrupt")
        with pytest.raises(PlantkgError):
            store.query(context, "power", window)
    finally:
        writer.close()


def test_series_faults_are_not_visible(tmp_path, monkeypatch):
    from plantkg.storage.integrity import check_references
    import pyarrow.parquet as pq
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = SourceRecord(source_id="s", source_version="v", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature="derived", event_time=now, available_at=now, source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=2)
        bundle = SourceBundle(bundle_id="b", source_id="s", source_version="v", mapping_version="m", records=(record,))
        refs = []
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: refs)
        write = pq.write_table
        def half_write(table, target):
            with open(target, "wb") as stream:
                stream.write(b"partial")
            raise OSError("injected write failure")
        monkeypatch.setattr(pq, "write_table", half_write)
        with pytest.raises(OSError):
            store.write_partition(bundle)
        assert list(store.path.iterdir()) == []
        monkeypatch.setattr(pq, "write_table", write)
        submit = writer.submit
        monkeypatch.setattr(writer, "submit", lambda fn: (_ for _ in ()).throw(RuntimeError("registration failed")))
        with pytest.raises(RuntimeError):
            store.write_partition(bundle)
        orphan = next(store.path.glob("*.parquet"))
        ref = "series:v1:" + orphan.stem
        refs.append(ref)
        assert check_references(writer, None, store, "p", refs)[0].status == "not_prepared"
        context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=now, version_manifest_id="v")
        with pytest.raises(PlantkgError):
            store.query(context, "power", Interval(start=now, end=now + timedelta(hours=1)))
        monkeypatch.setattr(writer, "submit", submit)
        assert store.write_partition(bundle) == ref
        assert check_references(writer, None, store, "p", refs)[0].status == "complete"
        orphan.unlink()
        assert check_references(writer, None, store, "p", refs)[0].status == "missing"
    finally:
        writer.close()


def test_series_window_visibility_and_scope(tmp_path):
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        hour = timedelta(hours=1)
        def record(nature, event, available, interval=None):
            return SourceRecord(source_id="s", source_version="v", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature=nature, event_time=event, interval=interval, available_at=available, source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=1)
        records = (
            record("configured", start, start + hour),
            record("derived", start + hour, start + 2 * hour),
            record("configured", start + 2 * hour, start),
            record("measured", start - hour, start, Interval(start=start - hour, end=start)),
            record("measured", start - hour, start + hour, Interval(start=start - hour, end=start + hour)),
            record("configured", start + 2 * hour, start, Interval(start=start + 2 * hour, end=start + 3 * hour)),
        )
        bundle = SourceBundle(bundle_id="b", source_id="s", source_version="v", mapping_version="m", records=records)
        refs = []
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: refs)
        refs.append(store.write_partition(bundle))
        context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=start + hour, version_manifest_id="v")
        window = Interval(start=start, end=start + 2 * hour)
        assert set(store.query(context, "power", window).records) == {records[0], records[4]}
        later = context.model_copy(update={"as_of": start + 2 * hour})
        assert set(store.query(later, "power", window).records) == {records[0], records[1], records[4]}
        assert store.query(later, "other", window).records == ()
        with pytest.raises(PlantkgError):
            store.query(context.model_copy(update={"plant_id": "other"}), "power", window)
        with pytest.raises(PlantkgError):
            store.query(context.model_copy(update={"version_manifest_id": None}), "power", window)
        other_bundle = bundle.model_copy(update={"bundle_id": "other", "source_id": "other", "records": ()})
        refs.append(store.write_partition(other_bundle))
        with pytest.raises(PlantkgError, match="Multiple partitions"):
            store.query(context, "power", window)
    finally:
        writer.close()


@pytest.mark.parametrize("param", [(1, 2), datetime(2026, 1, 1, tzinfo=timezone.utc)])
def test_lossy_transform_parameters_are_rejected_before_publish(tmp_path, param):
    from plantkg.core.contracts import TransformStep
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = SourceRecord(source_id="s", source_version="v", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature="derived", event_time=now, available_at=now, source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=1, transform_steps=(TransformStep(op="x", version="1", params={"nested": {"value": param}}),))
        bundle = SourceBundle(bundle_id="b", source_id="s", source_version="v", mapping_version="m", records=(record,))
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: [])
        with pytest.raises(PlantkgError, match="round-trip"):
            store.write_partition(bundle)
        assert list(store.path.iterdir()) == []
    finally:
        writer.close()


@pytest.mark.parametrize("param", [float("nan"), float("inf"), -float("inf"), Decimal("NaN"), Decimal("Infinity")])
def test_nonfinite_transform_parameters_are_invalid_request(tmp_path, param):
    from plantkg.core.contracts import ErrorCode, TransformStep
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = SourceRecord(source_id="s", source_version="v", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature="derived", event_time=now, available_at=now, source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=1, transform_steps=(TransformStep(op="x", version="1", params={"nested": [param]}),))
        bundle = SourceBundle(bundle_id="b", source_id="s", source_version="v", mapping_version="m", records=(record,))
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: [])
        with pytest.raises(PlantkgError) as exc:
            store.write_partition(bundle)
        assert exc.value.code == ErrorCode.INVALID_REQUEST
        assert list(store.path.iterdir()) == []
    finally:
        writer.close()


def test_b05_pvgis_bundle_still_round_trips(tmp_path):
    from plantkg.adapters import PvgisAdapter
    from tests.adapters.test_adapters import pv_file, pv_map
    bundle = PvgisAdapter().normalize(pv_file(tmp_path), pv_map(), {})
    writer = SQLiteWriter(tmp_path / "catalog.db")
    try:
        MigrationRunner(writer).apply()
        refs = []
        store = ParquetSeriesStore(tmp_path / "series", writer, "p", lambda context, quantity: refs)
        refs.append(store.write_partition(bundle))
        context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=max(r.available_at for r in bundle.records), version_manifest_id="v")
        window = Interval(start=min(r.interval.start for r in bundle.records), end=max(r.interval.end for r in bundle.records))
        assert store.query(context, bundle.records[0].quantity, window) == bundle
    finally:
        writer.close()
