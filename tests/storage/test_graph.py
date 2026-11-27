from datetime import datetime, timezone

import pytest

from plantkg.core.contracts import Context, PlantkgError
from plantkg.storage.graph import OxigraphStore
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


@pytest.fixture
def writer(tmp_path):
    instance = SQLiteWriter(tmp_path / "catalog.db")
    yield instance
    instance.close()


def test_graph_fixed_reference_and_reopen(tmp_path, writer):
    MigrationRunner(writer).apply()
    refs = {}
    context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=datetime.now(timezone.utc), version_manifest_id="v1")
    store = OxigraphStore(tmp_path / "graph", writer, "p", lambda context, name: refs.get(name))
    ttl = '<urn:a> <urn:p> "first" . <urn:b> <urn:p> "second" .'
    ref = store.prepare("v1", ttl)
    assert store.prepare("v1", ttl) == ref
    with pytest.raises(PlantkgError):
        store.read_named_graph(context, "v1", "triples", {})
    refs["v1"] = ref
    page, cursor = store.read_named_graph(context, "v1", "triples", {"page_size": 1})
    assert len(page) == 1 and cursor
    assert len(store.read_named_graph(context, "v1", "triples", {"page_size": 1}, cursor)[0]) == 1
    with pytest.raises(PlantkgError):
        store.read_named_graph(context, "v1", "triples", {"page_size": 2}, cursor)
    with pytest.raises(PlantkgError):
        store.read_named_graph(context.model_copy(update={"version_manifest_id": "other"}), "v1", "triples", {"page_size": 1}, cursor)
    with pytest.raises(PlantkgError):
        store.read_named_graph(context, "v1", "triples", {"page_size": 1}, "not-base64")
    with pytest.raises(PlantkgError):
        store.prepare("v1", '<urn:a> <urn:p> "changed" .')
    with pytest.raises(SyntaxError):
        store.prepare("bad", "not turtle")
    store.close()
    store = OxigraphStore(tmp_path / "graph", writer, "p", lambda context, name: refs.get(name))
    assert store.read_named_graph(context, "v1", "triples", {"page_size": 1})[0] == page
    store.close()


def test_unregistered_graph_is_not_visible_and_empty_graph_loss_detected(tmp_path, writer, monkeypatch):
    from plantkg.storage.integrity import check_references
    MigrationRunner(writer).apply()
    refs = {}
    context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=datetime.now(timezone.utc), version_manifest_id="v1")
    store = OxigraphStore(tmp_path / "graph", writer, "p", lambda context, name: refs.get(name))
    try:
        original = writer.submit
        monkeypatch.setattr(writer, "submit", lambda fn: (_ for _ in ()).throw(RuntimeError("registration failed")))
        with pytest.raises(RuntimeError, match="registration failed"):
            store.prepare("orphan", '<urn:a> <urn:p> "x" .')
        orphan = str(next(store.store.named_graphs())).removeprefix("<urn:plantkg:").removesuffix(">")
        refs["orphan"] = orphan
        assert check_references(writer, store, None, "p", [orphan])[0].status == "not_prepared"
        with pytest.raises(PlantkgError):
            store.read_named_graph(context, "orphan", "triples", {})
        monkeypatch.setattr(writer, "submit", original)
        assert store.prepare("orphan", '<urn:a> <urn:p> "x" .') == orphan
        empty = store.prepare("empty", "")
        refs["empty"] = empty
        assert store.read_named_graph(context, "empty", "triples", {}) == ([], None)
        from pyoxigraph import NamedNode
        store.store.remove_graph(NamedNode("urn:plantkg:" + empty))
        assert check_references(writer, store, None, "p", [empty])[0].status == "missing"
        with pytest.raises(PlantkgError):
            store.read_named_graph(context, "empty", "triples", {})
    finally:
        store.close()


def test_import_failure_after_persistent_write_stays_invisible(tmp_path, writer):
    MigrationRunner(writer).apply()
    refs = {}
    context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=datetime.now(timezone.utc), version_manifest_id="v")
    store = OxigraphStore(tmp_path / "graph", writer, "p", lambda context, name: refs.get(name))
    try:
        real = store.store
        class FailingImport:
            def __getattr__(self, name):
                return getattr(real, name)
            def load(self, *args, **kwargs):
                real.load(*args, **kwargs)
                raise RuntimeError("injected import failure")
        store.store = FailingImport()
        with pytest.raises(RuntimeError, match="injected"):
            store.prepare("broken", '<urn:a> <urn:p> "x" .')
        orphan = str(next(real.named_graphs())).removeprefix("<urn:plantkg:").removesuffix(">")
        refs["broken"] = orphan
        store.store = real
        with pytest.raises(PlantkgError):
            store.read_named_graph(context, "broken", "triples", {})
        assert store.prepare("broken", '<urn:a> <urn:p> "x" .') == orphan
        assert len(store.read_named_graph(context, "broken", "triples", {})[0]) == 1
    finally:
        store.close()


def test_manifest_resolver_controls_graph_visibility_at_as_of(tmp_path, writer):
    from datetime import timedelta
    MigrationRunner(writer).apply()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    refs = {}
    def resolve(context, name):
        return refs.get(name) if context.version_manifest_id == "v" and context.as_of >= start else None
    store = OxigraphStore(tmp_path / "graph", writer, "p", resolve)
    try:
        refs["v"] = store.prepare("v", '<urn:a> <urn:p> "x" .')
        context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=start, version_manifest_id="v")
        with pytest.raises(PlantkgError):
            store.read_named_graph(context.model_copy(update={"as_of": start - timedelta(seconds=1)}), "v", "triples", {})
        assert len(store.read_named_graph(context, "v", "triples", {})[0]) == 1
    finally:
        store.close()


def test_pagination_shares_graph_snapshot_across_request_scopes(tmp_path, writer):
    from datetime import timedelta
    from plantkg.storage.integrity import check_references
    MigrationRunner(writer).apply()
    refs = {}
    context = Context(plant_id="p", industry_pack="demo", actor=None, channel="none", trace_id="t", as_of=datetime.now(timezone.utc), version_manifest_id="v")
    store = OxigraphStore(tmp_path / "graph", writer, "p", lambda context, name: refs.get(name))
    try:
        refs["g"] = store.prepare("g", "\n".join(f'<urn:s{i:03}> <urn:p> "x" .' for i in range(31)))
        real = store.store
        class CountingStore:
            scans = 0
            def __getattr__(self, name):
                return getattr(real, name)
            def quads_for_pattern(self, *args):
                self.scans += 1
                return real.quads_for_pattern(*args)
        counted = CountingStore()
        store.store = counted
        rows, cursor = store.read_named_graph(context, "g", "triples", {"page_size": 3})
        total = len(rows)
        while cursor:
            rows, cursor = store.read_named_graph(context, "g", "triples", {"page_size": 3}, cursor)
            total += len(rows)
        assert total == 31 and counted.scans == 1
        for i in range(5):
            different = context.model_copy(update={"as_of": context.as_of + timedelta(seconds=i + 1)})
            first, next_cursor = store.read_named_graph(different, "g", "triples", {"page_size": i + 1})
            assert len(first) == i + 1 and next_cursor
            assert len(store.read_named_graph(different, "g", "triples", {"page_size": i + 1}, next_cursor)[0]) > 0
        assert counted.scans == 1 and len(store._pages) == 1
        with pytest.raises(PlantkgError):
            store.read_named_graph(context.model_copy(update={"plant_id": "other"}), "g", "triples", {})
        refs.clear()
        with pytest.raises(PlantkgError):
            store.read_named_graph(context, "g", "triples", {})
        refs["g"] = next(iter(store._pages))[0]
        from pyoxigraph import NamedNode
        real.clear_graph(NamedNode("urn:plantkg:" + refs["g"]))
        assert check_references(writer, store, None, "p", [refs["g"]])[0].status == "corrupt"
        with pytest.raises(PlantkgError):
            store.read_named_graph(context, "g", "triples", {"page_size": 3})
        assert counted.scans == 3
    finally:
        store.close()
