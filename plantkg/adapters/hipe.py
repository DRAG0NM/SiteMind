"""HIPE interval aggregation and German-measured/Nanjing-configured views."""
import csv
import hashlib
import io
import math
import zipfile
from datetime import datetime, time, timedelta
from pathlib import Path

from plantkg.core.contracts import Locator, SourceBundle
from .common import SLOT, UTC, bundle_id, map_wall, positive, settings, source_record, step


def _open(ref):
    ref = str(ref)
    if Path(ref).is_file():
        return open(ref, "rb"), None, Path(ref).name
    # Archive paths can themselves contain !, so test candidate boundaries from right to left.
    for pos in reversed([i for i, ch in enumerate(ref) if ch == "!"]):
        archive, member = ref[:pos], ref[pos+1:]
        if Path(archive).is_file() and zipfile.is_zipfile(archive):
            z = zipfile.ZipFile(archive)
            if not member or member not in z.namelist():
                z.close()
                raise ValueError(f"ZIP member not found: {member}")
            return z.open(member), z, member
    return open(ref, "rb"), None, Path(ref).name


def _digest(ref):
    f, z, name = _open(ref)
    h = hashlib.sha256()
    try:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    finally:
        f.close()
        if z: z.close()
    return h.hexdigest(), name


def _transition(a, b, tz):
    """First instant with changed offset, to microsecond precision."""
    initial = a.astimezone(tz)
    before = (initial.utcoffset(), initial.fold)
    final = b.astimezone(tz)
    if (final.utcoffset(), final.fold) == before:
        return b
    lo, hi = a, b
    while (hi-lo).total_seconds() > 0.000001:
        mid = lo + (hi-lo)/2
        local = mid.astimezone(tz)
        if (local.utcoffset(), local.fold) == before: lo = mid
        else: hi = mid
    return hi


def _floor(t):
    return t.replace(minute=(t.minute//15)*15, second=0, microsecond=0)


def _add(bins, start, end, power, line, dependency_line, known_at, source_start, source_end, raw_start_text, raw_end_text):
    while start < end:
        bucket = _floor(start)
        part_end = min(end, bucket+SLOT)
        duration = (part_end-start).total_seconds()
        if bucket not in bins:
            bins[bucket] = [0.0, 0.0, line, dependency_line, known_at, source_start, source_end, raw_start_text, raw_end_text]
        b = bins[bucket]
        b[0] += power * duration / 3600
        b[1] += duration
        b[2] = min(b[2],line)
        b[3] = max(b[3],dependency_line)
        b[4] = max(b[4],known_at)
        b[5] = min(b[5],source_start)
        b[6] = max(b[6],source_end)
        b[8] = raw_end_text
        start = part_end


class HipeAdapter:
    name = "hipe"

    def normalize(self, source_ref, mapping, visibility):
        src_date, plant_date, days, delay, src_tz, dst_tz, target = settings(mapping, visibility, "Europe/Berlin")
        if str(src_tz) != "Europe/Berlin":
            raise ValueError("HIPE source timezone must be Europe/Berlin")
        factor = positive(mapping.get("load_scale", {}).get("factor", 25), "load_scale.factor")
        digest, member = _digest(source_ref)
        sid = mapping.get("source_id", "hipe")
        version = mapping.get("source_version", "1.0.1")
        raw_start = datetime.combine(src_date, time.min, src_tz).astimezone(UTC)
        raw_end = datetime.combine(src_date + timedelta(days=days), time.min, src_tz).astimezone(UTC)
        target_end = target+timedelta(days=days)
        raw_bins, target_bins = {}, {}
        prev = None
        machine = None
        duplicates = dropped = 0
        dropped_intervals = []  # 相邻丢弃段合并：源 UTC 区间 + 一基行号范围 + 源功率算出的电量
        dropped_energy_kwh = 0.0
        f, z, _ = _open(source_ref)
        try:
            with io.TextIOWrapper(f, encoding="utf-8-sig", newline="") as text:
                reader = csv.DictReader(text)
                if not reader.fieldnames or not {"SensorDateTime", "Machine", "P_kW"}.issubset(reader.fieldnames):
                    raise ValueError("HIPE missing required columns")
                for row in reader:
                    line = reader.line_num
                    instant = datetime.fromisoformat(row["SensorDateTime"])
                    if instant.tzinfo is None or instant.utcoffset() is None:
                        raise ValueError(f"missing HIPE offset at line {line}")
                    ts = instant.astimezone(UTC)
                    local = ts.astimezone(src_tz)
                    if local.utcoffset() != instant.utcoffset() or local.replace(tzinfo=None) != instant.replace(tzinfo=None):
                        raise ValueError(f"invalid Berlin offset at line {line}")
                    if machine is None: machine = row["Machine"]
                    if not machine or row["Machine"] != machine:
                        raise ValueError(f"inconsistent machine at line {line}")
                    power = None if row["P_kW"].strip() == "" else float(row["P_kW"])
                    if power is not None and not math.isfinite(power):
                        raise ValueError(f"nonfinite P_kW at line {line}")
                    if prev:
                        old_t, old_p, old_line, old_text, old_row = prev
                        if ts < old_t: raise ValueError(f"out of order HIPE row at line {line}")
                        if ts == old_t:
                            if old_row != row: raise ValueError(f"conflicting HIPE duplicate at line {line}")
                            duplicates += 1
                            continue
                        end = min(ts, old_t+timedelta(seconds=60), raw_end)
                        begin = max(old_t, raw_start)
                        if old_p is not None and begin < end:
                            _add(raw_bins, begin, end, old_p, old_line, line, ts, begin, end, old_text, row["SensorDateTime"])
                            segment = begin
                            while segment < end:
                                clipped = min(end, _transition(segment, end, src_tz))
                                if clipped <= segment:
                                    raise ValueError("unable to split DST transition")
                                if segment.astimezone(src_tz).fold == 0:
                                    mapped = map_wall(segment, src_date, plant_date, src_tz, dst_tz)
                                    mapped_end = mapped + (clipped-segment)
                                    left, right = max(mapped,target), min(mapped_end,target_end)
                                    if left < right:
                                        _add(target_bins,left,right,old_p,old_line,line, max(mapped_end, map_wall(ts, src_date, plant_date, src_tz, dst_tz)), segment, clipped, old_text, row["SensorDateTime"])
                                else:
                                    dropped += 1
                                    kwh = old_p * (clipped-segment).total_seconds() / 3600
                                    dropped_energy_kwh += kwh
                                    last = dropped_intervals[-1] if dropped_intervals else None
                                    if last and last["source_interval_utc"][1] == segment.isoformat():
                                        last["source_interval_utc"][1] = clipped.isoformat()
                                        last["source_line_end"] = line
                                        last["energy_kwh"] = round(last["energy_kwh"] + kwh, 6)
                                    else:
                                        dropped_intervals.append({"source_interval_utc": [segment.isoformat(), clipped.isoformat()],
                                                                  "source_line_start": old_line, "source_line_end": line,
                                                                  "energy_kwh": round(kwh, 6)})
                                segment = clipped
                    prev = (ts,power,line,row["SensorDateTime"],row)
        finally:
            if z: z.close()
        if machine is None: raise ValueError("HIPE file has no data rows")
        out=[]
        def build(bins, start, end, nature, multiplier):
            cursor=start
            while cursor < end:
                b=bins.get(cursor)
                coverage = b[1] if b else 0.0
                complete = abs(coverage-900) < 0.001
                if coverage > 900.001: raise ValueError("overlapping contributions in a bucket")
                val = b[0] * multiplier / (coverage/3600) if complete else None
                loc = Locator(meter=machine, line_start=b[2], line_end=b[3]) if b else Locator(meter=machine)
                params = {"member":member,"source_sha256":digest,"source_line_start":b[2] if b else None,
                    "source_line_end":b[3] if b else None,
                    "source_interval_utc": [b[5].isoformat(), b[6].isoformat()] if b else None,
                    "source_interval_local": [b[5].astimezone(src_tz).isoformat(), b[6].astimezone(src_tz).isoformat()] if b else None,
                    "source_timestamp_text_first_and_dependency_last": [b[7], b[8]] if b else None,
                    "coverage_seconds":round(coverage,6),
                    "known_energy_kwh":b[0]*multiplier if b else 0.0,"factor":multiplier,
                    "source_tz":"Europe/Berlin","source_start":src_date.isoformat(),
                    "plant_start":plant_date.isoformat(),"dst_policy":"keep_first" if nature == "configured" else "retain_both",
                    "dropped_repeat_segments":dropped,"duplicate_rows":duplicates,"source_resolution":"irregular",
                    "dropped_repeat_intervals":dropped_intervals,"dropped_energy_kwh":round(dropped_energy_kwh,6)}
                out.append(source_record(source_id=sid,source_version=version,digest=digest,locator=loc,nature=nature,
                    start=cursor,end=cursor+SLOT,available=max(cursor+SLOT, b[4] if b else cursor+SLOT)+delay,source_tz="Europe/Berlin",
                    source_unit="kW",quantity="active_power",value=val,
                    steps=[step("hipe_resample_map_scale",params)]))
                cursor+=SLOT
        build(raw_bins,raw_start,raw_end,"measured",1)
        build(target_bins,target,target_end,"configured",factor)
        return SourceBundle(bundle_id=mapping.get("bundle_id") or bundle_id("hipe", {"digest":digest, "member":member, "machine":machine, "source_id":sid, "source_version":version, "mapping_version":mapping.get("mapping_version", "b05-1"), "source_start":src_date.isoformat(), "plant_start":plant_date.isoformat(), "days":days, "source_tz":str(src_tz), "plant_tz":str(dst_tz), "factor":factor, "delay_seconds":delay.total_seconds(), "policy":"hipe-resample-keep-first-v1"}), source_id=sid,
            source_version=version,mapping_version=mapping.get("mapping_version","b05-1"),records=tuple(out),
            license="CC BY 4.0",note=f"{machine}: raw measured Germany; target configured, factor={factor}")
