"""PVGIS 5.3 ERA5 historical observations, scaled once for the target plant."""
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from plantkg.core.contracts import Interval, Locator, SourceBundle
from .common import SLOT, UTC, bundle_id, map_wall, positive, required, settings, source_record, step


class PvgisAdapter:
    name = "pvgis"

    def normalize(self, source_ref, mapping, visibility):
        raw = Path(source_ref).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        inputs = required(data, "inputs", "response")
        meta = required(required(required(data, "meta", "response"), "outputs", "response.meta"), "hourly", "response.meta.outputs")
        meteo = required(inputs, "meteo_data", "response.inputs")
        module = required(inputs, "pv_module", "response.inputs")
        variables = required(meta, "variables", "response.meta.outputs.hourly")
        p_meta = required(variables, "P", "response.meta.outputs.hourly.variables")
        hourly = required(required(data, "outputs", "response"), "hourly", "response.outputs")
        if not isinstance(hourly, list):
            raise ValueError("response.outputs.hourly must be a list")
        if (required(meteo, "radiation_db", "response.inputs.meteo_data") != "PVGIS-ERA5" or
            required(meta, "timestamp", "response.meta.outputs.hourly") != "hourly averages" or required(p_meta, "units", "response.meta.outputs.hourly.variables.P") != "W"):
            raise ValueError("unsupported PVGIS database, timestamp semantics or unit")
        if mapping.get("pvgis_version") != "5.3":
            raise ValueError("explicit pvgis_version=5.3 required")
        source_kwp = positive(required(mapping, "source_peak_kwp"), "source_peak_kwp")
        target_kwp = positive(required(mapping, "target_peak_kwp"), "target_peak_kwp")
        if source_kwp != positive(required(module, "peak_power", "response.inputs.pv_module"), "response.inputs.pv_module.peak_power"):
            raise ValueError("source capacity disagrees with PVGIS response")
        src_date, plant_date, days, delay, src_tz, dst_tz, target = settings(mapping, visibility, "Asia/Shanghai")
        if str(src_tz) != "Asia/Shanghai":
            raise ValueError("PVGIS source wall clock must be Asia/Shanghai for this mapping")
        stop = target + timedelta(days=days)
        slots = {}
        seen = set()
        sid = mapping.get("source_id", "pvgis")
        version = mapping.get("source_version", "5.3-era5")
        for index, row in enumerate(hourly):
            raw_time = required(row, "time", f"response.outputs.hourly[{index}]")
            try:
                t = datetime.strptime(raw_time, "%Y%m%d:%H%M").replace(tzinfo=UTC)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid response.outputs.hourly[{index}].time") from exc
            if t.minute != 30:
                raise ValueError("PVGIS ERA5 timestamp must be HH:30")
            if t in seen:
                raise ValueError(f"duplicate PVGIS hour at index {index}")
            seen.add(t)
            source_start = t - timedelta(minutes=30)
            source_end = t + timedelta(minutes=30)
            value = row.get("P")
            if value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid P at index {index} time {row['time']}") from exc
                from math import isfinite
                if not isfinite(value):
                    raise ValueError(f"P must be finite at index {index} time {row['time']}")
                if value < 0:
                    raise ValueError(f"negative P at index {index} time {row['time']}")
                value = value / 1000 * target_kwp / source_kwp
            mapped = map_wall(source_start, src_date, plant_date, src_tz, dst_tz)
            if mapped >= stop or mapped + timedelta(hours=1) <= target:
                continue
            available = mapped + timedelta(hours=1) + delay
            params = {"source_index_zero_based": index, "source_time": row["time"],
                "source_interval_utc": [source_start.isoformat(), source_end.isoformat()],
                "source_resolution": "1h", "assumption": "pvgis-era5-p-centered-hour-v1",
                "source_peak_kwp": source_kwp, "target_peak_kwp": target_kwp,
                "capacity_ratio": target_kwp/source_kwp, "source_sha256": digest}
            for quarter in range(4):
                start = mapped + quarter * SLOT
                if not target <= start < stop:
                    continue
                if start in slots:
                    raise ValueError("multiple source hours map to one target slot")
                slots[start] = source_record(source_id=sid, source_version=version, digest=digest,
                    locator=Locator(time_range=Interval(start=source_start,end=source_end)), nature="derived",
                    start=start, end=start+SLOT, available=available, source_tz="UTC", source_unit="W",
                    quantity="pv_ac_power", value=value, quality="estimated" if value is not None else "missing",
                    steps=[step("pvgis_hour_to_target_kw", {**params,"quarter":quarter},
                        "Hourly P represented as a centered hourly average; assumption, not measured 15 minute power")])
        records = []
        for i in range(days * 96):
            start = target + i*SLOT
            if start not in slots:
                # Unknown source slot stays unknown; this is not an observed zero.
                slots[start] = source_record(source_id=sid, source_version=version, digest=digest,
                    locator=Locator(meter="PVGIS"), nature="derived", start=start, end=start+SLOT,
                    available=start.replace(minute=0, second=0, microsecond=0)+timedelta(hours=1)+delay, source_tz="UTC", source_unit="W", quantity="pv_ac_power", value=None,
                    steps=[step("source_missing", {"source_sha256":digest})])
            records.append(slots[start])
        return SourceBundle(bundle_id=mapping.get("bundle_id") or bundle_id("pvgis", {"digest":digest, "source_id":sid, "source_version":version, "mapping_version":mapping.get("mapping_version", "b05-1"), "source_start":src_date.isoformat(), "plant_start":plant_date.isoformat(), "days":days, "source_tz":str(src_tz), "plant_tz":str(dst_tz), "source_peak_kwp":source_kwp, "target_peak_kwp":target_kwp, "delay_seconds":delay.total_seconds(), "pvgis_version":"5.3", "policy":"pvgis-era5-p-centered-hour-v1"}), source_id=sid,
            source_version=version, mapping_version=mapping.get("mapping_version", "b05-1"),
            records=tuple(records), license="PVGIS usage conditions", note="PVGIS derived; target kWp scaling applied once in B05")
