"""Construction and lifecycle for B03 storage resources."""
from __future__ import annotations

import tempfile
from pathlib import Path

from plantkg.core.ports import GraphStore, Repository, SeriesStore
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteRepository, SQLiteWriter
from plantkg.storage.references import DenyReferences


class StorageResources:
    def __init__(self, settings, registry, resolver=None):
        config = settings.section("storage") or {}
        if not isinstance(config, dict) or not isinstance(config.get("enabled", False), bool):
            raise ValueError("storage.enabled must be a boolean")
        self.enabled = config.get("enabled", False)
        self.writer = self.graph = self.temp = None
        if not self.enabled:
            return
        from plantkg.storage.graph import OxigraphStore
        from plantkg.storage.series import ParquetSeriesStore

        try:
            base = Path(__file__).resolve().parents[2]
            if settings.profile == "mock":
                self.temp = tempfile.TemporaryDirectory(prefix="plantkg-storage-")
                base = Path(self.temp.name)
                sqlite_path, graph_path, series_path = base / "catalog.db", base / "graph", base / "series"
            else:
                def path(name):
                    value = config.get(name)
                    if not isinstance(value, str) or not value:
                        raise ValueError(f"storage.{name} must be a path")
                    candidate = Path(value)
                    return candidate if candidate.is_absolute() else base / candidate
                sqlite_path, graph_path, series_path = path("sqlite"), path("graph"), path("series")
            self.writer = SQLiteWriter(sqlite_path)
            MigrationRunner(self.writer).apply()
            with self.writer.read_only() as con:
                formats = set(con.execute("SELECT kind,format_version FROM storage_formats"))
            if formats != {("graph", 1), ("series", 1)}:
                raise ValueError("Storage format history is unsupported or incomplete")
            resolver = resolver or DenyReferences()
            self.graph = OxigraphStore(graph_path, self.writer, settings.plant_id, resolver.graph)
            self.series = ParquetSeriesStore(series_path, self.writer, settings.plant_id, resolver.series)
            registry.register(Repository, SQLiteRepository(self.writer, settings.plant_id))
            registry.register(GraphStore, self.graph)
            registry.register(SeriesStore, self.series)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.graph is not None:
                try:
                    self.graph.close()
                finally:
                    self.graph = None
        finally:
            if self.writer is not None:
                self.writer.close()
                self.writer = None
            if self.temp is not None:
                self.temp.cleanup()
                self.temp = None
