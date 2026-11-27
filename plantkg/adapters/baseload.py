"""Traceable net-load candidates; no production operation is inferred."""
from plantkg.core.contracts import LEDGER_TOLERANCE, SourceBundle
from .common import bundle_id, source_record, step


def selected_device_baseload(total_bundle, device_bundles):
    """Subtract each explicitly selected configured meter once, preserving provenance."""
    if not device_bundles:
        raise ValueError("at least one explicitly selected meter is required")
    def configured(bundle):
        return [r for r in bundle.records if r.nature == "configured"]
    total = configured(total_bundle)
    selected = [configured(bundle) for bundle in device_bundles]
    if not total or any(len(rows) != len(total) for rows in selected):
        raise ValueError("configured meter series must be present and aligned")
    # Canonical order keeps both values and provenance stable for an unordered selection.
    selected.sort(key=lambda rows: rows[0].locator.meter or "")
    names = [rows[0].locator.meter for rows in selected]
    if None in names or len(set(names)) != len(names) or total[0].locator.meter in names:
        raise ValueError("meter selections must be distinct and exclude the total")
    records=[]
    for n, main in enumerate(total):
        parts=[rows[n] for rows in selected]
        if any(r.interval != main.interval for r in parts):
            raise ValueError("source intervals are not aligned")
        if any(r.unit != main.unit or r.quantity != main.quantity for r in parts):
            raise ValueError("source quantity or unit differs")
        inputs=[main]+parts
        factors=[r.transform_steps[-1].params.get("factor") for r in inputs]
        if any(f is None for f in factors) or len(set(factors))!=1:
            raise ValueError("source meter scaling factors differ")
        value=None if any(r.value is None for r in inputs) else main.value-sum(r.value for r in parts)
        raw_residual = value
        tolerance = LEDGER_TOLERANCE["power_zero_kw"]
        if value is not None:
            if abs(value) <= tolerance:
                value = 0.0
            elif value < -tolerance:
                raise ValueError(f"selected meters {names} exceed total at {main.event_time}: residual {value} kW")
        provenance=[{'source_id':r.source_id,'source_version':r.source_version,
            'content_hash':r.content_hash,'meter':r.locator.meter,
            'line_start':r.locator.line_start,'line_end':r.locator.line_end,
            'available_at':r.available_at.isoformat()} for r in inputs]
        records.append(source_record(source_id=main.source_id,source_version=main.source_version,
            digest=main.content_hash.removeprefix('sha256:'),locator=main.locator,nature='configured',
            start=main.interval.start,end=main.interval.end,available=max(r.available_at for r in inputs),
            source_tz=main.source_tz,source_unit='kW',quantity='baseline_load',value=value,
            steps=[*main.transform_steps,step('subtract_selected_meters',{'inputs':provenance,
                'selected_meters':names,'factor':factors[0], 'raw_residual_kw':raw_residual,
                'power_zero_kw':tolerance, 'tolerance_version':LEDGER_TOLERANCE['policy_version'],
                'purpose':'candidate baseline; not a schedule'})]))
    return SourceBundle(bundle_id=bundle_id('baseline', {'total':total_bundle.bundle_id, 'devices':sorted(b.bundle_id for b in device_bundles), 'meters':sorted(names), 'policy':LEDGER_TOLERANCE['policy_version']}),source_id=total_bundle.source_id,
        source_version=total_bundle.source_version,mapping_version=total_bundle.mapping_version,records=tuple(records),
        license=total_bundle.license,note='Selected meter deduction once; candidate only; B13/B14 must confirm Q25')
