from datetime import datetime, timedelta, timezone
import pytest
from plantkg.core.contracts import Context, Interval, SourceBundle, SourceRecord
from plantkg.core.ports import Forecaster
from plantkg.decision.forecasting import FixedParameters, ForecastStore, HistoryReader, ParameterSnapshot, PersistenceForecaster
from plantkg.decision.planner.planner import _aligned

UTC = timezone.utc
D = datetime(2026, 10, 31, tzinfo=UTC)
ISSUE = D - timedelta(hours=6)
HORIZON = Interval(start=D, end=D + timedelta(days=1))
CTX = Context(plant_id="plant", industry_pack="test", actor=None, channel="offline", run_id="run", trace_id="trace", as_of=ISSUE, version_manifest_id="m1")


def record(q, start, value=1., quality="ok", available=None):
    return SourceRecord(source_id=q, source_version="v1", content_hash="sha256:" + "a" * 64, locator={"line_start": int(start.timestamp())}, nature="configured", event_time=start, interval=Interval(start=start, end=start+timedelta(minutes=15)), available_at=available or start+timedelta(minutes=15), source_tz="UTC", source_unit="kW", quantity=q, unit="kW", value=value, quality={"flag":quality})


class Query:
    def __init__(self, rows): self.rows = rows
    def query(self, ctx, q, window, cursor=None):
        rows = sorted((r for r in self.rows if r.quantity == q and r.available_at <= ctx.as_of and window.start <= r.event_time < window.end), key=lambda r:r.event_time)
        offset = int(cursor or 0)
        return SourceBundle(bundle_id=q, source_id=q, source_version="v1", mapping_version="map", records=tuple(rows[offset:offset+50]), next_cursor=str(offset+50) if len(rows)>offset+50 else None)


def setup(rows, k=1., ctx=CTX, horizon=HORIZON, ref="params1"):
    reader = HistoryReader(Query(rows))
    f = PersistenceForecaster(reader, FixedParameters({ctx.version_manifest_id: ParameterSnapshot(ctx.version_manifest_id, ref, "persist-v1", k, 100.)}))
    return f, reader.read(ctx, horizon)


def rows_for_day():
    return [record(q, D-timedelta(days=days)+timedelta(minutes=15*i), 1 if days==1 else 2) for q in ("baseline_load", "pv_ac_power") for days in (1,2) for i in range(96)]


def test_boundary_pagination_determinism_and_no_future_leak():
    rows = rows_for_day()
    forecaster, history = setup(rows)
    assert isinstance(forecaster, Forecaster)
    bundle = forecaster.predict(CTX, HORIZON, history)
    for f in bundle.forecasts:
        assert f.values[:72] == (1.,)*72
        assert f.values[72:] == (2.,)*24
        assert [f.values[i] for i in (0, 71, 72, 73, 95)] == [1.,1.,2.,2.,2.]
        assert f.coverage == 1 and f.params["interval_status"] == "uncalibrated"
    future = [r.model_copy(update={"value":99.}) if r.available_at > ISSUE else r for r in rows]
    other, h2 = setup(list(reversed(future)))
    assert bundle.model_dump_json() == other.predict(CTX, HORIZON, h2).model_dump_json()
    assert all(isinstance(x, float) for x in bundle.uncertainty_summary.values())


def test_visibility_equal_and_one_second_late():
    t = D + timedelta(hours=17, minutes=45)
    one = record("baseline_load", t-timedelta(days=1), 3, available=ISSUE)
    back = record("baseline_load", t-timedelta(days=2), 7)
    f,h=setup([one,back]); assert f.predict(CTX,HORIZON,h).forecasts[0].values[71] == 3
    late=one.model_copy(update={"available_at":ISSUE+timedelta(seconds=1)})
    f,h=setup([late,back]); assert f.predict(CTX,HORIZON,h).forecasts[0].values[71] == 7
    forged=h.model_copy(update={"records":h.records+(late,)})
    with pytest.raises(ValueError): f.predict(CTX,HORIZON,forged)


def test_missing_zero_quality_and_conflict():
    rows=[record("baseline_load",D-timedelta(days=1),0), record("baseline_load",D-timedelta(days=1)+timedelta(minutes=15),None,"missing"), record("baseline_load",D-timedelta(days=2)+timedelta(minutes=15),4,"suspect")]
    f,h=setup(rows); out=f.predict(CTX,HORIZON,h).forecasts[0]
    assert out.values[:3] == (0.,None,None)
    assert out.coverage == 1/96 and "suspect" in out.params["slot_quality"][1]
    f,h=setup([rows[0],rows[0].model_copy(update={"value":3.})])
    with pytest.raises(ValueError, match="conflicting"): f.predict(CTX,HORIZON,h)


def test_estimated_pv_and_parameter_binding_capacity():
    row=record("pv_ac_power",D-timedelta(days=1),10,"estimated")
    a,h=setup([row]); old=a.predict(CTX,HORIZON,h)
    ctx=CTX.model_copy(update={"version_manifest_id":"m2"})
    b,h2=setup([row],1.5,ctx,ref="params2"); new=b.predict(ctx,HORIZON,h2)
    assert old.forecasts[1].values[0] == 10 and new.forecasts[1].values[0] == 15
    assert old.forecasts[1].forecast_id != new.forecasts[1].forecast_id
    assert "estimated" in old.forecasts[1].params["slot_quality"][0]
    assert row.value == 10
    for k in (-1,float("nan")):
        f,h=setup([row],k)
        with pytest.raises(ValueError): f.predict(CTX,HORIZON,h)


def test_corrected_pv_is_clipped_and_marked_without_losing_quality():
    start = D - timedelta(days=1)
    rows = [
        record("baseline_load", start, 120),
        record("pv_ac_power", start, 80, "estimated"),
        record("pv_ac_power", start + timedelta(minutes=15), 60),
        record("pv_ac_power", start - timedelta(days=1) + timedelta(minutes=30), 90),
    ]
    f, h = setup(rows, 1.5)
    bundle = f.predict(CTX, HORIZON, h)
    load, pv = bundle.forecasts
    assert load.values[0] == 120
    assert pv.values[:3] == (100, 90, 100)
    assert pv.params["slot_quality"][:3] == ["24h:estimated:clipped", "24h", "48h:24h_absent_or_not_visible:clipped"]
    assert rows[1].value == 80
    assert bundle.uncertainty_summary["pv_available_fallback_slots"] == 1


def test_nonfinite_corrected_pv_is_rejected():
    f, h = setup([record("pv_ac_power", D-timedelta(days=1), 1e308)], 2)
    with pytest.raises(ValueError, match="nonfinite"):
        f.predict(CTX, HORIZON, h)


def test_horizon_and_planner_visibility():
    f,h=setup(rows_for_day()); bundle=f.predict(CTX,HORIZON,h)
    class Request: horizon=HORIZON; step_minutes=15
    assert len(_aligned(bundle.forecasts[0], Request(), ISSUE)) == 96
    with pytest.raises(Exception): _aligned(bundle.forecasts[0], Request(), ISSUE-timedelta(seconds=1))
    bad=Interval(start=D, end=D+timedelta(minutes=16))
    f,h=setup([],horizon=bad)
    with pytest.raises(ValueError): f.predict(CTX,bad,h)


def test_storage_reopen_idempotence_conflict_and_versions(tmp_path):
    f,h=setup(rows_for_day()); bundle=f.predict(CTX,HORIZON,h)
    store=ForecastStore(tmp_path); key=store.put(bundle)
    assert store.put(bundle) == key and ForecastStore(tmp_path).get(key) == bundle
    first=bundle.forecasts[0].model_copy(update={"values":(99.,)+bundle.forecasts[0].values[1:]})
    with pytest.raises(ValueError,match="conflict"): store.put(bundle.model_copy(update={"forecasts":(first,bundle.forecasts[1])}))
    assert store.get(key) == bundle
    ctx=CTX.model_copy(update={"version_manifest_id":"m2"})
    f,h=setup(rows_for_day(),1.2,ctx,ref="params2")
    assert store.put(f.predict(ctx,HORIZON,h)) != key


def test_week_and_bad_grid():
    week=Interval(start=D,end=D+timedelta(days=7))
    f,h=setup(rows_for_day(),horizon=week)
    assert len(f.predict(CTX,week,h).forecasts[0].values)==672
    bad=record("baseline_load",D-timedelta(days=1)).model_copy(update={"event_time":D-timedelta(days=1)+timedelta(minutes=1)})
    f,h=setup([bad])
    with pytest.raises(ValueError,match="grid"): f.predict(CTX,HORIZON,h)


def test_b05_selected_meter_baseline_and_coverage():
    from plantkg.decision.forecasting import prepare_baseline
    from plantkg.adapters.baseload import selected_device_baseload
    from plantkg.core.contracts import Locator, TransformStep
    def meter(name, value):
        r=record("active_power",D-timedelta(days=1),value).model_copy(update={"source_id":"meters", "locator":Locator(meter=name), "transform_steps":(TransformStep(op="scale",version="v1",params={"factor":1.}),)})
        return SourceBundle(bundle_id=name,source_id="meters",source_version="v1",mapping_version="map",records=(r,))
    total,device=meter("total",10),meter("process",3)
    base=prepare_baseline(total,[device],["process"],selected_device_baseload)
    assert base.records[0].value == 7 and base.records[0].value + device.records[0].value == 10
    with pytest.raises(ValueError): prepare_baseline(total,[meter("process",11)],["process"],selected_device_baseload)
    with pytest.raises(ValueError): prepare_baseline(total,[],["process"],selected_device_baseload)
    with pytest.raises(ValueError): prepare_baseline(total,[device],["other"],selected_device_baseload)


def test_b07_real_query_online_allowlist_and_pagination():
    from plantkg.world.replay.series import ReplaySeries, online_record_hash
    rows=rows_for_day()
    hidden=record("pv_ac_power",D-timedelta(days=1),80).model_copy(update={"locator":record("pv_ac_power",D-timedelta(days=1)).locator.model_copy(update={"line_start":999})})
    # Unapproved truth never enters B14, even though it is returned by storage.
    class Clock:
        def current(self, context):
            return type("Run",(),{"run_id":context.run_id,"version_manifest_id":context.version_manifest_id})(),0,context.as_of
    class Runs:
        def run_inputs(self, run_id):
            return {"inputs":{"online_source_ids":["baseline_load","pv_ac_power"],"online_record_hashes":[online_record_hash(r) for r in rows]}}
    class Series:
        def query(self, context, quantity, window):
            return Query(rows+[hidden]).query(context,quantity,window).model_copy(update={"records":tuple(r for r in rows+[hidden] if r.quantity == quantity and r.available_at <= context.as_of and window.start <= r.event_time < window.end),"next_cursor":None})
    reader=HistoryReader(ReplaySeries(Clock(),Series(),Runs()))
    history=reader.read(CTX,HORIZON)
    assert hidden not in history.records and len(history.records)>100
    forecaster=PersistenceForecaster(reader,FixedParameters({"m1":ParameterSnapshot("m1","params1","persist-v1",1.,100.)}))
    assert forecaster.predict(CTX,HORIZON,history).forecasts[1].values[0]==1




def test_reject_future_record_from_misbehaving_query():
    future = record("baseline_load",D-timedelta(days=1),4,available=ISSUE+timedelta(seconds=1))
    class BadQuery(Query):
        def query(self, ctx, q, window, cursor=None):
            bundle=super().query(ctx,q,window,cursor)
            return bundle.model_copy(update={"records":(future,) if q=="baseline_load" else ()})
    reader=HistoryReader(BadQuery([]))
    f=PersistenceForecaster(reader,FixedParameters({"m1":ParameterSnapshot("m1","p","v",1,100)}))
    h=reader.read(CTX,HORIZON)
    with pytest.raises(ValueError,match="future"): f.predict(CTX,HORIZON,h)


def test_inconsistent_pages_rejected():
    class BadPages(Query):
        def query(self,ctx,q,window,cursor=None):
            return SourceBundle(bundle_id="a",source_id=q,source_version="v2" if cursor else "v1",mapping_version="m",records=(),next_cursor=None if cursor else "next")
    with pytest.raises(ValueError,match="version"):
        HistoryReader(BadPages([])).read(CTX,HORIZON)


def test_storage_failed_publish_preserves_previous_artifact(tmp_path, monkeypatch):
    f,h=setup(rows_for_day()); bundle=f.predict(CTX,HORIZON,h)
    store=ForecastStore(tmp_path); key=store.put(bundle)
    def fail(*args): raise OSError("simulated publication failure")
    monkeypatch.setattr("plantkg.decision.forecasting.store.os.link",fail)
    with pytest.raises(OSError): store.put(bundle)
    assert store.get(key)==bundle
    assert sorted(p.name for p in tmp_path.iterdir()) == [key+".json"]
