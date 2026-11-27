"""Deterministic 15-minute persistence forecast with explicit missingness."""
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite

from plantkg.core.contracts import Forecast, ForecastBundle, InputSlice, Interval
from .history import digest

STEP = timedelta(minutes=15)
# 固定相对误差带宽，标 configured：没有历史误差校准证据，不能当作已校准区间。
DEFAULT_INTERVAL_BAND_RATIO = 0.10


@dataclass(frozen=True)
class ParameterSnapshot:
    manifest_id: str
    parameter_ref: str
    model_version: str
    pv_correction_k: float = 1.0
    pv_capacity_kw: float = 0.0
    parameter_version: str = ""
    configuration_ref: str = ""
    interval_band_ratio: float = DEFAULT_INTERVAL_BAND_RATIO


class FixedParameters:
    """Example provider; production assembly must resolve B04's frozen references."""
    def __init__(self, snapshots):
        self.snapshots = dict(snapshots)

    def get(self, manifest_id):
        return self.snapshots[manifest_id]


class PersistenceForecaster:
    def __init__(self, history_reader, parameters):
        self.history_reader, self.parameters = history_reader, parameters

    def predict(self, context, horizon, visible_history):
        meta = self.history_reader.validate(context, horizon, visible_history)
        p = (self.parameters.get_for_context(context) if hasattr(self.parameters, "get_for_context")
             else self.parameters.get(context.version_manifest_id))
        if p.manifest_id != context.version_manifest_id or not p.parameter_ref or not p.model_version:
            raise ValueError("parameter snapshot is not bound to manifest")
        if isinstance(p.pv_correction_k, bool) or not isinstance(p.pv_correction_k, (float, int)) or not isfinite(p.pv_correction_k) or p.pv_correction_k < 0:
            raise ValueError("pv correction must be finite and nonnegative")
        if isinstance(p.pv_capacity_kw, bool) or not isinstance(p.pv_capacity_kw, (float, int)) or not isfinite(p.pv_capacity_kw) or p.pv_capacity_kw <= 0:
            raise ValueError("a finite positive frozen PV capacity is required")
        if (isinstance(p.interval_band_ratio, bool) or not isinstance(p.interval_band_ratio, (float, int))
                or not isfinite(p.interval_band_ratio) or not 0 <= p.interval_band_ratio < 1):
            raise ValueError("interval band ratio must be finite and in [0, 1)")
        if context.as_of > horizon.start or (horizon.end - horizon.start) % STEP or any((x.minute % 15 or x.second or x.microsecond) for x in (horizon.start, horizon.end)):
            raise ValueError("horizon must start after issuance and align to the 15-minute UTC grid")
        index = {}
        for r in visible_history.records:
            if r.available_at > context.as_of:
                raise ValueError("history contains a future record")
            if r.nature not in ("measured", "configured", "derived") or r.unit != "kW" or r.quantity not in ("baseline_load", "pv_ac_power"):
                raise ValueError("invalid source nature, unit or quantity")
            if r.interval is None or r.interval.end - r.interval.start != STEP or r.event_time != r.interval.start or r.interval.start.minute % 15 or r.interval.start.second or r.interval.start.microsecond:
                raise ValueError("history interval is not on the 15-minute grid")
            if r.interval.end > context.as_of:
                raise ValueError("incomplete history interval")
            if r.value is not None and (not isfinite(r.value) or r.value < 0):
                raise ValueError("invalid observed power")
            key = (r.quantity, r.interval.start)
            if key in index and index[key] != r:
                raise ValueError("conflicting duplicate interval")
            index[key] = r
        forecasts = []
        summary = {}
        for source_quantity, output in (("baseline_load", "base_load"), ("pv_ac_power", "pv_available")):
            values, reasons, used = [], [], []
            lower, upper = [], []
            slots = int((horizon.end - horizon.start) / STEP)
            for n in range(slots):
                t = horizon.start + n * STEP
                chosen = None
                problems = []
                for hours in (24, 48):
                    row = index.get((source_quantity, t - timedelta(hours=hours)))
                    if row is None:
                        problems.append(f"{hours}h_absent_or_not_visible")
                    elif row.quality.flag not in ("ok", "estimated") or row.value is None:
                        problems.append(f"{hours}h_{row.quality.flag}")
                    else:
                        chosen = row
                        break
                if chosen is None:
                    values.append(None)
                    lower.append(None)
                    upper.append(None)
                    reasons.append(";".join(problems))
                else:
                    value = chosen.value * (p.pv_correction_k if output == "pv_available" else 1.0)
                    if not isfinite(value):
                        raise ValueError("corrected power is nonfinite")
                    clipped = output == "pv_available" and value > p.pv_capacity_kw
                    if clipped:
                        value = p.pv_capacity_kw
                    values.append(value)
                    band = value * p.interval_band_ratio
                    lower.append(value - band)
                    upper.append(value + band)
                    used.append(chosen)
                    reasons.append(("24h" if not problems else "48h:" + ";".join(problems)) + (":estimated" if chosen.quality.flag == "estimated" else "") + (":clipped" if clipped else ""))
            coverage = sum(v is not None for v in values) / slots
            params = {"version_manifest_id": context.version_manifest_id, "parameter_ref": p.parameter_ref, "parameter_version": p.parameter_version, "configuration_ref": p.configuration_ref, "pv_correction_k": p.pv_correction_k, "pv_capacity_kw": p.pv_capacity_kw, "interval_status": "uncalibrated", "interval_band_ratio": p.interval_band_ratio, "interval_band_nature": "configured", "slot_quality": reasons, "source_metadata": meta, "plant_id": context.plant_id, "run_id": context.run_id}
            input_slice = InputSlice(source_ids=tuple(sorted({r.source_id for r in used})), visible_until=context.as_of, window=Interval(start=min(r.interval.start for r in used), end=max(r.interval.end for r in used)) if used else None, note="B07 visible history; persistence 24h then 48h")
            payload = {"quantity": output, "issued_at": context.as_of.isoformat(), "target": horizon.model_dump(mode="json"), "model": p.model_version, "params": params, "values": values, "input": input_slice.model_dump(mode="json"), "records": [r.model_dump(mode="json") for r in used]}
            forecasts.append(Forecast(forecast_id="b14-" + digest(payload), quantity=output, unit="kW", issued_at=context.as_of, target=horizon, step_minutes=15, values=tuple(values), lower=tuple(lower), upper=tuple(upper), model_version=p.model_version, params=params, input_slice=input_slice, coverage=coverage))
            summary[output + "_coverage"] = coverage
            summary[output + "_missing_slots"] = float(values.count(None))
            summary[output + "_fallback_slots"] = float(sum(s.startswith("48h:") for s in reasons))
        summary["interval_calibrated"] = 0.0
        return ForecastBundle(forecasts=tuple(forecasts), uncertainty_summary=summary)
