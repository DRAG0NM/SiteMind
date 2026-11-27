"""Read-only diagnostics for registered immutable artifacts."""
from dataclasses import dataclass
import hashlib
import re

from pyoxigraph import NamedNode


@dataclass(frozen=True)
class IntegrityResult:
    reference: str
    status: str
    detail: str


def check_references(writer, graph, series, plant_id, references):
    results = []
    for reference in references:
        with writer.read_only() as con:
            row = con.execute("SELECT kind,checksum,format_version,content_checksum FROM storage_artifacts WHERE plant_id=? AND reference=?", (plant_id, reference)).fetchone()
        if row is None:
            with writer.read_only() as con:
                other_owner = con.execute("SELECT 1 FROM storage_artifacts WHERE plant_id<>? AND reference=? LIMIT 1", (plant_id, reference)).fetchone()
            if other_owner:
                results.append(IntegrityResult(reference, "unverifiable", "Artifact belongs to another plant"))
                continue
            residual = False
            if graph is not None and re.fullmatch(r"graph:v1:[0-9a-f]{64}", reference):
                residual = graph.store.contains_named_graph(NamedNode("urn:plantkg:" + reference))
            elif series is not None and re.fullmatch(r"series:v1:[0-9a-f]{64}", reference):
                residual = (series.path / (reference.removeprefix("series:v1:") + ".parquet")).is_file()
            results.append(IntegrityResult(reference, "not_prepared" if residual else "missing", "Artifact is not registered for this plant"))
            continue
        kind, checksum, version, content_checksum = row
        if version != 1:
            results.append(IntegrityResult(reference, "unverifiable", "Unsupported format version"))
            continue
        if kind == "graph" and graph is not None:
            try:
                name = NamedNode("urn:plantkg:" + reference)
                actual = graph._graph_checksum(name)
                status = "missing" if not graph.store.contains_named_graph(name) else "complete" if actual == content_checksum else "corrupt"
                if status != "complete":
                    graph._invalidate_cache(reference)
                results.append(IntegrityResult(reference, status, "Graph content checksum"))
            except Exception as exc:
                results.append(IntegrityResult(reference, "unverifiable", type(exc).__name__))
        elif kind == "series" and series is not None and re.fullmatch(r"series:v1:[0-9a-f]{64}", reference):
            path = series.path / (reference.removeprefix("series:v1:") + ".parquet")
            if not path.is_file():
                results.append(IntegrityResult(reference, "missing", "Parquet file missing"))
            else:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != checksum:
                    results.append(IntegrityResult(reference, "corrupt", "Parquet file checksum mismatch"))
                    continue
                try:
                    import pyarrow.parquet as pq
                    metadata = pq.read_schema(path).metadata or {}
                    valid = metadata.get(b"format_version") == b"1" and metadata.get(b"bundle_hash") == reference.removeprefix("series:v1:").encode() and b"plantkg_bundle" in metadata
                    results.append(IntegrityResult(reference, "complete" if valid else "corrupt", "Parquet format and metadata"))
                except Exception as exc:
                    results.append(IntegrityResult(reference, "corrupt", type(exc).__name__))
        else:
            results.append(IntegrityResult(reference, "unverifiable", "Storage engine unavailable"))
    return tuple(results)
