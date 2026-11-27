"""Versioned named graphs; visibility is supplied by a fixed-manifest resolver."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Mapping

from pyoxigraph import NamedNode, RdfFormat, Store

from plantkg.core.contracts import Context, ErrorCode, PlantkgError
from plantkg.storage.sqlite import SQLiteWriter, canonical


class OxigraphStore:
    def __init__(self, path: Path, writer: SQLiteWriter, plant_id: str, resolver: Callable[[Context, str], str | None]):
        self.store = Store(str(path))
        self.writer, self.plant_id, self.resolver = writer, plant_id, resolver
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        # Four immutable graph snapshots, shared by all authorized requests.
        self._pages: OrderedDict[tuple[str, str], tuple] = OrderedDict()

    def _graph_checksum(self, graph):
        rows = sorted((str(q.subject), str(q.predicate), str(q.object)) for q in self.store.quads_for_pattern(None, None, None, graph))
        return hashlib.sha256(canonical(rows).encode()).hexdigest()

    def _invalidate_cache(self, ref):
        with self._cache_lock:
            for key in tuple(self._pages):
                if key[0] == ref:
                    del self._pages[key]

    def prepare(self, graph_name: str, triples_ttl: str) -> str:
        self.writer.require_outside_transaction()
        source_hash = hashlib.sha256(triples_ttl.encode("utf-8")).hexdigest()
        ref = "graph:v1:" + hashlib.sha256(canonical([self.plant_id, graph_name, source_hash]).encode()).hexdigest()
        graph = NamedNode("urn:plantkg:" + ref)
        with self._lock:
            with self.writer.read_only() as con:
                row = con.execute("SELECT reference,checksum,content_checksum FROM storage_artifacts WHERE plant_id=? AND kind='graph' AND name=?", (self.plant_id, graph_name)).fetchone()
            if row:
                if row[:2] != (ref, source_hash):
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "Graph name already has different content")
                if not self.store.contains_named_graph(graph) or row[2] != self._graph_checksum(graph):
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Graph is corrupt")
                return ref
            # Parse completely in a separate store before touching the persistent store.
            staging = Store()
            staging.load(input=triples_ttl, format=RdfFormat.TURTLE)
            # load() is transactional; the graph remains invisible until catalog insertion.
            # A previous registration failure may have left an unreachable partial attempt.
            self.store.remove_graph(graph)
            self.store.load(input=triples_ttl, format=RdfFormat.TURTLE, to_graph=graph)
            self.store.add_graph(graph)
            self.store.flush()
            content_checksum = self._graph_checksum(graph)
            self.writer.submit(lambda con: con.execute("INSERT INTO storage_artifacts VALUES (?,?,?,?,?,?,?)", (self.plant_id, "graph", graph_name, ref, source_hash, 1, content_checksum)))
        return ref

    def read_named_graph(self, context: Context, graph_name: str, query_id: str, params: Mapping[str, Any], cursor: str | None = None):
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "Graph plant mismatch")
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "Fixed manifest required")
        ref = self.resolver(context, graph_name)
        if not ref:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "Graph is not in the fixed manifest")
        if query_id != "triples" or set(params) - {"page_size"}:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Unknown graph query or parameters")
        size = params.get("page_size", 50)
        if not isinstance(size, int) or not 1 <= size <= 500:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Invalid page size")
        with self.writer.read_only() as con:
            row = con.execute("SELECT reference,content_checksum,format_version FROM storage_artifacts WHERE plant_id=? AND kind='graph' AND name=?", (self.plant_id, graph_name)).fetchone()
        if not row or row[0] != ref:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "Graph reference is unavailable")
        if row[2] != 1:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Graph format is unsupported")
        scope = hashlib.sha256(canonical([context.plant_id, context.version_manifest_id, context.as_of, graph_name, ref, query_id, params]).encode()).hexdigest()
        offset = 0
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if data["scope"] != scope or not isinstance(data["offset"], int) or data["offset"] < 0:
                    raise ValueError()
                offset = data["offset"]
            except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "Cursor does not match graph query") from None
        graph = NamedNode("urn:plantkg:" + ref)
        if not self.store.contains_named_graph(graph):
            self._invalidate_cache(ref)
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Graph is corrupt")
        cache_key = (ref, row[1])
        with self._cache_lock:
            cached = self._pages.get(cache_key)
            if cached is not None:
                rows = cached
                self._pages.move_to_end(cache_key)
            else:
                rows = tuple(sorted((str(q.subject), str(q.predicate), str(q.object)) for q in self.store.quads_for_pattern(None, None, None, graph)))
                if hashlib.sha256(canonical(rows).encode()).hexdigest() != row[1]:
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Graph is corrupt")
                self._pages[cache_key] = rows
                self._pages.move_to_end(cache_key)
                while len(self._pages) > 4:
                    self._pages.popitem(last=False)
        page = [{"subject": s, "predicate": p, "object": o} for s, p, o in rows[offset:offset + size]]
        next_offset = offset + size
        next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "offset": next_offset}).encode()).decode() if next_offset < len(rows) else None
        return page, next_cursor

    def close(self):
        with self._cache_lock:
            self._pages.clear()
        if self.store is not None:
            try:
                self.store.flush()
            finally:
                self.store = None
