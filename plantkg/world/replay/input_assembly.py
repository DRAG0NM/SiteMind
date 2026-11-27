"""Trusted preparation of baseline and PV inputs before a run is created."""
from datetime import timedelta
import math

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.world.replay.series import online_record_hash


def prepare_online_inputs(series, artifacts, total, devices, selected_meters, pv, subtract, *, approved_baseline, approved_pv, as_of, basis):
    """Prepare partitions and explicit approved records; caller controls the trusted selection.

    ``approved_*`` are records from the resulting baseline and PV bundles, respectively.
    No raw meter records are added to the online run allowlist.
    """
    if as_of.utcoffset() is None:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'A timezone-aware cutoff is required')
    devices = tuple(devices)
    names = list(selected_meters)
    if not names or len(set(names)) != len(names):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Explicit distinct meter selection required')
    for bundle in (total, *devices):
        configured = [r for r in bundle.records if r.nature == 'configured']
        if (not configured or any((r.source_id, r.source_version) != (bundle.source_id, bundle.source_version)
                                  for r in configured) or
                (bundle.source_id, bundle.source_version) != (total.source_id, total.source_version)):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Meter source identity or version mismatch')
        intervals = [r.interval for r in configured]
        if (any(i is None or i.end - i.start != timedelta(minutes=15) for i in intervals) or
                len({(i.start, i.end) for i in intervals}) != len(intervals)):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Meter intervals must be unique 15-minute slots')
        if any(r.quality.flag == 'suspect' or (r.value is not None and not math.isfinite(r.value)) for r in configured):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Meter quality or value is invalid')
        if len({r.locator.meter for r in configured}) != 1 or configured[0].locator.meter is None:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Meter identity must be consistent')
    actual = []
    for bundle in devices:
        configured = [r for r in bundle.records if r.nature == 'configured']
        meters = {r.locator.meter for r in configured}
        if len(meters) != 1 or None in meters:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Device meter identity must be consistent')
        actual.extend(meters)
    if len(actual) != len(names) or set(actual) != set(names):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Device bundles do not match frozen selection')
    baseline = subtract(total, devices)
    if not baseline.records or any(r.quantity != 'baseline_load' for r in baseline.records):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Invalid baseline bundle')
    if not pv.records or any(r.quantity != 'pv_ac_power' for r in pv.records):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Invalid PV bundle')
    approvals = (tuple(approved_baseline(baseline)), tuple(approved_pv(pv)))
    for bundle, records in zip((baseline, pv), approvals):
        available = {online_record_hash(r) for r in bundle.records}
        if not records or any(online_record_hash(r) not in available or r.available_at > as_of or
                              (r.interval.end if r.interval else r.event_time) > as_of for r in records):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Approved records must belong to bundle and be visible at cutoff')
    refs = [series.write_partition(b) for b in (baseline, pv)]
    entries = [dict(quantity=q, ref=ref, source_id=b.source_id, source_version=b.source_version, basis=basis)
               for q, ref, b in zip(('baseline_load', 'pv_ac_power'), refs, (baseline, pv))]
    source_map = artifacts.prepare_series_map(entries)
    records = approvals[0] + approvals[1]
    inputs = {'online_source_ids': sorted({r.source_id for r in records}),
              'online_record_hashes': sorted({online_record_hash(r) for r in records}),
              'selected_meters': sorted(names), 'online_approved_as_of': as_of.isoformat()}
    return source_map, inputs, baseline
