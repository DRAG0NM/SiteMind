from datetime import datetime, timezone

import pytest

from plantkg.core.contracts import Context, ErrorCode, PlantkgError
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteRepository, SQLiteWriter


@pytest.fixture
def writer(tmp_path):
    instance = SQLiteWriter(tmp_path / "catalog.db")
    yield instance
    instance.close()


def test_repository_atomic_and_persistent(writer):
    assert MigrationRunner(writer).apply().applied
    assert not MigrationRunner(writer).apply().applied
    repo = SQLiteRepository(writer, "plant")
    context = Context(plant_id="plant", industry_pack="demo", actor=None, channel="none", trace_id="trace", as_of=datetime.now(timezone.utc))
    with pytest.raises(RuntimeError):
        with repo.transaction_short():
            assert repo.append_once("a", {"x": 1})
            assert repo.read(context, "storage_idempotency", {"key": "a"}) == {"x": 1}
            raise RuntimeError("rollback")
    assert repo.read(context, "storage_idempotency", {"key": "a"}) is None
    assert repo.append_once("a", {"b": 2, "a": 1})
    assert not repo.append_once("a", {"a": 1, "b": 2})
    with pytest.raises(PlantkgError) as exc:
        repo.append_once("a", {"a": 2})
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with writer.read_only() as con, pytest.raises(Exception):
        con.execute("DELETE FROM storage_idempotency")
    with pytest.raises(RuntimeError, match="rolled back"):
        with repo.transaction_short():
            repo.append_once("rolled-back", {"a": 1})
            with pytest.raises(PlantkgError):
                repo.append_once("a", {"different": 1})
    assert repo.read(context, "storage_idempotency", {"key": "rolled-back"}) is None


def test_canonical_records_normalize_models_and_datetimes(writer):
    from datetime import timedelta
    from enum import Enum
    from pydantic import BaseModel
    class Kind(str, Enum):
        configured = "configured"
    class Record(BaseModel):
        when: datetime
        kind: Kind
        value: int | None
    MigrationRunner(writer).apply()
    repo = SQLiteRepository(writer, "p")
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = Record(when=when, kind=Kind.configured, value=None)
    assert repo.append_once("record", {"record": record})
    assert not repo.append_once("record", {"record": {"value": None, "kind": Kind.configured, "when": when.astimezone(timezone(timedelta(hours=8)))}})


def test_migration_failure_retry_and_backup(writer, tmp_path):
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_create.sql").write_text("CREATE TABLE sample (value TEXT); INSERT INTO sample VALUES ('old');", encoding="utf-8")
    second = folder / "B03_002_next.sql"
    second.write_text("INSERT INTO sample VALUES ('new'); INSERT INTO missing VALUES (1);", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    with pytest.raises(Exception):
        runner.apply()
    with writer.read_only() as con:
        assert con.execute("SELECT value FROM sample").fetchall() == [("old",)]
        assert con.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)
    second.write_text("INSERT INTO sample VALUES ('new');", encoding="utf-8")
    result = runner.apply()
    assert result.backup is not None
    import sqlite3
    with sqlite3.connect(result.backup) as con:
        assert con.execute("SELECT value FROM sample").fetchall() == [("old",)]
    (folder / "B03_001_create.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        runner.apply()


def test_later_lower_numbered_module_is_not_global_history_gap(writer, tmp_path):
    root = tmp_path / "migrations"
    storage, assets, runs = (root / name for name in ("storage", "assets", "runs"))
    storage.mkdir(parents=True)
    assets.mkdir()
    (storage / "B03_001_initial.sql").write_text("CREATE TABLE storage_test (id INTEGER);", encoding="utf-8")
    (assets / "B06_001_assets.sql").write_text("CREATE TABLE asset_test (id INTEGER);", encoding="utf-8")
    runner = MigrationRunner(writer, root)
    assert len(runner.apply().applied) == 2
    runs.mkdir()
    (runs / "B04_001_runs.sql").write_text("CREATE TABLE run_test (id INTEGER);", encoding="utf-8")
    assert runner.apply().applied == ("runs/B04_001_runs",)
    assert runner.apply().applied == ()


def test_earlier_migration_added_within_same_module_is_rejected(writer, tmp_path):
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_002_later.sql").write_text("CREATE TABLE later (id INTEGER);", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    (folder / "B03_001_earlier.sql").write_text("CREATE TABLE earlier (id INTEGER);", encoding="utf-8")
    with pytest.raises(ValueError, match="gap in module sample"):
        runner.apply()


def test_migration_checksum_normalizes_crlf_and_accepts_legacy_history(writer, tmp_path):
    import hashlib
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    path = folder / "B03_001_initial.sql"
    lf = b"CREATE TABLE sample (value TEXT);\nINSERT INTO sample VALUES ('ok');\n"
    crlf = lf.replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    with writer.read_only() as con:
        assert con.execute("SELECT checksum FROM schema_migrations").fetchone()[0] == hashlib.sha256(lf).hexdigest()
    path.write_bytes(lf)
    assert runner.apply().applied == ()
    # Reproduce a database created before normalization, preserving its ledger digest.
    def make_legacy(con):
        con.execute("UPDATE schema_migrations SET checksum=?", (hashlib.sha256(crlf).hexdigest(),))
        owners = con.execute("SELECT table_name,module,schema_checksum FROM schema_table_owners ORDER BY table_name,module,schema_checksum").fetchall()
        ledger = con.execute("SELECT module,id,checksum,applied_at FROM schema_migrations ORDER BY module,id,checksum,applied_at").fetchall()
        digest = hashlib.sha256(repr((owners, ledger)).encode()).hexdigest()
        con.execute("UPDATE schema_ownership_state SET checksum=? WHERE id=1", (digest,))
    writer.submit(make_legacy)
    assert runner.apply().applied == ()
    with writer.read_only() as con:
        assert con.execute("SELECT checksum FROM schema_migrations").fetchone()[0] == hashlib.sha256(crlf).hexdigest()


def test_single_owner_concurrency_and_recovery(writer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    MigrationRunner(writer).apply()
    with pytest.raises(RuntimeError, match="already owns"):
        SQLiteWriter(writer.path)
    repo = SQLiteRepository(writer, "plant")
    barrier = Barrier(8)

    def append(index):
        barrier.wait()
        return repo.append_once(str(index), {"index": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(append, range(8)))
    with pytest.raises(PlantkgError):
        repo.append_once("0", {"different": True})
    assert repo.append_once("after-error", {"ok": True})
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM storage_idempotency").fetchone() == (9,)
    with repo.transaction_short(), pytest.raises(RuntimeError, match="Nested"):
        with repo.transaction_short():
            pass


def test_other_process_writer_is_rejected(writer):
    import subprocess
    import sys
    script = "from pathlib import Path; from plantkg.storage.sqlite import SQLiteWriter; SQLiteWriter(Path(__import__('sys').argv[1]))"
    result = subprocess.run([sys.executable, "-c", script, str(writer.path)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "already owns" in result.stderr


def test_unclosed_writer_does_not_hang_interpreter(tmp_path):
    import subprocess
    import sys
    script = "from pathlib import Path; from plantkg.storage.sqlite import SQLiteWriter; w=SQLiteWriter(Path(__import__('sys').argv[1])); print(w._thread.daemon)"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "exit.db")], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "True" in result.stdout


def test_short_transaction_rejects_active_event_loop(writer):
    import asyncio
    MigrationRunner(writer).apply()
    repo = SQLiteRepository(writer, "p")
    async def scenario():
        with pytest.raises(RuntimeError, match="event-loop"):
            with repo.transaction_short():
                pass
        def worker():
            with repo.transaction_short():
                assert repo.append_once("worker", {"v": 1})
        await asyncio.to_thread(worker)
    asyncio.run(scenario())
    assert not repo.append_once("worker", {"v": 1})


def test_migration_module_foreign_key_boundaries(writer, tmp_path):
    root = tmp_path / "modules"
    one, two = root / "one", root / "two"
    one.mkdir(parents=True)
    two.mkdir()
    (one / "B03_001_parent.sql").write_text("CREATE TABLE parent (id INTEGER PRIMARY KEY); CREATE TABLE child (p INTEGER REFERENCES parent(id));", encoding="utf-8")
    runner = MigrationRunner(writer, root)
    runner.apply()
    second = two / "B04_001_bad.sql"
    second.write_text("CREATE TABLE other (p INTEGER REFERENCES parent(id));", encoding="utf-8")
    with pytest.raises(ValueError, match="Cross-module"):
        runner.apply()
    with writer.read_only() as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='other'").fetchone() is None
    second.write_text("CREATE TABLE other (id INTEGER PRIMARY KEY);", encoding="utf-8")
    runner.apply()
    with pytest.raises(Exception):
        writer.submit(lambda con: con.execute("INSERT INTO child VALUES (123)"))


def test_typed_downstream_mapping_scoped_and_transactional(writer, tmp_path):
    import shutil
    from datetime import timedelta
    from pydantic import BaseModel
    from plantkg.core.contracts import Actor
    from plantkg.storage.sqlite import TableMapping

    root = tmp_path / "modules"
    shutil.copytree(__import__("pathlib").Path(__file__).parents[2] / "plantkg" / "storage" / "migrations", root)
    folder = root / "downstream_demo"
    folder.mkdir()
    (folder / "B06_001_assets.sql").write_text("CREATE TABLE asset_record (plant_id TEXT NOT NULL, version_manifest_id TEXT NOT NULL, available_at TEXT NOT NULL, asset_id TEXT NOT NULL, label TEXT NOT NULL CHECK (label != '\"bad\"'), PRIMARY KEY (plant_id, version_manifest_id, asset_id));", encoding="utf-8")
    MigrationRunner(writer, root).apply()
    class AssetRecord(BaseModel):
        plant_id: str
        version_manifest_id: str
        available_at: datetime
        asset_id: str
        label: str
    repo = SQLiteRepository(writer, "p")
    repo.register_table(TableMapping("downstream_demo", "asset_record", AssetRecord, ("asset_id",)))
    now = datetime.now(timezone.utc)
    ctx = Context(plant_id="p", industry_pack="demo", actor=Actor(actor_id="person"), channel="offline", trace_id="t", as_of=now, version_manifest_id="v1")
    record = AssetRecord(plant_id="p", version_manifest_id="v1", available_at=now + timedelta(hours=1), asset_id="a", label="hello")
    with repo.transaction_short():
        repo.insert_record(ctx, "asset_record", record)
        assert repo.read(ctx, "asset_record", {"asset_id": "a"}) is None
        assert repo.read(ctx.model_copy(update={"as_of": now + timedelta(hours=1)}), "asset_record", {"asset_id": "a"})["label"] == "hello"
    assert repo.read(ctx.model_copy(update={"version_manifest_id": "v2"}), "asset_record", {"asset_id": "a"}) is None
    with pytest.raises(PlantkgError) as duplicate:
        repo.insert_record(ctx, "asset_record", record)
    assert duplicate.value.code == ErrorCode.STATE_CONFLICT
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError) as check:
        repo.insert_record(ctx, "asset_record", record.model_copy(update={"asset_id": "b", "label": "bad"}))
    assert check.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_CHECK
    with pytest.raises(PlantkgError):
        repo.insert_record(ctx.model_copy(update={"actor": None, "channel": "none"}), "asset_record", record)


def test_old_database_backup_and_future_history_rejected(writer, tmp_path):
    import sqlite3
    writer.submit(lambda con: con.execute("CREATE TABLE old_data (value TEXT NOT NULL)"))
    writer.submit(lambda con: con.execute("INSERT INTO old_data VALUES ('persisted in WAL')"))
    result = MigrationRunner(writer).apply()
    assert result.backup
    with sqlite3.connect(result.backup) as restored:
        assert restored.execute("SELECT value FROM old_data").fetchone() == ("persisted in WAL",)
        assert restored.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone() is None
    writer.submit(lambda con: con.execute("INSERT INTO schema_migrations VALUES ('future','B99_001_future',?,?)", ("0" * 64, datetime.now(timezone.utc).isoformat())))
    with pytest.raises(ValueError, match="Unknown migration history"):
        MigrationRunner(writer).apply()


def test_upgrade_existing_owned_schema_and_restore_backup(writer, tmp_path):
    import sqlite3
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_old.sql").write_text("CREATE TABLE sample (value TEXT); INSERT INTO sample VALUES ('original');", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    (folder / "B03_002_upgrade.sql").write_text("ALTER TABLE sample ADD COLUMN version INTEGER NOT NULL DEFAULT 1; UPDATE sample SET version=2;", encoding="utf-8")
    result = runner.apply()
    assert result.backup is not None
    assert result.backup.stat().st_size > 0
    assert list(writer.path.parent.glob("*.partial")) == []
    with writer.read_only() as con:
        assert con.execute("SELECT value,version FROM sample").fetchone() == ("original", 2)
    with sqlite3.connect(result.backup) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM sample").fetchone() == ("original",)
        assert restored.execute("SELECT id FROM schema_migrations").fetchall() == [("B03_001_old",)]
    assert runner.apply().applied == ()


def test_failed_backup_publication_leaves_no_partial_or_false_backup(writer, tmp_path, monkeypatch):
    import sqlite3
    import plantkg.storage.migration as migration
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_old.sql").write_text("CREATE TABLE sample (value TEXT); INSERT INTO sample VALUES ('old');", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    (folder / "B03_002_new.sql").write_text("INSERT INTO sample VALUES ('new');", encoding="utf-8")
    existing = writer.path.with_name(writer.path.name + ".existing.backup")
    with sqlite3.connect(existing) as prior:
        prior.execute("CREATE TABLE preserved (value TEXT)")
        prior.execute("INSERT INTO preserved VALUES ('valid')")
    original = existing.read_bytes()
    def fail_publish(partial, target):
        assert partial.stat().st_size > 0
        assert not target.exists()
        raise OSError("injected publication failure")
    monkeypatch.setattr(migration.os, "link", fail_publish)
    with pytest.raises(OSError, match="injected publication failure"):
        runner.apply()
    assert existing.read_bytes() == original
    assert list(writer.path.parent.glob("*.partial")) == []
    assert list(writer.path.parent.glob("*.backup")) == [existing]
    with writer.read_only() as con:
        assert con.execute("SELECT value FROM sample").fetchall() == [("old",)]


def test_backup_publication_never_overwrites_existing_target(writer, tmp_path, monkeypatch):
    import sqlite3
    import plantkg.storage.migration as migration
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_old.sql").write_text("CREATE TABLE sample (value TEXT);", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    (folder / "B03_002_new.sql").write_text("INSERT INTO sample VALUES ('new');", encoding="utf-8")
    real_link = migration.os.link
    def collide(partial, target):
        with sqlite3.connect(target) as prior:
            prior.execute("CREATE TABLE preserved (value TEXT)")
            prior.execute("INSERT INTO preserved VALUES ('valid')")
        return real_link(partial, target)
    monkeypatch.setattr(migration.os, "link", collide)
    with pytest.raises(FileExistsError):
        runner.apply()
    backups = list(writer.path.parent.glob("*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as prior:
        assert prior.execute("SELECT value FROM preserved").fetchone() == ("valid",)
    assert list(writer.path.parent.glob("*.partial")) == []


def test_empty_backup_is_never_published(writer, tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_old.sql").write_text("CREATE TABLE sample (value TEXT);", encoding="utf-8")
    runner = MigrationRunner(writer, folder.parent)
    runner.apply()
    (folder / "B03_002_new.sql").write_text("INSERT INTO sample VALUES ('new');", encoding="utf-8")
    original_stat = Path.stat
    def simulated_stat(path, *args, **kwargs):
        if path.suffix == ".partial":
            return SimpleNamespace(st_size=0)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", simulated_stat)
    with pytest.raises(RuntimeError, match="Backup is empty"):
        runner.apply()
    assert not list(writer.path.parent.glob("*.backup"))
    assert not list(writer.path.parent.glob("*.partial"))


def test_read_connection_remains_available_during_short_write(writer):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    MigrationRunner(writer).apply()
    repo = SQLiteRepository(writer, "p")
    assert repo.append_once("visible", {"value": 1})
    started, release = threading.Event(), threading.Event()
    def writing():
        with repo.transaction_short():
            repo.append_once("pending", {"value": 2})
            started.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writing)
        try:
            assert started.wait(5)
            with writer.read_only() as con:
                assert con.execute("SELECT count(*) FROM storage_idempotency").fetchone() == (1,)
        finally:
            release.set()
        future.result(timeout=5)
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM storage_idempotency").fetchone() == (2,)


def test_read_only_connections_of_finished_threads_are_recycled(writer):
    import sqlite3
    import threading

    captured = []

    def read_once():
        with writer.read_only() as con:
            assert con.execute("SELECT 1").fetchone() == (1,)
        captured.append(con)

    for _ in range(50):
        thread = threading.Thread(target=read_once)
        thread.start()
        thread.join()
    # 旧实现每个短线程留一条；现在新建连接时回收已结束线程的，只剩最后一个线程那条等下次回收
    assert len(writer._read_cons) == 1
    with writer.read_only() as con:
        assert con.execute("SELECT 1").fetchone() == (1,)
    assert len(writer._read_cons) == 1  # 主线程自己的连接
    assert len(captured) == 50
    for con in captured:  # 被回收的连接确实已关闭
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


def test_nested_read_only_rolls_back_only_at_outermost_exit(writer):
    with writer.read_only() as outer:
        outer.execute("BEGIN")
        with writer.read_only() as inner:
            assert inner is outer  # 同线程复用同一连接
            assert inner.execute("SELECT 1").fetchone() == (1,)
        assert outer.in_transaction  # 内层退出不回滚外层开始的事务
        outer.execute("ROLLBACK")
    assert not outer.in_transaction


def test_close_does_not_cut_off_active_reader(writer):
    import sqlite3
    import threading

    entered, release = threading.Event(), threading.Event()
    result = {}

    def reader():
        try:
            with writer.read_only() as con:
                result["con"] = con
                entered.set()
                assert release.wait(10)
                result["value"] = con.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001 — 记给主线程断言，异常外泄会让测试失败
            result["error"] = exc

    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(10)
    writer.close()  # 读线程仍在 read_only 里：只关空闲连接，不掐断这次读
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert "error" not in result
    assert result["value"] == (1,)
    with pytest.raises(sqlite3.ProgrammingError):  # 退出 read_only 后连接已关闭
        result["con"].execute("SELECT 1")
    assert writer._read_cons == {}


def test_read_only_closes_connection_when_pragma_fails(writer, monkeypatch):
    import sqlite3 as sqlite_mod

    from plantkg.storage import sqlite as storage_sqlite

    class BadConnection:
        closed = False

        def execute(self, sql):
            raise sqlite_mod.OperationalError("PRAGMA 失败")

        def close(self):
            self.closed = True

    made = []

    def connect(*args, **kwargs):
        con = BadConnection()
        made.append(con)
        return con

    monkeypatch.setattr(storage_sqlite.sqlite3, "connect", connect)
    with pytest.raises(sqlite_mod.OperationalError):
        with writer.read_only():
            pass
    assert made and made[0].closed  # 新连接初始化失败时要关掉，不能泄漏
    assert writer._read_cons == {}


def test_deleted_migration_history_is_rejected(writer):
    MigrationRunner(writer).apply()
    writer.submit(lambda con: con.execute("DELETE FROM schema_migrations WHERE id='B03_003_formats'"))
    with pytest.raises(ValueError, match="history is corrupt"):
        MigrationRunner(writer).apply()


def test_duplicate_migration_number_rejected(writer, tmp_path):
    folder = tmp_path / "migrations" / "module"
    folder.mkdir(parents=True)
    (folder / "B03_001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (folder / "B03_001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate migration"):
        MigrationRunner(writer, folder.parent).apply()


def test_migration_sql_triggers_and_semicolons_are_atomic(writer, tmp_path):
    folder = tmp_path / "migrations" / "sample"
    folder.mkdir(parents=True)
    (folder / "B03_001_trigger.sql").write_text("""
        CREATE TABLE sample (value TEXT);
        CREATE TABLE audit (value TEXT);
        CREATE TRIGGER record_insert AFTER INSERT ON sample BEGIN
            INSERT INTO audit VALUES ('one;two');
            INSERT INTO audit VALUES (NEW.value);
        END;
        INSERT INTO sample VALUES ('value');
        -- trailing comment
    """, encoding="utf-8")
    MigrationRunner(writer, folder.parent).apply()
    with writer.read_only() as con:
        assert con.execute("SELECT value FROM audit ORDER BY rowid").fetchall() == [("one;two",), ("value",)]


def test_busy_lock_is_bounded_and_writer_recovers(tmp_path):
    import sqlite3
    import time
    writer = SQLiteWriter(tmp_path / "busy.db", timeout=0.1)
    try:
        MigrationRunner(writer).apply()
        other = sqlite3.connect(writer.path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError):
                SQLiteRepository(writer, "p").append_once("locked", {"x": 1})
            assert time.monotonic() - start < 2
            other.rollback()
        finally:
            other.close()
        assert SQLiteRepository(writer, "p").append_once("after", {"x": 1})
    finally:
        writer.close()


def test_blocked_computation_does_not_hold_transaction(writer):
    import threading
    MigrationRunner(writer).apply()
    ready, release = threading.Event(), threading.Event()
    def calculation():
        ready.set()
        assert release.wait(5)
    worker = threading.Thread(target=calculation)
    worker.start()
    try:
        assert ready.wait(5)
        assert SQLiteRepository(writer, "p").append_once("during-calculation", {"x": 1})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_if_not_exists_migration_claims_pre_existing_table(tmp_path):
    """P2-4：升级库里的表若已由懒建逻辑建过，IF NOT EXISTS 迁移要补登记 owner（结构一致时）。"""
    from plantkg.storage.migration import MigrationRunner

    writer = SQLiteWriter(tmp_path / "upgrade.db")
    try:
        writer.submit(lambda con: con.execute(
            "CREATE TABLE b32_sensor_artifacts (plant_id TEXT NOT NULL, digest TEXT NOT NULL, "
            "body TEXT NOT NULL, PRIMARY KEY (plant_id, digest))"))
        MigrationRunner(writer).apply()
        with writer.read_only() as con:
            row = con.execute("SELECT module FROM schema_table_owners WHERE table_name='b32_sensor_artifacts'").fetchone()
        assert row is not None and row[0] == "evolution"
    finally:
        writer.close()
