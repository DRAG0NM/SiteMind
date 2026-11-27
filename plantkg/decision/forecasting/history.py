"""Collect complete pages from the trusted B07 port, preserving query provenance."""
import hashlib
import json
from datetime import timedelta

from plantkg.core.contracts import Interval, SourceBundle


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class HistoryReader:
    def __init__(self, query):
        self.query = query
        self._issued = {}

    def read(self, context, horizon):
        if not context.run_id or not context.version_manifest_id:
            raise ValueError("run and manifest are required")
        start = horizon.start - timedelta(hours=48)
        end = min(horizon.end - timedelta(hours=24), context.as_of)
        if end <= start:
            raise ValueError("history window is empty")
        window = Interval(start=start, end=end)
        all_records = []
        meta = []
        for quantity in ("baseline_load", "pv_ac_power"):
            cursor = None
            seen = set()
            first = None
            while True:
                page = self.query.query(context, quantity, window, cursor)
                key = (page.source_id, page.source_version, page.mapping_version)
                if first is None:
                    first = key
                elif key != first:
                    raise ValueError("source version changed across pages")
                if any((r.source_id, r.source_version) != key[:2] or r.quantity != quantity for r in page.records):
                    raise ValueError("query returned mismatched source or quantity")
                all_records.extend(page.records)
                cursor = page.next_cursor
                if cursor is None:
                    break
                if cursor in seen:
                    raise ValueError("repeated page cursor")
                seen.add(cursor)
            meta.append([quantity, *first])
        records = tuple(sorted(all_records, key=lambda r: json.dumps(r.model_dump(mode="json"), sort_keys=True)))
        identity = digest([context.plant_id, context.run_id, context.version_manifest_id, context.as_of.isoformat(), horizon.model_dump(mode="json"), meta, [r.model_dump(mode="json") for r in records]])
        bundle = SourceBundle(bundle_id="b14-" + identity, source_id="b14-query", source_version="1", mapping_version="1", records=records)
        self._issued[identity] = (context.plant_id, context.run_id, context.version_manifest_id, context.as_of, horizon, digest(bundle.model_dump(mode="json")), meta)
        return bundle

    def validate(self, context, horizon, bundle):
        identity = bundle.bundle_id.removeprefix("b14-")
        saved = self._issued.get(identity)
        if saved is None or saved[:5] != (context.plant_id, context.run_id, context.version_manifest_id, context.as_of, horizon) or saved[5] != digest(bundle.model_dump(mode="json")):
            raise ValueError("history was not obtained by this reader for the bound run and horizon")
        return saved[6]
