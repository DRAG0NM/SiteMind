from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.ports import GraphStore, Repository, SeriesStore
from plantkg.storage.graph import OxigraphStore
from plantkg.storage.series import ParquetSeriesStore
from plantkg.storage.sqlite import SQLiteRepository


def test_formal_assembly_and_shutdown():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        repository = services.registry.resolve(Repository)
        graph = services.registry.resolve(GraphStore)
        series = services.registry.resolve(SeriesStore)
        assert isinstance(repository, SQLiteRepository)
        assert isinstance(graph, OxigraphStore)
        assert isinstance(series, ParquetSeriesStore)
        assert repository.writer is graph.writer is series.writer
        root = repository.writer.path.parent
        assert root.exists()
    finally:
        services.close()
        services.close()
    assert not root.exists()


def test_health_reports_disabled_and_unchecked_storage():
    from starlette.testclient import TestClient
    from plantkg.api import create_app
    for enabled, expected in ((False, "disabled"), (True, "unchecked")):
        settings = load_settings(profile="mock", overrides={"storage.enabled": enabled})
        services = build_services(settings)
        with TestClient(create_app(services)) as client:
            health = client.get("/api/v1/health").json()
            assert health["dependencies"]["storage"] == expected
            assert health["status"] == "degraded"


def test_dev_health_and_reopen(tmp_path):
    from starlette.testclient import TestClient
    from plantkg.api import create_app
    overrides = {"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                 "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")}
    settings = load_settings(profile="dev", overrides=overrides)
    for _ in range(2):
        services = build_services(settings)
        with TestClient(create_app(services)) as client:
            result = client.get("/api/v1/health")
            assert result.status_code == 200
            assert result.json()["dependencies"]["storage"] == "unchecked"


def test_fixed_references_survive_new_artifacts_and_restart(tmp_path):
    from datetime import datetime, timezone
    from plantkg.core.contracts import Context, Interval, SourceBundle, SourceRecord
    from plantkg.storage.integrity import check_references

    class Resolver:
        graph_ref = None
        series_ref = None
        def graph(self, context, graph_name):
            return self.graph_ref if context.version_manifest_id == "old" and graph_name == "old" else None
        def series(self, context, quantity):
            return [self.series_ref] if context.version_manifest_id == "old" and self.series_ref else []

    settings = load_settings(profile="dev", overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"), "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    resolver = Resolver()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    context = Context(plant_id=settings.plant_id, industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=now, version_manifest_id="old")
    record = SourceRecord(source_id="s", source_version="v1", content_hash="sha256:" + "a" * 64, locator={"line_start": 1}, nature="configured", event_time=now, available_at=now, source_tz="UTC", source_unit="kW", quantity="power", unit="kW", value=3)
    bundle = SourceBundle(bundle_id="b1", source_id="s", source_version="v1", mapping_version="m", records=(record,))
    services = build_services(settings, storage_resolver=resolver)
    try:
        graph = services.registry.resolve(GraphStore)
        series = services.registry.resolve(SeriesStore)
        resolver.graph_ref = graph.prepare("old", '<urn:a> <urn:p> "old" .')
        resolver.series_ref = series.write_partition(bundle)
        graph.prepare("new", '<urn:a> <urn:p> "new" .')
        series.write_partition(bundle.model_copy(update={"bundle_id": "b2", "records": (record.model_copy(update={"value": 4}),)}))
        statuses = check_references(services.storage.writer, graph, series, settings.plant_id, [resolver.graph_ref, resolver.series_ref, "missing"])
        assert [item.status for item in statuses] == ["complete", "complete", "missing"]
    finally:
        services.close()
    services = build_services(settings, storage_resolver=resolver)
    try:
        graph = services.registry.resolve(GraphStore)
        series = services.registry.resolve(SeriesStore)
        assert '"old"' in graph.read_named_graph(context, "old", "triples", {})[0][0]["object"]
        assert series.query(context, "power", Interval(start=now, end=now.replace(hour=1))).records == (record,)
    finally:
        services.close()


def test_dev_http_server_start_stop_and_reopen(tmp_path):
    import socket
    import threading
    import time
    import urllib.request
    import uvicorn
    from plantkg.api import create_app
    from plantkg.mcp_server import create_mcp_server

    settings = load_settings(profile="dev", overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "db"), "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    for _ in range(2):
        services = build_services(settings)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                threading.Event().wait(0.02)
            assert server.started
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5) as response:
                assert response.status == 200
                assert b'"storage":"unchecked"' in response.read()
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
            services.close()
        assert not thread.is_alive()


def test_dev_cli_dry_run_persistent_reopen(tmp_path):
    import subprocess
    import sys
    command = [sys.executable, "-m", "plantkg", "--profile", "dev", "--dry-run",
               "--set", "storage.enabled=true", "--set", f"storage.sqlite={tmp_path / 'db'}",
               "--set", f"storage.graph={tmp_path / 'graph'}", "--set", f"storage.series={tmp_path / 'series'}"]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        assert '"storage": "unchecked"' in result.stdout


def test_mcp_lifespan_and_startup_failure_cleanup(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from starlette.testclient import TestClient
    import pytest
    from plantkg.api import create_app
    from plantkg.storage import graph as graph_module
    from plantkg.storage.sqlite import SQLiteWriter

    settings = load_settings(profile="dev", overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "db"), "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    events = []
    class McpApp:
        @asynccontextmanager
        async def lifespan(self, app):
            events.append("start")
            try:
                yield
            finally:
                events.append("stop")
        async def __call__(self, scope, receive, send):
            raise AssertionError("not called")
    class Mcp:
        def http_app(self, **kwargs):
            return McpApp()
    services = build_services(settings)
    with TestClient(create_app(services, Mcp())) as client:
        assert client.get("/api/v1/health").status_code == 200
    assert events == ["start", "stop"]
    original = graph_module.OxigraphStore
    monkeypatch.setattr(graph_module, "OxigraphStore", lambda *args: (_ for _ in ()).throw(RuntimeError("injected initialization failure")))
    with pytest.raises(RuntimeError, match="initialization failure"):
        build_services(settings)
    monkeypatch.setattr(graph_module, "OxigraphStore", original)
    writer = SQLiteWriter(tmp_path / "db")
    writer.close()
