"""Prepare B05 candidate from explicitly frozen, already-authorized meter bundles."""



def prepare_baseline(total_bundle, device_bundles, selected_meters, subtract_selected):
    selected_meters = tuple(selected_meters)
    if not selected_meters or len(set(selected_meters)) != len(selected_meters):
        raise ValueError("frozen meter selection must be nonempty and distinct")
    bundles = (total_bundle, *device_bundles)
    if any(b.next_cursor for b in bundles):
        raise ValueError("all meter pages must be collected")
    if len({(b.source_id, b.source_version, b.mapping_version) for b in bundles}) != 1:
        raise ValueError("meter sources and mapping versions must match")
    names = []
    normalized = []
    for b in bundles:
        rows = [r for r in b.records if r.nature == "configured"]
        if not rows or len(rows) != len(b.records):
            raise ValueError("only complete configured meter bundles are accepted")
        if any(r.source_id != b.source_id or r.source_version != b.source_version or r.interval is None or r.unit != "kW" or r.value is None or r.quality.flag != "ok" for r in rows):
            raise ValueError("missing, suspect, unbound or invalid meter record")
        meters = {r.locator.meter for r in rows}
        if len(meters) != 1 or None in meters:
            raise ValueError("each bundle must represent one meter")
        names.append(next(iter(meters)))
        normalized.append(b.model_copy(update={"records":tuple(sorted(rows, key=lambda r:r.interval.start))}))
        if len({r.interval.start for r in rows}) != len(rows):
            raise ValueError("duplicate meter intervals")
    if set(names[1:]) != set(selected_meters) or len(names[1:]) != len(selected_meters):
        raise ValueError("device bundles do not match frozen meter selection")
    return subtract_selected(normalized[0], normalized[1:])
