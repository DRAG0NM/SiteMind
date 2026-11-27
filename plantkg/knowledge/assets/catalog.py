"""资产目录（B06）：资料版本身份、稳定实体、血缘与激活状态。

身份规则（03 §3.3）：
- 资料版本 `asset://{行业}/{完整散列前8位}/{类型}/{定位}`；去重用完整 sha256，内容一变就是新版本。
- 稳定实体 `entity://{厂区}/{类型}/{编号}` 只按 entity_id 显式登记，同名/别名不合并，资料改版不生成新实体。
- 接入时把原文按内容寻址复制一份，之后原文件被改也能取回当初的字节。

存储：表由 `plantkg/storage/migrations/assets/` 建立。与 B04（plantkg/core/versions.py）相同，B03 的 SQLiteWriter 由装配层注入：
写用 writer.submit(fn(con))，读用 writer.read_only()；只读写本模块的 asset_* 表，这里不导入 storage。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from pydantic import TypeAdapter

from plantkg.core.contracts import AssetVersion, Context, Entity, ErrorCode, PlantkgError, UtcDatetime

STAGES = ("dormant", "identified", "in_graph", "referenced")  # 沉睡 → 已标识 → 已入图 → 被规则/计划引用
ACTIVATION_NOTE = "激活率只统计资料被使用的程度，是辅助指标；被引用不等于产生业务收益"
MAX_PAGE_SIZE = 50
_UTC = TypeAdapter(UtcDatetime)


def _ts(value: datetime) -> str:
    """固定宽度 UTC 文本，字典序即时间序。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text.lower()).strip("-.") or "asset"


class SQLiteAssetCatalog:
    """AssetRepository 的 SQLite 实现。一个实例只服务一个厂区。"""

    def __init__(self, writer: Any, plant_id: str, blob_dir: Path, *, base_dir: Path | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.writer, self.plant_id = writer, plant_id
        self.blob_dir = Path(blob_dir)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._clock = clock

    # ---------------------------------------------------------------- 资料版本

    def ingest(self, context: Context, source_uri: str, asset_type: str, available_at: datetime | str, *,
               nature: str | None = None, entity_ids: Sequence[str] | None = None, title: str | None = None,
               license: str | None = None) -> AssetVersion:
        """幂等接入。同内容同类型返回已有版本；同来源内容变化生成新版本并沿用上一版的实体。"""
        return self.ingest_detail(context, source_uri, asset_type, available_at, nature=nature, entity_ids=entity_ids,
                                  title=title, license=license)[0]

    def ingest_detail(self, context, source_uri, asset_type, available_at, *, nature=None, entity_ids=None, title=None,
                      license=None) -> tuple[AssetVersion, bool]:
        """返回（资料版本, 是否命中已有版本）。"""
        self._check_write(context)
        available_at = _UTC.validate_python(available_at)
        if not re.fullmatch(r"[a-z0-9_-]+", asset_type or ""):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "asset_type 只能用小写字母、数字、下划线和连字符")
        path = self._source_path(source_uri)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self._store_blob(digest, content)
        source_key = os.path.normcase(str(path.resolve()))
        requested = tuple(sorted(set(entity_ids))) if entity_ids is not None else None
        now = self._clock()

        def write(con):
            with _savepoint(con):
                for entity_id in requested or ():
                    if not con.execute("SELECT 1 FROM asset_entities WHERE plant_id=? AND entity_id=?",
                                       (self.plant_id, entity_id)).fetchone():
                        raise PlantkgError(ErrorCode.NOT_FOUND, f"实体 {entity_id} 未登记，先登记稳定实体再关联资料")
                row = con.execute("SELECT record FROM asset_versions WHERE plant_id=? AND asset_type=? AND content_hash=?",
                                  (self.plant_id, asset_type, "sha256:" + digest)).fetchone()
                if row:
                    # 同字节可能是两台同型号设备共用的资料：版本不变，只追加新关联，从不删除或改写已有关联
                    existing = AssetVersion.model_validate_json(row[0])
                    self._record_source(con, asset_type, source_key, existing.asset_version_id, now)
                    when = max(existing.available_at, available_at)
                    self._link(con, context, existing, [(entity_id, when) for entity_id in requested or ()])
                    return self._with_links(con, existing), True
                parent = con.execute(
                    "SELECT v.record FROM asset_sources s JOIN asset_versions v "
                    "ON v.plant_id=s.plant_id AND v.asset_version_id=s.asset_version_id "
                    "WHERE s.plant_id=? AND s.asset_type=? AND s.source_key=? ORDER BY s.seq DESC LIMIT 1",
                    (self.plant_id, asset_type, source_key)).fetchone()
                parent = AssetVersion.model_validate_json(parent[0]) if parent else None
                if requested is not None:
                    linked = [(entity_id, available_at) for entity_id in requested]
                elif parent:
                    # 继承父版本的全部关联，各自生效时间取“原关联生效时间”与“新版本可见时间”中较晚的，回放不会提前看到
                    linked = [(entity_id, max(datetime.fromisoformat(at.replace("Z", "+00:00")), available_at))
                              for entity_id, at in con.execute(
                                  "SELECT entity_id, available_at FROM asset_entity_links WHERE plant_id=? AND asset_version_id=?",
                                  (self.plant_id, parent.asset_version_id))]
                else:
                    linked = []
                asset = AssetVersion(
                    asset_version_id=f"asset://{context.industry_pack}/{digest[:8]}/{asset_type}/{_slug(path.stem)}",
                    content_hash="sha256:" + digest, industry=context.industry_pack, asset_type=asset_type,
                    source_uri=source_uri, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    nature=nature or ("measured" if asset_type == "timeseries" else "configured"),
                    available_at=available_at, ingested_at=now,
                    parent_version_id=parent.asset_version_id if parent else None,
                    license=license, title=title or path.stem,
                )
                if con.execute("SELECT 1 FROM asset_versions WHERE plant_id=? AND asset_version_id=?",
                               (self.plant_id, asset.asset_version_id)).fetchone():
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, f"{asset.asset_version_id} 短哈希碰撞，内容不同")
                con.execute("INSERT INTO asset_versions VALUES (?,?,?,?,?,?,?)",
                            (self.plant_id, asset.asset_version_id, asset.content_hash, asset_type,
                             _ts(available_at), _ts(now), asset.model_dump_json()))
                self._record_source(con, asset_type, source_key, asset.asset_version_id, now)
                self._link(con, context, asset, linked)
                return self._with_links(con, asset), False
        return self.writer.submit(write)

    def get(self, context: Context, asset_version_id: str) -> AssetVersion:
        """entity_ids 只含 as_of 时已生效的关联。"""
        self._check_plant(context)
        row = self._query_one("SELECT record FROM asset_versions WHERE plant_id=? AND asset_version_id=?",
                              (self.plant_id, asset_version_id))
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"资料版本 {asset_version_id} 不存在")
        asset = AssetVersion.model_validate_json(row[0])
        if asset.available_at > context.as_of:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, f"资料版本 {asset_version_id} 在 as_of 时尚不可见")
        return self._read(lambda con: self._with_links(con, asset, context.as_of))

    def list(self, context: Context, cursor: str | None = None, page_size: int = MAX_PAGE_SIZE, *,
             asset_type: str | None = None) -> tuple[Sequence[AssetVersion], str | None]:
        """按 asset_version_id 排序，只含 available_at <= as_of 的版本。"""
        self._check_plant(context)
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"page_size 须在 1～{MAX_PAGE_SIZE}")
        after = _decode_cursor(cursor)
        sql = "SELECT record FROM asset_versions WHERE plant_id=? AND available_at<=? AND asset_version_id>?"
        params: list[Any] = [self.plant_id, _ts(context.as_of), after]
        if asset_type:
            sql += " AND asset_type=?"
            params.append(asset_type)
        sql += " ORDER BY asset_version_id LIMIT ?"
        params.append(page_size + 1)

        def query(con):
            rows = con.execute(sql, params).fetchall()
            return rows, [self._with_links(con, AssetVersion.model_validate_json(r[0]), context.as_of) for r in rows[:page_size]]
        rows, items = self._read(query)
        next_cursor = _encode_cursor(items[-1].asset_version_id) if len(rows) > page_size else None
        return items, next_cursor

    def lineage(self, context: Context, asset_version_id: str) -> list[str]:
        """从该版本往上追到最早版本；只含当前可见的祖先。"""
        chain, asset = [], self.get(context, asset_version_id)
        while asset.parent_version_id:
            try:
                asset = self.get(context, asset.parent_version_id)
            except PlantkgError as exc:
                if exc.code == ErrorCode.NOT_VISIBLE:
                    break
                raise
            chain.append(asset.asset_version_id)
        return chain

    def read_content(self, context: Context, asset_version_id: str) -> bytes:
        """取接入时的原文字节，并按完整散列校验。原文件后来被改不影响结果。"""
        asset = self.get(context, asset_version_id)
        digest = asset.content_hash.removeprefix("sha256:")
        try:
            content = self._blob_path(digest).read_bytes()
        except FileNotFoundError:
            raise PlantkgError(ErrorCode.INTERNAL, f"{asset_version_id} 的原文快照缺失") from None
        if hashlib.sha256(content).hexdigest() != digest:
            raise PlantkgError(ErrorCode.INTERNAL, f"{asset_version_id} 的原文快照已损坏")
        return content

    # ---------------------------------------------------------------- 稳定实体

    def register_entity(self, context: Context, entity: Entity) -> Entity:
        """幂等登记；同 ID 不同内容拒绝。只按 entity_id 判断同一，从不按名称合并。"""
        self._check_write(context)
        if entity.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "实体不属于本厂区")
        record = entity.model_dump_json()

        def write(con):
            row = con.execute("SELECT record FROM asset_entities WHERE plant_id=? AND entity_id=?",
                              (self.plant_id, entity.entity_id)).fetchone()
            if row is None:
                con.execute("INSERT INTO asset_entities VALUES (?,?,?,?)", (self.plant_id, entity.entity_id, record, _ts(self._clock())))
            elif Entity.model_validate_json(row[0]) != entity:
                raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, f"实体 {entity.entity_id} 已登记为不同内容")
            return entity
        return self.writer.submit(write)

    def resolve_entity(self, context: Context, entity_id: str) -> Entity:
        self._check_plant(context)
        if entity_id.startswith("entity://") and entity_id.split("/")[2] != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "实体属于其他厂区")
        row = self._query_one("SELECT record FROM asset_entities WHERE plant_id=? AND entity_id=?", (self.plant_id, entity_id))
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"实体 {entity_id} 未登记")
        return Entity.model_validate_json(row[0])

    def find_entities(self, context: Context, name: str) -> list[Entity]:
        """按名称或别名列出全部匹配实体。多于一个即有歧义，由调用方（B22 链接候选/人工）决定，这里不挑。"""
        self._check_plant(context)
        key = name.strip().casefold()
        rows = self._query("SELECT record FROM asset_entities WHERE plant_id=? ORDER BY entity_id", (self.plant_id,))
        entities = [Entity.model_validate_json(r[0]) for r in rows]
        return [e for e in entities if key in {n.strip().casefold() for n in (e.label, *e.aliases)}]

    # ---------------------------------------------------------------- 激活状态

    def record_activation(self, context: Context, asset_version_id: str, stage: str, ref: str | None = None,
                          available_at: datetime | str | None = None) -> str:
        """登记一次使用事件，返回登记后的当前阶段。事件只追加，重复登记幂等；referenced 必须带引用方 ref。"""
        self._check_write(context)
        if stage not in STAGES[1:]:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"stage 须是 {STAGES[1:]} 之一")
        if stage == "referenced" and not ref:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "referenced 必须写明引用它的规则或计划 ID")
        when = _UTC.validate_python(available_at) if available_at is not None else context.as_of
        effective = context.model_copy(update={"as_of": max(context.as_of, when)})  # 事件生效时资料必须已可见
        self.get(effective, asset_version_id)
        self.writer.submit(lambda con: self._insert_activation(con, context, asset_version_id, stage, ref or "", when))
        return self.activation(effective, asset_version_id)

    def activation(self, context: Context, asset_version_id: str) -> str:
        self.get(context, asset_version_id)
        rows = self._query("SELECT DISTINCT stage FROM asset_activations WHERE plant_id=? AND asset_version_id=? AND available_at<=?",
                           (self.plant_id, asset_version_id, _ts(context.as_of)))
        return max((r[0] for r in rows), key=STAGES.index, default="dormant")

    def activation_summary(self, context: Context) -> dict[str, Any]:
        """当前 as_of 可见资料的阶段分布。阶段取已达到的最高一级。"""
        self._check_plant(context)
        as_of = _ts(context.as_of)
        rows = self._query(
            "SELECT v.asset_version_id, a.stage FROM asset_versions v LEFT JOIN asset_activations a "
            "ON a.plant_id=v.plant_id AND a.asset_version_id=v.asset_version_id AND a.available_at<=? "
            "WHERE v.plant_id=? AND v.available_at<=?", (as_of, self.plant_id, as_of))
        best: dict[str, str] = {}
        for asset_id, stage in rows:
            current = best.get(asset_id, "dormant")
            best[asset_id] = max(current, stage or "dormant", key=STAGES.index)
        counts = {stage: sum(1 for s in best.values() if s == stage) for stage in STAGES}
        total = len(best)
        return {"total": total, "by_stage": counts, "referenced_rate": counts["referenced"] / total if total else None,
                "note": ACTIVATION_NOTE}

    # ---------------------------------------------------------------- 内部

    def _record_source(self, con, asset_type: str, source_key: str, asset_version_id: str, now: datetime) -> None:
        """每次接入都记来源 → 版本，下一次改版的父版本取该来源最近一次接入的版本。"""
        con.execute("INSERT INTO asset_sources (plant_id, asset_type, source_key, asset_version_id, ingested_at) VALUES (?,?,?,?,?)",
                    (self.plant_id, asset_type, source_key, asset_version_id, _ts(now)))

    def _link(self, con, context: Context, asset: AssetVersion, links: Sequence[tuple[str, datetime]]) -> None:
        """追加（实体, 生效时间）关联；已有关联保留最早的生效时间。“已标识”从最早一条关联生效起算。"""
        for entity_id, when in links:
            con.execute("INSERT INTO asset_entity_links VALUES (?,?,?,?) ON CONFLICT DO UPDATE "
                        "SET available_at=min(available_at, excluded.available_at)",
                        (self.plant_id, asset.asset_version_id, entity_id, _ts(when)))
            self._insert_activation(con, context, asset.asset_version_id, "identified", "", when)

    def _with_links(self, con, asset: AssetVersion, as_of: datetime | None = None) -> AssetVersion:
        sql, params = "SELECT entity_id FROM asset_entity_links WHERE plant_id=? AND asset_version_id=?", [self.plant_id, asset.asset_version_id]
        if as_of is not None:
            sql += " AND available_at<=?"
            params.append(_ts(as_of))
        ids = tuple(r[0] for r in con.execute(sql + " ORDER BY entity_id", params))
        return asset.model_copy(update={"entity_ids": ids})

    def _insert_activation(self, con, context: Context, asset_version_id: str, stage: str, ref: str, when: datetime) -> None:
        # 同一事件重复登记时保留最早的可见时间（回填更早的时间生效）
        con.execute("INSERT INTO asset_activations VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE "
                    "SET available_at=min(available_at, excluded.available_at)",
                    (self.plant_id, asset_version_id, stage, ref, _ts(when), context.actor.actor_id, _ts(self._clock())))

    def _check_plant(self, context: Context) -> None:
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "资产目录只服务本厂区")

    def _check_write(self, context: Context) -> None:
        self._check_plant(context)
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "写入资产目录需要可信写上下文")

    def _source_path(self, source_uri: str) -> Path:
        parsed = urlparse(source_uri)
        if parsed.scheme == "file":
            # file:///D:/x、file://localhost/D:/x 是绝对路径；file://datasets/x 按契约样例当作 base_dir 下的相对路径
            if parsed.netloc in ("", "localhost"):
                raw = url2pathname(parsed.path)
            else:
                raw = parsed.netloc + unquote(parsed.path)
        elif len(parsed.scheme) > 1:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"暂只支持本地文件，不支持 {parsed.scheme}://")
        else:
            raw = source_uri
        path = Path(raw)
        path = path if path.is_absolute() else self.base_dir / path
        if not path.is_file():
            raise PlantkgError(ErrorCode.NOT_FOUND, f"找不到资料文件 {source_uri}")
        return path

    def _blob_path(self, digest: str) -> Path:
        return self.blob_dir / "sha256" / digest[:2] / digest

    def _store_blob(self, digest: str, content: bytes) -> None:
        target = self._blob_path(digest)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{digest}.{secrets.token_hex(8)}.partial")
        try:
            temp.write_bytes(content)
            os.replace(temp, target)
        except OSError:
            temp.unlink(missing_ok=True)
            if not target.exists():
                raise

    def _read(self, fn: Callable[[Any], Any]) -> Any:
        """读走 B03 的只读连接（与 B04 相同），写走 writer.submit；本模块只碰自己迁移建的 asset_* 表。"""
        with self.writer.read_only() as con:
            return fn(con)

    def _query(self, sql: str, params: Sequence[Any]) -> list[tuple]:
        return self._read(lambda con: con.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: Sequence[Any]) -> tuple | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None


class _savepoint:
    """检查与写入放在同一保存点里；在 B03 短事务内外都可用。"""

    def __init__(self, con) -> None:
        self.con = con

    def __enter__(self):
        self.con.execute("SAVEPOINT b06_catalog")

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.con.execute("ROLLBACK TO b06_catalog")
        self.con.execute("RELEASE b06_catalog")
        return False


def _encode_cursor(last_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"after": last_id}).encode()).decode()


def _decode_cursor(cursor: str | None) -> str:
    if not cursor:
        return ""
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))["after"]
    except (ValueError, KeyError, TypeError):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "cursor 无效") from None
