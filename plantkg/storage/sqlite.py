"""A single thread owns the writable SQLite connection for a service instance."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from plantkg.core.contracts import Context, ErrorCode, PlantkgError

_owners_lock = threading.Lock()
_owners: set[Path] = set()


def _acquire_file_lock(path: Path):
    handle = open(str(path) + ".writer.lock", "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        raise RuntimeError(f"SQLite writer already owns {path}") from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.utcoffset() is not None else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported record value: {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SQLiteWriter:
    def __init__(self, path: Path, timeout: float = 2.0):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _owners_lock:
            if self.path in _owners:
                raise RuntimeError(f"SQLite writer already owns {self.path}")
            _owners.add(self.path)
        try:
            self._file_lock = _acquire_file_lock(self.path)
        except BaseException:
            with _owners_lock:
                _owners.discard(self.path)
            raise
        self.timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._local = threading.local()
        self._read_cons: dict[sqlite3.Connection, threading.Thread] = {}
        self._read_busy: dict[sqlite3.Connection, int] = {}  # 正在 read_only 里的连接：close() 不关，等持有线程退出
        self._closed = False
        self._released = False
        ready: Future = Future()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="plantkg-sqlite-writer", daemon=True)
        self._thread.start()
        try:
            ready.result()
        except BaseException:
            self._thread.join()
            with _owners_lock:
                _owners.discard(self.path)
            self._file_lock.close()
            raise

    def _run(self, ready: Future) -> None:
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
            con.execute("PRAGMA foreign_keys=ON")
            if con.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise RuntimeError("SQLite WAL unavailable")
            ready.set_result(None)
        except BaseException as exc:
            if con is not None:
                con.close()
            ready.set_exception(exc)
            return
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                fn, future = job
                try:
                    future.set_result(fn(con))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            con.close()

    def submit(self, fn):
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("SQLite writer is closed")
            target = getattr(self._local, "transaction_queue", self._queue)
            target.put((fn, future))
        return future.result()

    @contextmanager
    def transaction(self):
        """Run a short synchronous transaction; its body must not await."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("SQLite short transactions cannot run on an active event-loop thread; use a worker thread")
        if getattr(self._local, "transaction_queue", None) is not None:
            raise RuntimeError("Nested transactions are not supported")
        commands: queue.Queue = queue.Queue()
        started, finished = Future(), Future()

        def run(con):
            try:
                con.execute("BEGIN IMMEDIATE")
                started.set_result(None)
                failure = None
                while True:
                    fn, result = commands.get()
                    if fn is None:
                        if result and failure is not None:
                            con.rollback()
                            raise RuntimeError("Transaction rolled back after a failed operation") from failure
                        if result:
                            con.commit()
                        else:
                            con.rollback()
                        return
                    try:
                        result.set_result(fn(con))
                    except BaseException as exc:
                        failure = exc
                        result.set_exception(exc)
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)
                con.rollback()
                raise

        with self._lock:
            if self._closed:
                raise RuntimeError("SQLite writer is closed")
            self._queue.put((run, finished))
        started.result()
        self._local.transaction_queue = commands
        try:
            yield
        except BaseException:
            commands.put((None, False))
            finished.result()
            raise
        else:
            commands.put((None, True))
            finished.result()
        finally:
            del self._local.transaction_queue

    @contextmanager
    def read_only(self):
        """在同一线程内复用只读连接：每次 connect/close 的开销在逐分钟推进时会占掉明显份额（#perf）。

        连接记下所属线程，新建连接时顺手关掉已结束线程的；长跑的 live 进程里会诊回复、网关、预演这些
        一次性的短线程不再各自漏一条连接直到 writer 关闭。close() 只关空闲连接，正在读的连接由持有线程
        退出 read_only 时关闭，不会把读线程的查询掐断（#207 P2）。writer 关闭后再调用本方法抛 RuntimeError。
        """
        con = getattr(self._local, "read_con", None)
        with self._lock:
            if con is not None and con not in self._read_cons:
                con = None
            if con is not None:
                self._read_busy[con] = self._read_busy.get(con, 0) + 1
        if con is None:
            # check_same_thread=False：连接只在本线程复用，close() 可能在别的线程统一关闭（测试 teardown）
            con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout, check_same_thread=False)
            try:  # PRAGMA 失败时把刚建的连接关掉，不留在 _read_cons 之外
                con.execute("PRAGMA query_only=ON")
                con.execute("PRAGMA foreign_keys=ON")
            except BaseException:
                con.close()
                raise
            with self._lock:
                for stale, owner in list(self._read_cons.items()):
                    if not owner.is_alive():
                        del self._read_cons[stale]
                        self._read_busy.pop(stale, None)
                        stale.close()
                if self._closed:
                    con.close()
                    raise RuntimeError("SQLite writer is closed")
                self._read_cons[con] = threading.current_thread()
                self._read_busy[con] = 1
            self._local.read_con = con
        depth = getattr(self._local, "read_depth", 0)
        self._local.read_depth = depth + 1
        try:
            yield con
        finally:
            self._local.read_depth = depth
            # 复用连接：调用方可能 BEGIN 了读事务（如 jobs.snapshot），退出时回滚，保持与“每次新连接”相同的隔离语义。
            # 同一线程可嵌套 read_only，只有最外层退出才回滚，内层不能把外层开始的事务一起结束。
            if depth == 0:
                try:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                except sqlite3.ProgrammingError:
                    pass  # 连接已被 close() 关闭：只读事务随连接结束，不盖掉调用方的异常
            close_now = False
            with self._lock:
                busy = self._read_busy.get(con, 1) - 1
                if busy > 0:
                    self._read_busy[con] = busy
                else:
                    self._read_busy.pop(con, None)
                    if self._closed and self._read_cons.pop(con, None) is not None:
                        close_now = True
            if close_now:
                con.close()
                if getattr(self._local, "read_con", None) is con:
                    self._local.read_con = None

    def close(self):
        if getattr(self._local, "transaction_queue", None) is not None:
            raise RuntimeError("Cannot close SQLite writer inside a transaction")
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise TimeoutError("SQLite writer did not finish accepted work within 10 seconds")
        with self._lock:
            # 只关空闲的只读连接；正在 read_only 的留给持有线程退出时关，避免把它的查询掐断（#207 P2）
            read_cons = [con for con in self._read_cons if self._read_busy.get(con, 0) == 0]
            for con in read_cons:
                del self._read_cons[con]
                self._read_busy.pop(con, None)
        for con in read_cons:
            con.close()
        with self._lock:
            if not self._released:
                self._file_lock.close()
                with _owners_lock:
                    _owners.discard(self.path)
                self._released = True

    def require_outside_transaction(self):
        if getattr(self._local, "transaction_queue", None) is not None:
            raise RuntimeError("Artifact preparation or migration inside a SQLite transaction is unsupported")


class SQLiteRepository:
    """The only built-in mapping is the scoped B03 idempotency record."""
    def __init__(self, writer: SQLiteWriter, plant_id: str):
        self.writer, self.plant_id = writer, plant_id
        self._tables: dict[str, TableMapping] = {}

    def register_table(self, mapping: TableMapping) -> None:
        """Register trusted, typed module code; no SQL or connection is exposed to callers."""
        names = (mapping.module, mapping.table, *mapping.model.model_fields, *mapping.key_fields)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
            raise ValueError("Invalid mapping identifier")
        fields = set(mapping.model.model_fields)
        if not {"plant_id", "version_manifest_id", "available_at"} <= fields or not mapping.key_fields or not set(mapping.key_fields) <= fields - {"plant_id", "version_manifest_id", "available_at"}:
            raise ValueError("Mapping requires scope fields and declared keys")
        with self.writer.read_only() as con:
            owner = con.execute("SELECT module FROM schema_table_owners WHERE table_name=?", (mapping.table,)).fetchone()
            columns = {r[1] for r in con.execute(f'PRAGMA table_info("{mapping.table}")')}
        if owner != (mapping.module,) or columns != fields or mapping.table in self._tables:
            raise ValueError("Mapping does not match an owned migrated table, or is already registered")
        self._tables[mapping.table] = mapping

    def insert_record(self, context: Context, table: str, record: BaseModel) -> None:
        mapping = self._tables.get(table)
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "A trusted write context is required")
        if mapping is None or not isinstance(record, mapping.model):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Unknown mapping or record type")
        if not isinstance(record.available_at, datetime) or record.available_at.utcoffset() != timedelta(0):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "available_at must be a UTC datetime")
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "Fixed manifest required")
        data = record.model_dump(mode="json")
        if context.plant_id != self.plant_id or data["plant_id"] != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "Repository plant mismatch")
        if data["version_manifest_id"] != context.version_manifest_id:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "Record manifest mismatch")
        columns = tuple(mapping.model.model_fields)
        values = tuple(data[name] if name in ("plant_id", "version_manifest_id") else canonical(data[name]) for name in columns)
        quoted = ",".join(f'"{name}"' for name in columns)
        placeholders = ",".join("?" for _ in columns)
        def insert(con):
            try:
                con.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', values)
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_ROWID):
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "Record key already exists") from exc
                raise
        self.writer.submit(insert)

    def transaction_short(self):
        return self.writer.transaction()

    def append_once(self, key: str, record: Mapping[str, Any]) -> bool:
        content = canonical(record)
        def write(con):
            row = con.execute("SELECT content FROM storage_idempotency WHERE plant_id=? AND key=?", (self.plant_id, key)).fetchone()
            if row:
                if row[0] != content:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "Idempotency key reused with different content")
                return False
            con.execute("INSERT INTO storage_idempotency(plant_id,key,content) VALUES (?,?,?)", (self.plant_id, key, content))
            return True
        return self.writer.submit(write)

    def read(self, context: Context, table: str, key: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "Repository plant mismatch")
        if table in self._tables:
            return self._read_mapped(context, self._tables[table], key)
        if table != "storage_idempotency" or set(key) != {"key"}:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Unknown storage mapping or key")
        def query(con):
            row = con.execute("SELECT content FROM storage_idempotency WHERE plant_id=? AND key=?", (self.plant_id, key["key"])).fetchone()
            return json.loads(row[0]) if row else None
        if getattr(self.writer._local, "transaction_queue", None) is not None:
            return self.writer.submit(query)
        with self.writer.read_only() as con:
            return query(con)

    def _read_mapped(self, context, mapping, key):
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "Fixed manifest required")
        if set(key) != set(mapping.key_fields):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "Mapping key does not match")
        columns = tuple(mapping.model.model_fields)
        select = ",".join(f'"{name}"' for name in columns)
        where = '"plant_id"=? AND "version_manifest_id"=?' + "".join(f' AND "{name}"=?' for name in mapping.key_fields)
        params = (self.plant_id, context.version_manifest_id, *(canonical(key[name]) for name in mapping.key_fields))
        def query(con):
            rows = con.execute(f'SELECT {select} FROM "{mapping.table}" WHERE {where}', params).fetchmany(2)
            if len(rows) > 1:
                raise PlantkgError(ErrorCode.INTERNAL, "Mapping keys are not unique")
            if not rows:
                return None
            data = {name: value if name in ("plant_id", "version_manifest_id") else json.loads(value) for name, value in zip(columns, rows[0])}
            record = mapping.model.model_validate(data)
            if not isinstance(record.available_at, datetime) or record.available_at.utcoffset() != timedelta(0):
                raise PlantkgError(ErrorCode.INTERNAL, "Stored available_at is invalid")
            if record.available_at > context.as_of:
                return None
            return record.model_dump(mode="json")
        if getattr(self.writer._local, "transaction_queue", None) is not None:
            return self.writer.submit(query)
        with self.writer.read_only() as con:
            return query(con)


@dataclass(frozen=True)
class TableMapping:
    module: str
    table: str
    model: type[BaseModel]
    key_fields: tuple[str, ...]
