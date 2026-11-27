"""不可变版本清单（B04）：写不可变产物 → check_integrity → SQLite 短事务激活（ADR-0005）。

任一时刻每个厂区恰好一份 active；激活检查预期活动版本，不符返回 STATE_CONFLICT。回滚 = 重新激活
retired 清单，不删产物、不改历史。B18/B27/B30 只能调用这里的原语，不自行改活动版本。

writer 是 B03 的 SQLiteWriter（由装配层传入，core 不导入存储实现）：submit(fn(con)) 在单写线程执行。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from plantkg.core.contracts import (
    MANIFEST_TRANSITIONS,
    Actor,
    ComponentRef,
    Context,
    ErrorCode,
    ManifestComponent,
    Page,
    PlantkgError,
    VersionManifest,
    can_transition,
)

# 完整性检查结果，与 B03 check_references 一致；只有 complete 可以继续
Verifier = Callable[[ComponentRef], str]
USABLE_FOR_RUNS = frozenset({"verified", "active", "retired"})  # 运行可绑定；verified 用于候选回测


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic(writer: Any, fn: Callable[[Any], Any]) -> Any:
    """在单写线程里用一个 BEGIN IMMEDIATE 事务执行 fn；异常（含 COMMIT 失败）回滚。可在事件循环线程调用。

    B04 的写方法自带事务，不能放进外层 writer.transaction()。
    """
    writer.require_outside_transaction()

    def run(con):
        con.execute("BEGIN IMMEDIATE")
        try:
            result = fn(con)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        return result
    return writer.submit(run)


def read(writer: Any, fn: Callable[[Any], Any]) -> Any:
    with writer.read_only() as con:
        return fn(con)


def page_args(cursor: str | None, page_size: int | str | None, default: int = 50) -> tuple[int, int]:
    """cursor 是上一页最后一行的序号；page_size 1～200（ADR-0006）。"""
    try:
        after = int(cursor) if cursor else 0
        size = int(page_size) if page_size not in (None, "") else default
    except ValueError:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "cursor 或 page_size 不合法") from None
    if after < 0 or not 1 <= size <= 200:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "page_size 须在 1～200")
    return after, size


class SqliteVersionStore:
    """VersionStore 端口实现。verifiers 按引用前缀登记（B03 存储引用由装配层登记，其余由产物所有者登记）。"""

    def __init__(self, writer: Any, plant_id: str, clock: Callable[[], datetime] = utcnow) -> None:
        self.writer, self.plant_id, self._clock = writer, plant_id, clock
        self._verifiers: dict[str, Verifier] = {}

    def register_verifier(self, prefix: str, verifier: Verifier) -> None:
        if prefix in self._verifiers:
            raise ValueError(f"引用前缀 {prefix} 的校验函数重复登记")
        self._verifiers[prefix] = verifier

    def has_verifier(self, prefix: str) -> bool:
        """引用前缀是否已登记校验函数（公开查询，调用方不必读内部登记表）。"""
        return prefix in self._verifiers

    # ---- 发布原语
    def prepare(self, components: Mapping[str, Any], reason: str) -> VersionManifest:
        try:
            refs = {ManifestComponent(k).value: ComponentRef.model_validate(v) for k, v in components.items()}
        except ValueError as exc:  # 含 pydantic ValidationError
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"清单组件不合法：{type(exc).__name__}") from None
        if not refs:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "清单至少包含一个组件")
        body = {k: v.model_dump(mode="json") for k, v in refs.items()}
        manifest_hash = "sha256:" + digest(body)

        def write(con):
            # 幂等：同内容且未丢弃的清单直接返回；丢弃过的内容可以重新准备成新清单
            row = con.execute("SELECT record FROM version_manifests WHERE plant_id=? AND manifest_hash=? AND status<>'discarded' ORDER BY rowid LIMIT 1",
                              (self.plant_id, manifest_hash)).fetchone()
            if row:
                return VersionManifest.model_validate_json(row[0])
            manifest = VersionManifest(version_manifest_id="vm-" + secrets.token_hex(8), components=refs, status="prepared",
                                       created_at=self._clock(), reason=reason or None, manifest_hash=manifest_hash)
            self._insert(con, manifest)
            self._event(con, manifest.version_manifest_id, None, "prepared", None, reason, None)
            return manifest
        return atomic(self.writer, write)

    def check_integrity(self, manifest_id: str) -> VersionManifest:
        manifest = self.get(manifest_id)
        if manifest.status != "prepared":
            if manifest.status == "discarded":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"清单 {manifest_id} 已丢弃")
            return manifest  # verified 及之后不再降级
        statuses = self._verify(manifest)
        if any(s in ("missing", "corrupt") for s in statuses.values()):
            target, reason = "discarded", "完整性检查失败：" + canonical(statuses)
        elif all(s == "complete" for s in statuses.values()):
            target, reason = "verified", None
        else:  # not_prepared / unverifiable：不丢弃，产物补齐或登记校验函数后可重试
            raise PlantkgError(ErrorCode.PRECONDITION, "清单组件暂时无法校验", components=statuses)

        def write(con):
            current = self._load(con, manifest_id)
            if current.status != "prepared":  # 并发检查已推进
                return current
            updated = current.model_copy(update={"status": target, "reason": reason or current.reason})
            self._save(con, VersionManifest.model_validate(updated.model_dump()))
            self._event(con, manifest_id, "prepared", target, None, reason, None)
            return updated
        result = atomic(self.writer, write)
        if result.status == "discarded":
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"清单 {manifest_id} 的产物缺失或损坏，已丢弃", components=statuses)
        return result

    def activate(self, expected_active: str | None, candidate: str, actor: Actor, reason: str) -> VersionManifest:
        if not reason:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "激活必须给出原因")
        manifest = self.get(candidate)
        if manifest.status in ("verified", "retired"):
            # 激活前再查一次：校验之后产物仍可能被带外改动
            statuses = self._verify(manifest)
            if not all(s == "complete" for s in statuses.values()):
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"清单 {candidate} 的产物未通过激活前检查", components=statuses)

        def write(con):
            row = con.execute("SELECT manifest_id FROM version_active WHERE plant_id=?", (self.plant_id,)).fetchone()
            current_id = row[0] if row else None
            target = self._load(con, candidate)
            if current_id == candidate and target.previous_active_id == expected_active:
                return target  # 同一请求重试：已生效，原样返回
            if current_id != expected_active:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "活动版本已变化", expected_active=expected_active, actual_active=current_id)
            if not can_transition(MANIFEST_TRANSITIONS, target.status, "active"):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"清单 {candidate} 处于 {target.status}，不能激活")
            now = self._clock()
            if current_id is not None:
                old = self._load(con, current_id)
                self._save(con, old.model_copy(update={"status": "retired"}))
                self._event(con, current_id, "active", "retired", actor.actor_id, reason, None)
            activated = VersionManifest.model_validate(target.model_dump() | {
                "status": "active", "ever_active": True, "previous_active_id": current_id,
                "activated_at": now, "activated_by": actor.actor_id, "reason": reason})
            self._save(con, activated)
            con.execute("INSERT INTO version_active(plant_id,manifest_id) VALUES (?,?) ON CONFLICT(plant_id) DO UPDATE SET manifest_id=excluded.manifest_id",
                        (self.plant_id, candidate))
            self._event(con, candidate, target.status, "active", actor.actor_id, reason, current_id)
            return activated
        return atomic(self.writer, write)

    def discard(self, manifest_id: str, actor: Actor, reason: str) -> VersionManifest:
        """放弃从未激活的清单（prepared/verified → discarded）。"""
        def write(con):
            manifest = self._load(con, manifest_id)
            if manifest.status == "discarded":
                return manifest
            if not can_transition(MANIFEST_TRANSITIONS, manifest.status, "discarded"):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"清单 {manifest_id} 处于 {manifest.status}，不能丢弃")
            updated = manifest.model_copy(update={"status": "discarded", "reason": reason})
            self._save(con, updated)
            self._event(con, manifest_id, manifest.status, "discarded", actor.actor_id, reason, None)
            return updated
        return atomic(self.writer, write)

    # ---- 读取
    def active(self) -> VersionManifest:
        def query(con):
            row = con.execute("SELECT manifest_id FROM version_active WHERE plant_id=?", (self.plant_id,)).fetchone()
            if row is None:
                raise PlantkgError(ErrorCode.PRECONDITION, "尚无活动版本清单")
            return self._load(con, row[0])
        return read(self.writer, query)

    def get(self, manifest_id: str) -> VersionManifest:
        return read(self.writer, lambda con: self._load(con, manifest_id))

    def list(self, cursor: str | None = None, page_size: int | str | None = None) -> tuple[list[VersionManifest], str | None]:
        after, size = page_args(cursor, page_size)
        rows = read(self.writer, lambda con: con.execute(
            "SELECT rowid, record FROM version_manifests WHERE plant_id=? AND rowid>? ORDER BY rowid LIMIT ?", (self.plant_id, after, size + 1)).fetchall())
        items = [VersionManifest.model_validate_json(r[1]) for r in rows[:size]]
        return items, str(rows[size - 1][0]) if len(rows) > size else None

    def history(self, manifest_id: str | None = None) -> list[dict[str, Any]]:
        """状态变化审计：操作人、时间、前后状态与前一活动版本。"""
        sql = "SELECT manifest_id,from_status,to_status,actor_id,reason,previous_active_id,occurred_at FROM version_events WHERE plant_id=?"
        params: tuple = (self.plant_id,)
        if manifest_id:
            sql, params = sql + " AND manifest_id=?", params + (manifest_id,)
        keys = ("manifest_id", "from_status", "to_status", "actor_id", "reason", "previous_active_id", "occurred_at")
        return [dict(zip(keys, r)) for r in read(self.writer, lambda con: con.execute(sql + " ORDER BY seq", params).fetchall())]

    def knowledge_version(self, manifest_id: str) -> int:
        """“知识 vN”（v0.8 RunBinding）：清单按首次激活先后的序号，从 1 起；回滚回到旧清单仍是原来的 N。"""
        order = list(dict.fromkeys(e["manifest_id"] for e in self.history() if e["to_status"] == "active"))
        if manifest_id not in order:
            raise PlantkgError(ErrorCode.PRECONDITION, f"清单 {manifest_id} 从未激活，没有知识版本号")
        return order.index(manifest_id) + 1

    def usable(self, manifest_id: str) -> VersionManifest:
        """运行可绑定的清单：verified（候选）、active 或 retired。"""
        manifest = self.get(manifest_id)
        if manifest.status not in USABLE_FOR_RUNS:
            raise PlantkgError(ErrorCode.PRECONDITION, f"清单 {manifest_id} 处于 {manifest.status}，不能用于运行")
        return manifest

    # ---- 内部
    def _verify(self, manifest: VersionManifest) -> dict[str, str]:
        statuses = {}
        for name, ref in manifest.components.items():
            verifier = next((v for p, v in self._verifiers.items() if ref.ref.startswith(p)), None)
            try:
                statuses[str(name)] = verifier(ref) if verifier else "unverifiable"
            except Exception:  # noqa: BLE001 — 校验器自身故障按无法校验处理，不丢弃清单
                statuses[str(name)] = "unverifiable"
        return statuses

    def _load(self, con: Any, manifest_id: str) -> VersionManifest:
        row = con.execute("SELECT record,manifest_hash FROM version_manifests WHERE plant_id=? AND manifest_id=?", (self.plant_id, manifest_id)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"版本清单 {manifest_id} 不存在")
        manifest = VersionManifest.model_validate_json(row[0])
        body = {str(k): v.model_dump(mode="json") for k, v in manifest.components.items()}
        if (manifest.version_manifest_id != manifest_id or manifest.manifest_hash != row[1]
                or manifest.manifest_hash != "sha256:" + digest(body)):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Manifest content hash mismatch")
        return manifest

    def _insert(self, con: Any, manifest: VersionManifest) -> None:
        con.execute("INSERT INTO version_manifests(plant_id,manifest_id,manifest_hash,status,record) VALUES (?,?,?,?,?)",
                    (self.plant_id, manifest.version_manifest_id, manifest.manifest_hash, manifest.status, manifest.model_dump_json()))

    def _save(self, con: Any, manifest: VersionManifest) -> None:
        con.execute("UPDATE version_manifests SET status=?, record=? WHERE plant_id=? AND manifest_id=?",
                    (manifest.status, manifest.model_dump_json(), self.plant_id, manifest.version_manifest_id))

    def _event(self, con: Any, manifest_id: str, before: str | None, after: str, actor_id: str | None, reason: str | None, previous: str | None) -> None:
        con.execute("INSERT INTO version_events(plant_id,manifest_id,from_status,to_status,actor_id,reason,previous_active_id,occurred_at) VALUES (?,?,?,?,?,?,?,?)",
                    (self.plant_id, manifest_id, before, after, actor_id, reason, previous, self._clock().isoformat()))


class ManifestReferences:
    """B03 ReferenceResolver：按运行固定的清单给出图与时序引用，不回退到最新版本。

    图：清单组件里登记名为 graph_name 的 graph:v1 引用；时序：清单组件里的 series:v1 引用。
    装配时先创建再 bind（存储写者在 B03 装配后才有）。
    """

    def __init__(self) -> None:
        self._store: SqliteVersionStore | None = None
        self._source_maps = None

    def bind(self, store: SqliteVersionStore) -> None:
        self._store = store

    def bind_source_maps(self, source_maps: Any) -> None:
        self._source_maps = source_maps

    def _components(self, context: Context) -> Sequence[ComponentRef]:
        if self._store is None or not context.version_manifest_id or context.plant_id != self._store.plant_id:
            return ()
        try:
            return tuple(self._store.usable(context.version_manifest_id).components.values())
        except PlantkgError:
            return ()

    def graph(self, context: Context, graph_name: str) -> str | None:
        refs = [c.ref for c in self._components(context) if c.ref.startswith("graph:v1:")]
        if not refs:
            return None
        store = self._store
        marks = ",".join("?" for _ in refs)
        row = read(store.writer, lambda con: con.execute(
            f"SELECT reference FROM storage_artifacts WHERE plant_id=? AND kind='graph' AND name=? AND reference IN ({marks})",
            (store.plant_id, graph_name, *refs)).fetchone())
        return row[0] if row else None

    def series(self, context: Context, quantity: str) -> tuple[str, ...]:
        components = self._components(context)
        if not components:
            return ()
        manifest = self._store.usable(context.version_manifest_id)
        source = manifest.components.get("data_sources")
        if source and source.ref.startswith("series-map:"):
            if self._source_maps is None:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "时序映射读取器尚未接入")
            ref = self._source_maps.read_series_map(source).get(quantity)
            return (ref,) if ref else ()
        return tuple(c.ref for c in components if c.ref.startswith("series:v1:"))


def rest_providers(store: SqliteVersionStore) -> dict[str, Callable[..., Any]]:
    """GET /versions 的提供者；签名 provider(context, path, query)。"""
    def versions(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Page[VersionManifest]:
        items, next_cursor = store.list(query.get("cursor"), query.get("page_size"))
        return Page[VersionManifest](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)
    return {"/versions": versions}
