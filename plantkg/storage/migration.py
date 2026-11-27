"""Forward-only, checksum-verified SQLite migrations."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from plantkg.storage.sqlite import SQLiteWriter

_NAME = re.compile(r"^(B\d+)_(\d+)_([a-zA-Z0-9_]+)\.sql$")
_CREATE_IF_MISSING = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
log = logging.getLogger("plantkg.storage.migration")


_COLUMN_STOP = ("PRIMARY", "NOT", "NULL", "DEFAULT", "UNIQUE", "CHECK", "REFERENCES", "COLLATE", "GENERATED", "AS", "CONSTRAINT")


def _declared_columns(statement: str, table: str) -> list[tuple[str, str, bool]]:
    """从 CREATE TABLE IF NOT EXISTS 语句解析 (列名, 类型, 是否主键成员)（只做结构比对，不执行 DDL）。"""
    match = re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+" + re.escape(table) + r"\s*\((.*)\)\s*;?\s*$",
                      statement, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    body, depth, current, parts = match.group(1), 0, "", []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    columns: list[list] = []
    table_pk: set[str] = set()
    for part in parts:
        words = part.strip().split(None, 1)
        token = words[0] if words else ""
        if token.upper() == "PRIMARY":
            group = re.search(r"\((.*)\)", part, re.DOTALL)
            table_pk.update(name.strip().strip('"`[]').casefold() for name in (group.group(1) if group else "").split(","))
            continue
        if not token or token.upper() in ("UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "KEY"):
            continue
        rest = words[1] if len(words) > 1 else ""
        type_words = []
        for word in re.split(r"\s+", rest.strip()):
            if not word or word.upper() in _COLUMN_STOP or word.startswith("("):
                break
            type_words.append(word)
        columns.append([token.strip('"`[]').casefold(), " ".join(type_words).casefold(),
                        bool(re.search(r"PRIMARY\s+KEY", rest, re.IGNORECASE))])
    return [(name, kind, pk or name in table_pk) for name, kind, pk in columns]


def _live_columns(con, table: str) -> list[tuple[str, str, bool]]:
    """已存在的表经 PRAGMA table_info 取同口径的 (列名, 类型, 是否主键成员)。"""
    return [(row[1].casefold(), " ".join(str(row[2]).split()).casefold(), bool(row[5]))
            for row in con.execute(f'PRAGMA table_info("{table}")')]


def _statements(sql: str):
    # complete_statement understands quoted semicolons, comments and trigger bodies.
    statement = ""
    for char in sql:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        statement += "\n;"
        if not sqlite3.complete_statement(statement):
            raise ValueError("Incomplete migration SQL")
        yield statement


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]
    backup: Path | None
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()


class MigrationRunner:
    def __init__(self, writer: SQLiteWriter, directory: Path | None = None):
        self.writer = writer
        self.directory = directory or Path(__file__).parent / "migrations"

    def discover(self):
        result = []
        ids = set()
        for path in self.directory.glob("*/*.sql"):
            match = _NAME.fullmatch(path.name)
            if not match:
                raise ValueError(f"Invalid migration name: {path}")
            module = path.parent.name
            identity = (module, match[1], int(match[2]))
            if identity in ids:
                raise ValueError(f"Duplicate migration number: {identity}")
            ids.add(identity)
            # Canonicalize checkout line endings before both hashing and execution.
            sql = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
            raw = sql.encode("utf-8")
            result.append((module, match[1], int(match[2]), path.stem, hashlib.sha256(raw).hexdigest(), sql))
        return sorted(result, key=lambda x: (int(x[1][1:]), x[2], x[0], x[3]))

    def apply(self) -> MigrationResult:
        self.writer.require_outside_transaction()
        migrations = self.discover()
        def upgrade(con):
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            history = con.execute("SELECT module,id,checksum,applied_at FROM schema_migrations").fetchall() if exists else []
            if exists and not history and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_ownership_state'").fetchone():
                raise ValueError("Migration history is unexpectedly empty")
            if history:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_table_owners'").fetchone():
                    raise ValueError("Migration table ownership history is missing")
                try:
                    ownership = con.execute("SELECT table_name,module,schema_checksum FROM schema_table_owners").fetchall()
                    state = con.execute("SELECT checksum FROM schema_ownership_state WHERE id=1").fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("Migration table ownership history is corrupt") from exc
                digest = hashlib.sha256(repr((sorted(ownership), sorted(history))).encode()).hexdigest()
                for table_name, module, schema_checksum in ownership:
                    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
                    actual = hashlib.sha256(row[0].encode()).hexdigest() if row and row[0] else None
                    if actual != schema_checksum:
                        raise ValueError(f"Owned schema missing or changed: {module}/{table_name}")
            known = {(m[0], m[3]): m for m in migrations}
            for module, identity, checksum, applied_at in history:
                if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise ValueError(f"Corrupt migration checksum: {module}/{identity}")
                try:
                    datetime.fromisoformat(applied_at)
                except (TypeError, ValueError):
                    raise ValueError(f"Corrupt migration timestamp: {module}/{identity}") from None
                if (module, identity) not in known:
                    raise ValueError(f"Unknown migration history: {module}/{identity}")
                migration = known[module, identity]
                # Databases made by the original Windows implementation may have
                # recorded the raw CRLF hash. Verify it without rewriting history.
                legacy = hashlib.sha256(migration[5].replace("\n", "\r\n").encode("utf-8")).hexdigest()
                if checksum not in (migration[4], legacy):
                    raise ValueError(f"Migration checksum mismatch: {module}/{identity}")
            if history and (not state or state[0] != digest):
                raise ValueError("Migration or table ownership history is corrupt")
            applied_ids = {(m, i) for m, i, _, _ in history}
            if len(applied_ids) != len(history):
                raise ValueError("Duplicate migration history")
            pending = [m for m in migrations if (m[0], m[3]) not in applied_ids]
            previous = tuple(f"{m[0]}/{m[3]}" for m in migrations if (m[0], m[3]) in applied_ids)
            # A later module may be introduced after a higher B-numbered module.
            # Only the sequence within the same module must have no applied gap.
            modules = {m[0] for m in migrations}
            for module in modules:
                seen_pending = False
                for migration in (m for m in migrations if m[0] == module):
                    applied = (module, migration[3]) in applied_ids
                    if applied and seen_pending:
                        raise ValueError(f"Migration history has a gap in module {module}")
                    seen_pending = seen_pending or not applied
            if not pending:
                return MigrationResult((), None, previous, previous)
            backup = None
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                target = self.writer.path.with_name(self.writer.path.name + "." + stamp + ".backup")
                fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".partial", dir=target.parent)
                os.close(fd)
                partial = Path(name)
                try:
                    dest = sqlite3.connect(partial)
                    try:
                        con.backup(dest)
                        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RuntimeError("Backup integrity check failed")
                    finally:
                        dest.close()
                    if partial.stat().st_size == 0:
                        raise RuntimeError("Backup is empty")
                    # Same-directory link publishes atomically and fails if a
                    # target already exists, unlike replace which overwrites it.
                    os.link(partial, target)
                    backup = target
                finally:
                    partial.unlink(missing_ok=True)
            done = []
            for module, _, _, identity, checksum, sql in pending:
                con.execute("BEGIN IMMEDIATE")
                try:
                    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (module TEXT NOT NULL, id TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL, PRIMARY KEY(module,id))")
                    con.execute("CREATE TABLE IF NOT EXISTS schema_table_owners (table_name TEXT PRIMARY KEY, module TEXT NOT NULL, schema_checksum TEXT NOT NULL)")
                    con.execute("CREATE TABLE IF NOT EXISTS schema_ownership_state (id INTEGER PRIMARY KEY CHECK(id=1), checksum TEXT NOT NULL)")
                    before = {name: ddl for name, ddl in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    owners = dict(con.execute("SELECT table_name,module FROM schema_table_owners"))
                    owner_names = {name.casefold(): owner for name, owner in owners.items()}
                    def authorize(action, arg1, _arg2, _db, _trigger):
                        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
                            return sqlite3.SQLITE_DENY
                        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and arg1:
                            name = arg1.casefold()
                            if name in ("schema_migrations", "schema_table_owners", "schema_ownership_state") or (name in owner_names and owner_names[name] != module):
                                return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK
                    con.set_authorizer(authorize)
                    if_missing: dict[str, str] = {}
                    try:
                        for statement in _statements(sql):
                            match = _CREATE_IF_MISSING.search(statement)
                            con.execute(statement)
                            if match:
                                if_missing[match.group(1)] = statement
                    finally:
                        con.set_authorizer(None)
                    after = {name: ddl for name, ddl in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    changed = {name for name in after if before.get(name) != after[name]}
                    # 升级库：表已由懒建逻辑建过但没登记 owner；结构一致时由本迁移补登记（不再报 unowned）
                    for name, statement in if_missing.items():
                        if name in before and owners.get(name) is None:
                            columns = _live_columns(con, name)
                            if columns and columns == _declared_columns(statement, name):
                                owners[name] = module
                                changed.add(name)
                    for name in changed:
                        owner = owners.get(name)
                        if owner is not None and owner != module:
                            raise ValueError(f"Table {name} belongs to module {owner}, not {module}")
                        if name in before and owner is None:
                            raise ValueError(f"Cannot alter unowned existing table {name}")
                        owners[name] = module
                    for name in changed:
                        for foreign_key in con.execute("SELECT \"table\" FROM pragma_foreign_key_list(?)", (name,)):
                            target = foreign_key[0]
                            if owners.get(target) != module:
                                raise ValueError(f"Cross-module or unowned foreign key: {module}/{name} -> {target}")
                    for name in changed:
                        schema_hash = hashlib.sha256(after[name].encode()).hexdigest()
                        con.execute("INSERT INTO schema_table_owners VALUES (?,?,?) ON CONFLICT(table_name) DO UPDATE SET schema_checksum=excluded.schema_checksum", (name, module, schema_hash))
                    for name in set(before) - set(after):
                        if owners.get(name) != module:
                            raise ValueError(f"Cannot drop table {name} owned by another module")
                        con.execute("DELETE FROM schema_table_owners WHERE table_name=?", (name,))
                    violations = con.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise ValueError(f"Foreign key violations after {module}/{identity}: {violations[:5]}")
                    con.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (module, identity, checksum, datetime.now(timezone.utc).isoformat()))
                    ownership = con.execute("SELECT table_name,module,schema_checksum FROM schema_table_owners ORDER BY table_name,module,schema_checksum").fetchall()
                    ledger = con.execute("SELECT module,id,checksum,applied_at FROM schema_migrations ORDER BY module,id,checksum,applied_at").fetchall()
                    digest = hashlib.sha256(repr((ownership, ledger)).encode()).hexdigest()
                    con.execute("INSERT INTO schema_ownership_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET checksum=excluded.checksum", (digest,))
                    con.commit()
                    done.append(f"{module}/{identity}")
                except BaseException as exc:
                    con.rollback()
                    exc.add_note(f"Migration failed: {module}/{identity}; applied in this run: {done}; backup: {backup}")
                    log.error("Migration failed: %s/%s; before=%s; applied=%s; backup=%s", module, identity, previous, done, backup)
                    raise
            log.info("Migrations complete: before=%s; after=%s; applied=%s; backup=%s", previous, previous + tuple(done), done, backup)
            return MigrationResult(tuple(done), backup, previous, previous + tuple(done))
        return self.writer.submit(upgrade)
