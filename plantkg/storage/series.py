"""Immutable Parquet bundles queried only through fixed, scoped references."""
from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from typing import Callable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from plantkg.core.contracts import Context, ErrorCode, Interval, PlantkgError, SourceBundle, SourceRecord
from plantkg.storage.sqlite import SQLiteWriter, canonical


def _has_nonfinite(value):
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_nonfinite(k) or _has_nonfinite(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nonfinite(item) for item in value)
    return False


class ParquetSeriesStore:
    def __init__(self, path: Path, writer: SQLiteWriter, plant_id: str, resolver: Callable[[Context, str], list[str]]):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.writer, self.plant_id, self.resolver = writer, plant_id, resolver
        self._lock = threading.Lock()

    def write_partition(self, bundle: SourceBundle) -> str:
        self.writer.require_outside_transaction()
        with self._lock:
            return self._write_partition(bundle)

    def _write_partition(self, bundle: SourceBundle) -> str:
        if any((record.source_id, record.source_version) != (bundle.source_id, bundle.source_version) for record in bundle.records):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Record identity does not match its source bundle")
        if any(_has_nonfinite(step.params) for record in bundle.records for step in record.transform_steps):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Transform parameters must contain finite numbers")
        content = canonical(bundle)
        # Arbitrary params can contain values (e.g. datetime or tuple) whose
        # type would silently change in JSON. Reject such lossy bundles.
        if SourceBundle.model_validate_json(content) != bundle:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Bundle cannot round-trip without changing value types")
        identity = hashlib.sha256(content.encode("utf-8")).hexdigest()
        ref = "series:v1:" + identity
        final = self.path / (identity + ".parquet")
        with self.writer.read_only() as con:
            old = con.execute("SELECT reference FROM storage_artifacts WHERE plant_id=? AND kind='series' AND name=?", (self.plant_id, bundle.bundle_id)).fetchone()
        if old:
            if old[0] != ref:
                raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "Bundle ID already has different content")
            with self.writer.read_only() as con:
                expected = con.execute("SELECT checksum FROM storage_artifacts WHERE plant_id=? AND kind='series' AND name=?", (self.plant_id, bundle.bundle_id)).fetchone()[0]
            if not final.is_file() or hashlib.sha256(final.read_bytes()).hexdigest() != expected:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Series artifact missing or corrupt")
            return ref
        metadata = bundle.model_dump(mode="json", exclude={"records"})
        schema = pa.schema([
            ("quantity", pa.string()), ("event_time", pa.timestamp("us", tz="UTC")),
            ("interval_start", pa.timestamp("us", tz="UTC")), ("interval_end", pa.timestamp("us", tz="UTC")),
            ("available_at", pa.timestamp("us", tz="UTC")), ("record_json", pa.string()),
        ], metadata={b"plantkg_bundle": canonical(metadata).encode(), b"format_version": b"1", b"bundle_hash": identity.encode()})
        rows = [{"quantity": r.quantity, "event_time": r.event_time,
                 "interval_start": r.interval.start if r.interval else None,
                 "interval_end": r.interval.end if r.interval else None,
                 "available_at": r.available_at, "record_json": canonical(r)} for r in bundle.records]
        fd, temporary = tempfile.mkstemp(prefix=".part-", suffix=".parquet", dir=self.path)
        os.close(fd)
        try:
            pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary)
            check = pq.read_table(temporary)
            if not check.schema.equals(schema, check_metadata=True) or check.num_rows != len(rows) or check.to_pylist() != rows:
                raise ValueError("Parquet verification failed")
            digest = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
            # Hard-link publication is exclusive; an existing artifact is never overwritten.
            try:
                os.link(temporary, final)
            except FileExistsError:
                if hashlib.sha256(final.read_bytes()).hexdigest() != digest:
                    raise ValueError("Existing Parquet artifact differs")
            self.writer.submit(lambda con: con.execute("INSERT INTO storage_artifacts VALUES (?,?,?,?,?,?,?)", (self.plant_id, "series", bundle.bundle_id, ref, digest, 1, None)))
        finally:
            Path(temporary).unlink(missing_ok=True)
        return ref

    def query(self, context: Context, quantity: str, window: Interval) -> SourceBundle:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "Series plant mismatch")
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "Fixed manifest required")
        refs = self.resolver(context, quantity)
        if not refs:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "No fixed series references")
        refs = tuple(dict.fromkeys(refs))
        if len(refs) != 1:
            raise PlantkgError(ErrorCode.PRECONDITION, "Multiple partitions cannot be represented by one bundle identity")
        files = []
        metadata = []
        import json
        with self.writer.read_only() as con:
            for ref in refs:
                row = con.execute("SELECT checksum,format_version FROM storage_artifacts WHERE plant_id=? AND kind='series' AND reference=?", (self.plant_id, ref)).fetchone()
                if not row or row[1] != 1 or not re.fullmatch(r"series:v1:[0-9a-f]{64}", ref):
                    raise PlantkgError(ErrorCode.NOT_VISIBLE, "Series reference unavailable")
                path = self.path / (ref.removeprefix("series:v1:") + ".parquet")
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row[0]:
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Series artifact missing or corrupt")
                md = pq.read_schema(path).metadata or {}
                if md.get(b"format_version") != b"1" or md.get(b"bundle_hash") != ref.removeprefix("series:v1:").encode():
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Invalid series metadata")
                metadata.append(json.loads(md[b"plantkg_bundle"]))
                files.append(str(path))
        first = metadata[0]
        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute("SELECT record_json FROM read_parquet(?) WHERE quantity=? AND available_at<=? AND ((interval_start IS NOT NULL AND interval_start<? AND interval_end>?) OR (interval_start IS NULL AND event_time>=? AND event_time<?)) ORDER BY event_time, record_json", [files, quantity, context.as_of, window.end, window.start, window.start, window.end]).fetchall()
        finally:
            connection.close()
        return SourceBundle.model_validate({**first, "records": [SourceRecord.model_validate_json(row[0]) for row in rows], "next_cursor": None})
