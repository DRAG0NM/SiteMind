"""Shared mapping, validation and provenance helpers."""
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo

from plantkg.core.contracts import Interval, Quality, SourceRecord, TransformStep

UTC = timezone.utc
SLOT = timedelta(minutes=15)


def positive(value, name):
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not isfinite(n) or n <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return n


def required(mapping, key, prefix="mapping"):
    if not isinstance(mapping, Mapping) or key not in mapping:
        raise ValueError(f"missing required {prefix}.{key}")
    return mapping[key]


def bundle_id(kind, config):
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    return f"b05-{kind}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def settings(mapping, visibility, source_tz):
    try:
        start = date.fromisoformat(required(mapping, "source_start"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid mapping.source_start") from exc
    try:
        plant = date.fromisoformat(required(mapping, "plant_start"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid mapping.plant_start") from exc
    n = mapping.get("days", 7)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 366:
        raise ValueError("days must be an integer between 1 and 366")
    try:
        delay = float(visibility.get("delay_seconds", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid visibility.delay_seconds") from exc
    if not isfinite(delay) or delay < 0:
        raise ValueError("delay_seconds must be finite and nonnegative")
    src = ZoneInfo(mapping.get("source_tz", source_tz))
    dst = ZoneInfo(mapping.get("plant_tz", "Asia/Shanghai"))
    target = datetime.combine(plant, datetime.min.time(), dst).astimezone(UTC)
    return start, plant, n, timedelta(seconds=delay), src, dst, target


def map_wall(instant, source_start, plant_start, src_tz, dst_tz):
    local = instant.astimezone(src_tz)
    shift = plant_start - source_start
    mapped_date = local.date() + shift
    wall = datetime.combine(mapped_date, local.timetz().replace(tzinfo=None), dst_tz)
    mapped = wall.astimezone(UTC)
    if mapped.astimezone(dst_tz).replace(tzinfo=None) != wall.replace(tzinfo=None):
        raise ValueError("target local time does not exist")
    if wall.replace(fold=0).utcoffset() != wall.replace(fold=1).utcoffset():
        raise ValueError("ambiguous target local time is unsupported")
    return mapped


def source_record(*, source_id, source_version, digest, locator, nature, start, end,
                  available, source_tz, source_unit, quantity, value, steps, quality=None):
    if available < end:
        raise ValueError("available_at precedes complete input interval")
    return SourceRecord(source_id=source_id, source_version=source_version,
        content_hash="sha256:" + digest, locator=locator, nature=nature,
        event_time=start, interval=Interval(start=start, end=end), available_at=available,
        source_tz=source_tz, source_unit=source_unit, quantity=quantity, unit="kW",
        value=value, quality=Quality(flag=quality or ("missing" if value is None else "ok")),
        transform_steps=tuple(steps))


def step(op, params, note=None):
    return TransformStep(op=op, params=params, version="b05-1", note=note)
