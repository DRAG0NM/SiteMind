"""运行管理（B04）：每个 run 固定一份清单、输入切片、种子与时钟起点；状态只按合法转换追加。

run_id 由这里分配（ADR-0004）；发布与回滚只影响之后新建的运行，已有运行的清单与事件不再改写。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from plantkg.core.contracts import Actor, Context, ErrorCode, Interval, Page, PlantkgError, RunInfo
from plantkg.core.versions import SqliteVersionStore, atomic, canonical, page_args, read, utcnow

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"running", "failed"}),
    "running": frozenset({"finished", "failed"}),
    "finished": frozenset(),
    "failed": frozenset(),
}


class SqliteRunStore:
    """RunStore 端口实现。create_run 未指定清单时绑定当前活动清单。"""

    def __init__(self, writer: Any, plant_id: str, versions: SqliteVersionStore, clock: Callable[[], datetime] = utcnow) -> None:
        self.writer, self.plant_id, self.versions, self._clock = writer, plant_id, versions, clock
        self.creation_hook: Callable[[Any, RunInfo], None] | None = None

    def create_run(self, context: Context, kind: str, clock: Interval, forked_from: str | None = None, *,
                   seed: int = 0, inputs: Mapping[str, Any] | None = None,
                   transaction_hook: Callable[[Any, RunInfo], None] | None = None, forked_at: datetime | None = None,
                   energy_end_target_kwh: float | None = None, data_sources: tuple = ()) -> RunInfo:
        """inputs 是数据切片等输入引用（JSON 对象）；与清单、种子、时钟一起固定，供复现旧运行。
        forked_at、energy_end_target_kwh、data_sources 是 v0.8 场景运行字段（B17 分钟级分叉时传入）。"""
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "运行只能建在本厂区")
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "创建运行需要可信写上下文")
        manifest = self.versions.usable(context.version_manifest_id) if context.version_manifest_id else self.versions.active()
        if forked_from is not None:
            self.get_run(forked_from)
        approved_at = (inputs or {}).get("online_approved_as_of")
        if approved_at is not None and forked_from is None:
            try:
                approved_instant = datetime.fromisoformat(approved_at)
                if approved_instant.utcoffset() is None:
                    raise ValueError("timezone required")
            except (TypeError, ValueError):
                raise PlantkgError(ErrorCode.PRECONDITION, "在线输入批准时点必须与运行截止一致且不晚于回放起点") from None
            if approved_instant != context.as_of or context.as_of > clock.start:
                raise PlantkgError(ErrorCode.PRECONDITION, "在线输入批准时点必须与运行截止一致且不晚于回放起点")
        inputs_text = canonical(dict(inputs or {}))
        run = RunInfo(run_id="run-" + secrets.token_hex(8), plant_id=self.plant_id, kind=kind, version_manifest_id=manifest.version_manifest_id,
                      clock=clock, as_of=context.as_of, status="created", forked_from=forked_from, created_at=self._clock(),
                      forked_at=forked_at, energy_end_target_kwh=energy_end_target_kwh, data_sources=tuple(data_sources))

        def write(con):
            con.execute("INSERT INTO runs(plant_id,run_id,status,seed,inputs,record) VALUES (?,?,?,?,?,?)",
                        (self.plant_id, run.run_id, run.status, int(seed), inputs_text, run.model_dump_json()))
            self._event(con, run, "created", None, "created", context.actor, {"seed": int(seed), "trace_id": context.trace_id})
            if self.creation_hook is not None:
                self.creation_hook(con, run)
            if transaction_hook is not None:
                transaction_hook(con, run)
            return run
        return atomic(self.writer, write)

    def get_run(self, run_id: str) -> RunInfo:
        return read(self.writer, lambda con: self._load(con, run_id)[0])

    def run_inputs(self, run_id: str) -> dict[str, Any]:
        """复现一次运行所需的固定输入：清单、时钟、as_of、种子与数据切片。"""
        run, seed, inputs = read(self.writer, lambda con: self._load(con, run_id))
        return {"run_id": run.run_id, "version_manifest_id": run.version_manifest_id, "clock": run.clock.model_dump(mode="json"),
                "as_of": run.as_of.isoformat(), "seed": seed, "inputs": inputs}

    def append_event(self, run_id: str, event_type: str, actor: Actor | None, payload: Mapping[str, Any] | None = None,
                     to_status: str | None = None) -> RunInfo:
        """追加运行事件；带 to_status 时按 RUN_TRANSITIONS 迁移状态。事件不可修改。"""
        if not event_type:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "事件类型不能为空")

        def write(con):
            run = self._load(con, run_id)[0]
            if to_status is None:
                self._event(con, run, event_type, run.status, None, actor, payload or {})
                return run
            if to_status not in RUN_TRANSITIONS.get(run.status, frozenset()):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"运行 {run_id} 不能从 {run.status} 变为 {to_status}")
            updated = RunInfo.model_validate(run.model_dump() | {"status": to_status})
            con.execute("UPDATE runs SET status=?, record=? WHERE plant_id=? AND run_id=?", (to_status, updated.model_dump_json(), self.plant_id, run_id))
            self._event(con, run, event_type, run.status, to_status, actor, payload or {})
            return updated
        return atomic(self.writer, write)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        keys = ("seq", "event_type", "from_status", "to_status", "actor_id", "version_manifest_id", "payload", "occurred_at")
        rows = read(self.writer, lambda con: con.execute(
            "SELECT seq,event_type,from_status,to_status,actor_id,version_manifest_id,payload,occurred_at FROM run_events "
            "WHERE plant_id=? AND run_id=? ORDER BY seq", (self.plant_id, run_id)).fetchall())
        return [dict(zip(keys, r)) for r in rows]

    def list_runs(self, cursor: str | None = None, page_size: int | str | None = None) -> tuple[list[RunInfo], str | None]:
        after, size = page_args(cursor, page_size)
        rows = read(self.writer, lambda con: con.execute(
            "SELECT rowid, record FROM runs WHERE plant_id=? AND rowid>? ORDER BY rowid LIMIT ?", (self.plant_id, after, size + 1)).fetchall())
        return [RunInfo.model_validate_json(r[1]) for r in rows[:size]], str(rows[size - 1][0]) if len(rows) > size else None

    def _load(self, con: Any, run_id: str) -> tuple[RunInfo, int, dict[str, Any]]:
        row = con.execute("SELECT record,seed,inputs FROM runs WHERE plant_id=? AND run_id=?", (self.plant_id, run_id)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {run_id} 不存在")
        return RunInfo.model_validate_json(row[0]), row[1], json.loads(row[2])

    def _event(self, con: Any, run: RunInfo, event_type: str, before: str | None, after: str | None, actor: Actor | None, payload: Mapping[str, Any]) -> None:
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE plant_id=? AND run_id=?", (self.plant_id, run.run_id)).fetchone()[0]
        con.execute("INSERT INTO run_events(plant_id,run_id,seq,event_type,from_status,to_status,actor_id,version_manifest_id,payload,occurred_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self.plant_id, run.run_id, seq, event_type, before, after, actor.actor_id if actor else None, run.version_manifest_id,
                     canonical(dict(payload)), self._clock().isoformat()))


def rest_providers(store: SqliteRunStore) -> dict[str, Callable[..., Any]]:
    def runs(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Page[RunInfo]:
        items, next_cursor = store.list_runs(query.get("cursor"), query.get("page_size"))
        return Page[RunInfo](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)

    def run(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> RunInfo:
        return store.get_run(path["run_id"])
    return {"/runs": runs, "/runs/{run_id}": run}
