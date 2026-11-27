"""待确认动作（B04）：Agent 侧特权工具只生成 PendingAction，复核员经统一确认端点确认后才调用登记的执行函数（09 D18）。

确认/拒绝带 expected_revision 与 idempotency_key：修订不符 REVISION_CONFLICT，同键不同内容 IDEMPOTENCY_MISMATCH，
已过期 ACTION_EXPIRED，同键同内容返回原结果。执行函数由 B15/B18/B24 登记到 Registry.action_handlers，不改本文件。

工作台登录（B35/B23）接入前，确认端点只接受本机运维令牌：服务启动时生成随机令牌写到本机文件，
`python -m plantkg actions ...` 读取后带令牌与复核员 ID 调端点；复核员 ID 须在配置 actions.reviewers 中。
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from plantkg.core.contracts import (
    API_PREFIX,
    PENDING_ACTION_TRANSITIONS,
    ActionDecisionRequest,
    Actor,
    Context,
    ErrorCode,
    EvidenceEvent,
    Page,
    PendingAction,
    PendingActionKind,
    PlantkgError,
    ProposalOrigin,
    Role,
    ToolResult,
    can_transition,
)
from plantkg.core.versions import atomic, digest, page_args, read, utcnow

log = logging.getLogger("plantkg.actions")
TOKEN_HEADER = "x-plantkg-operator-token"
ACTOR_HEADER = "x-plantkg-actor"
# 工作台登录（B35/B23）接入前，需要运维令牌的入口共用这一句，避免文案漂移（#203）
WORKBENCH_LOGIN_PENDING = "工作台登录尚未接入（B35/B23）"
# #234：拒绝也允许声明“不是系统判断有误”，但只收 system_error 一个键，且只对这些复核/执行类动作开放
REJECT_EDITS_KINDS = frozenset({"review", "review_decision", "correction", "sampling", "work_order"})
REJECT_EDITS_KEYS = frozenset({"system_error"})


class SqlitePendingActionStore:
    """PendingActionStore 端口实现。handlers 是 Registry.action_handlers（kind → ActionHandler），按需查找。"""

    def __init__(self, writer: Any, plant_id: str, handlers: Mapping[str, Any], evidence: Any | None = None,
                 ttl_s: int = 24 * 3600, clock: Callable[[], datetime] = utcnow,
                 action_clock: Callable[[str, str | None], datetime] | None = None) -> None:
        self.writer, self.plant_id, self._handlers, self._evidence = writer, plant_id, handlers, evidence
        self._ttl, self._clock = timedelta(seconds=ttl_s), clock
        self._action_clock = action_clock
        self._decide = threading.RLock()  # 确认、执行与过期串行：同一动作不会执行两次，执行中也不会被标为过期
        self._recover_intents()  # 启动扫描：上次进程停在“确认中”的动作不能重放或拒绝，标 needs_repair 等人工处理

    def set_action_clock(self, action_clock: Callable[[str, str | None], datetime]) -> None:
        """Install the per-action clock before the service is exposed to callers."""
        self._action_clock = action_clock

    def propose(self, kind: PendingActionKind | str, payload: Mapping[str, Any], context: Context,
                *, action_id: str | None = None) -> PendingAction:
        """提议一个待确认动作；action_id 缺省自动生成，演示种子等需要固定编号时由调用方传入。"""
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "提议需要经工作台网关发起的运行")
        if context.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "不能为其他厂区提议")
        kind = PendingActionKind(kind).value
        try:
            payload = json.loads(json.dumps(dict(payload), allow_nan=False))  # 只收 JSON；深拷贝，提议后调用方再改不影响
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "提议内容必须是 JSON 对象") from None
        request_hash = digest({"kind": kind, "payload": payload})
        key = context.idempotency_key or "auto-" + digest({"hash": request_hash, "agent_run_id": context.agent_run_id, "trace_id": context.trace_id})[:32]
        now = self._now(kind, context.run_id)
        action = PendingAction(action_id=action_id or "act-" + secrets.token_hex(8), kind=kind, plant_id=self.plant_id, payload=payload,
                               proposed_by=ProposalOrigin(agent_run_id=context.agent_run_id, run_id=context.run_id, trace_id=context.trace_id),
                               proposed_at=now, expires_at=now + self._ttl, target_ref=payload.get("target_ref"),
                               target_revision=payload.get("target_revision"), idempotency_key=key)

        def write(con):
            row = con.execute("SELECT request_hash, record FROM pending_actions WHERE plant_id=? AND idempotency_key=?", (self.plant_id, key)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一幂等键对应了不同的提议")
                return PendingAction.model_validate_json(row[1]), False
            con.execute("INSERT INTO pending_actions(plant_id,action_id,idempotency_key,request_hash,status,record) VALUES (?,?,?,?,?,?)",
                        (self.plant_id, action.action_id, key, request_hash, action.status, action.model_dump_json()))
            return action, True
        stored, created = atomic(self.writer, write)
        if created:
            self._record(stored, "pending_action_proposed", context.trace_id, f"提议 {kind}")
        return stored

    def confirm(self, action_id: str, actor: Actor, reason: str, expected_revision: int, idempotency_key: str,
                edits: Mapping[str, Any] | None = None, trace_id: str | None = None) -> PendingAction:
        return self._decide_action(action_id, "confirmed", actor, reason, expected_revision, idempotency_key, edits, trace_id)

    def reject(self, action_id: str, actor: Actor, reason: str, expected_revision: int, idempotency_key: str,
               edits: Mapping[str, Any] | None = None, trace_id: str | None = None) -> PendingAction:
        return self._decide_action(action_id, "rejected", actor, reason, expected_revision, idempotency_key, edits, trace_id)

    def cancel(self, action_id: str, reason: str) -> PendingAction:
        """提议方发现对应事项已不可能执行（工单完成/过期/驳回、队列项已处理）时关闭待确认项。

        只允许从 proposed 转 expired；已确认或已进入执行返回 STATE_CONFLICT。expired 记录不带确认人，
        取消原因写进 payload.cancel_reason，保留可审计的原始提议。
        """
        with self._decide:
            action = self.get(action_id)
            if action.status == "expired":
                return action
            if not can_transition(PENDING_ACTION_TRANSITIONS, action.status, "expired"):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已是 {action.status}，不能取消")
            cancelled = PendingAction.model_validate(action.model_dump() | {
                "status": "expired", "revision": action.revision + 1,
                "payload": {**action.payload, "cancel_reason": reason}})

            def write(con):
                current = self._load(con, action_id)[0]
                if current.revision != action.revision:
                    raise PlantkgError(ErrorCode.REVISION_CONFLICT, "动作修订已变化")
                con.execute("UPDATE pending_actions SET status='expired', record=? WHERE plant_id=? AND action_id=?",
                            (cancelled.model_dump_json(), self.plant_id, action_id))
            atomic(self.writer, write)
        self._record(cancelled, "action_decided", cancelled.proposed_by.trace_id, f"事项已不可执行，动作置 expired：{reason}")
        return cancelled

    def get(self, action_id: str) -> PendingAction:
        self._expire_due(action_id)
        return read(self.writer, lambda con: self._load(con, action_id)[0])

    def result(self, action_id: str) -> ToolResult | None:
        """确认后执行函数的返回结果。"""
        text = read(self.writer, lambda con: self._load(con, action_id)[1])
        return ToolResult.model_validate_json(text) if text else None

    def list(self, status: str | None = None, kind: str | None = None, cursor: str | None = None,
             page_size: int | str | None = None) -> tuple[list[PendingAction], str | None]:
        self._expire_due()
        after, size = page_args(cursor, page_size)
        # needs_repair 记在执行意图表上，动作记录仍是 proposed：按意图状态查，供人工处置（#204）
        sql = "SELECT p.rowid, p.record FROM pending_actions p"
        params: list[Any] = []
        if status == "needs_repair":
            sql += " JOIN action_intents i ON i.plant_id=p.plant_id AND i.action_id=p.action_id"
        sql = sql + " WHERE p.plant_id=?"
        params.append(self.plant_id)
        if status == "needs_repair":
            sql += " AND i.status='needs_repair'"
        elif status:
            sql += " AND p.status=?"
            params.append(status)
        sql += " AND p.rowid>?"
        params.append(after)
        if kind:
            sql += " AND json_extract(p.record,'$.kind')=?"
            params.append(kind)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY p.rowid LIMIT ?", (*params, size + 1)).fetchall())
        return [PendingAction.model_validate_json(r[1]) for r in rows[:size]], str(rows[size - 1][0]) if len(rows) > size else None

    # ---- 内部
    def _decide_action(self, action_id: str, target: str, actor: Actor, reason: str, expected_revision: int, key: str,
                       edits: Mapping[str, Any] | None, trace_id: str | None) -> PendingAction:
        if Role.reviewer.value not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "确认与拒绝需要复核员角色")
        request = ActionDecisionRequest(reason=reason, expected_revision=expected_revision, idempotency_key=key,
                                        edits=dict(edits) if edits is not None else None)
        request_hash = digest({"action_id": action_id, "decision": target, "actor": actor.actor_id, "request": request.model_dump(mode="json")})
        # 空 edits（{}）等同未带修订：通用前端对拒绝一律带空对象时，不改变原有语义
        reject_edits = (request.edits or None) if target == "rejected" else None
        with self._decide:
            prior = read(self.writer, lambda con: con.execute(
                "SELECT action_id, request_hash FROM action_decisions WHERE plant_id=? AND idempotency_key=?", (self.plant_id, key)).fetchone())
            if prior:
                if prior[1] != request_hash:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一幂等键对应了不同的决定")
                return self.get(prior[0])
            action = self.get(action_id)
            if action.status == "expired":
                raise PlantkgError(ErrorCode.ACTION_EXPIRED, f"动作 {action_id} 已过期")
            if not can_transition(PENDING_ACTION_TRANSITIONS, action.status, target):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已是 {action.status}")
            if action.revision != expected_revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "动作修订已变化", expected_revision=expected_revision, actual_revision=action.revision)
            if reject_edits is not None:
                self._check_reject_edits(action.kind, reject_edits)
            now = self._now(action.kind, action.proposed_by.run_id)
            if action.expires_at is not None and action.expires_at <= now:  # 过期扫描之后刚好到期
                raise PlantkgError(ErrorCode.ACTION_EXPIRED, f"动作 {action_id} 已过期")
            # 拒绝的修订只进 action_decisions 审计：PendingAction 契约仍只允许 review_decision 带 edits
            decided = PendingAction.model_validate(action.model_dump() | {
                "status": target, "revision": action.revision + 1, "decided_by": actor.actor_id, "decided_at": now,
                "reason": reason, "edits": request.edits if target == "confirmed" else None})
            result = None
            if target == "confirmed":
                handler = self._handlers.get(action.kind)
                if handler is None:
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"{action.kind} 的执行函数尚未登记")
                # 阶段 1：先落“确认中”执行意图；进程在阶段 2/3 之间崩溃时，启动扫描据此禁止重放与拒绝（#123）
                self._begin_intent(action_id, target, key, request_hash, now, decided)
                try:
                    result = handler.apply(decided, actor)
                except BaseException:
                    # 执行函数报错按“未生效”处理（登记约定：先校验、后提交副作用），复核员可修正后重试
                    self._drop_intent(action_id)
                    raise
                if not result.ok:
                    self._drop_intent(action_id)  # 执行函数明确未生效：允许复核员改判拒绝或重试
                    error = result.error
                    raise PlantkgError(ErrorCode(error.code) if error else ErrorCode.INTERNAL, error.message if error else "执行函数失败")
            else:
                self._refuse_if_executing(action_id)
                reject_hook = getattr(self._handlers.get(action.kind), "reject", None)
                if callable(reject_hook):  # 复核项的“不通过”要回滚并降级，由执行函数自己实现（加法接口，非必需）
                    rejected = reject_hook(decided, actor, reject_edits)
                    if rejected is not None and not rejected.ok:
                        error = rejected.error
                        raise PlantkgError(ErrorCode(error.code) if error else ErrorCode.INTERNAL, error.message if error else "拒绝处理失败")
            # 阶段 3：写终态决定；失败时“确认中”意图留在表里，动作仍是 proposed，拒绝会被 STATE_CONFLICT 挡住
            try:
                atomic(self.writer, lambda con: self._commit_decision(
                    con, action_id, action.revision, target, decided, result, key, request_hash, now,
                    reject_edits if target == "rejected" else request.edits))
            except PlantkgError:
                raise
            except Exception as exc:  # noqa: BLE001 — 落库失败不能冒充已决，也不能让动作回到可拒绝状态
                log.exception("动作 %s 的决定落库失败", action_id)
                self._mark_needs_repair(action_id)
                raise PlantkgError(ErrorCode.INTERNAL, "确认结果落库失败；动作已进入执行，重启后标 needs_repair") from exc
        self._record(decided, "action_decided", trace_id or decided.proposed_by.trace_id, f"{actor.actor_id} {target}：{reason}")
        return decided

    def _begin_intent(self, action_id: str, decision: str, key: str, request_hash: str, at: datetime,
                      decided: PendingAction) -> None:
        def write(con):
            row = con.execute("SELECT status FROM action_intents WHERE plant_id=? AND action_id=?",
                              (self.plant_id, action_id)).fetchone()
            if row is not None:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已进入执行（{row[0]}），不能再确认")
            con.execute("INSERT INTO action_intents(plant_id,action_id,idempotency_key,request_hash,status,started_at,record) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (self.plant_id, action_id, key, request_hash, "confirming", at.isoformat(), decided.model_dump_json()))
        atomic(self.writer, write)

    def _drop_intent(self, action_id: str) -> None:
        atomic(self.writer, lambda con: con.execute(
            "DELETE FROM action_intents WHERE plant_id=? AND action_id=?", (self.plant_id, action_id)))

    def _mark_needs_repair(self, action_id: str) -> None:
        """终态决定写不进库时立刻把执行意图标 needs_repair；写者已关闭则留给下次启动扫描。"""
        try:
            atomic(self.writer, lambda con: con.execute(
                "UPDATE action_intents SET status='needs_repair' WHERE plant_id=? AND action_id=? AND status='confirming'",
                (self.plant_id, action_id)))
        except Exception:  # noqa: BLE001
            log.exception("动作 %s 的执行意图无法标 needs_repair，留给启动扫描", action_id)

    @staticmethod
    def _check_reject_edits(kind: str, edits: Mapping[str, Any]) -> None:
        """#234：拒绝只接受 system_error 一个修订键，且只对复核/执行类动作开放。"""
        extra = sorted(set(edits) - REJECT_EDITS_KEYS)
        if extra:
            raise PlantkgError(ErrorCode.INVALID_REQUEST,
                               f"拒绝只允许修订 system_error，收到：{'、'.join(extra)}")
        if kind not in REJECT_EDITS_KINDS:
            allowed = "、".join(sorted(REJECT_EDITS_KINDS))
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"{kind} 的拒绝不接受修订；只允许 {allowed}")

    def _refuse_if_executing(self, action_id: str) -> None:
        row = read(self.writer, lambda con: con.execute(
            "SELECT status FROM action_intents WHERE plant_id=? AND action_id=?", (self.plant_id, action_id)).fetchone())
        if row is not None:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已进入执行（{row[0]}），不能拒绝")

    def _commit_decision(self, con: Any, action_id: str, expected_revision: int, target: str, decided: PendingAction,
                         result: ToolResult | None, key: str, request_hash: str, at: datetime,
                         edits: Mapping[str, Any] | None = None) -> None:
        current = self._load(con, action_id)[0]
        if current.revision != expected_revision:
            raise PlantkgError(ErrorCode.REVISION_CONFLICT, "动作修订已变化")
        con.execute("UPDATE pending_actions SET status=?, record=?, result=? WHERE plant_id=? AND action_id=?",
                    (target, decided.model_dump_json(), result.model_dump_json() if result else None, self.plant_id, action_id))
        con.execute("INSERT INTO action_decisions(plant_id,idempotency_key,action_id,request_hash,edits) VALUES (?,?,?,?,?)",
                    (self.plant_id, key, action_id, request_hash,
                     json.dumps(dict(edits), ensure_ascii=False, sort_keys=True) if edits is not None else None))
        if target == "confirmed":
            con.execute("UPDATE action_intents SET status='done', finished_at=?, record=? "
                        "WHERE plant_id=? AND action_id=? AND status='confirming'",
                        (at.isoformat(), decided.model_dump_json(), self.plant_id, action_id))

    def _recover_intents(self) -> None:
        """启动扫描：上次停在“确认中”的动作可能已执行一半，不自动重放，标 needs_repair 等人工核查。"""
        atomic(self.writer, lambda con: con.execute(
            "UPDATE action_intents SET status='needs_repair' WHERE plant_id=? AND status='confirming'", (self.plant_id,)))

    def _expire_due(self, action_id: str | None = None) -> None:
        with self._decide:
            rows = read(self.writer, lambda con: con.execute(
                "SELECT record FROM pending_actions WHERE plant_id=? AND status='proposed'" +
                (" AND action_id=?" if action_id is not None else ""),
                (self.plant_id, action_id) if action_id is not None else (self.plant_id,)).fetchall())
            due = []
            for (record,) in rows:
                action = PendingAction.model_validate_json(record)
                try:
                    if action.expires_at is not None and action.expires_at <= self._now(action.kind, action.proposed_by.run_id):
                        due.append((action, record))
                except PlantkgError:
                    if action_id is not None:
                        raise  # The requested action cannot be decided without its clock.
                    log.warning("无法读取动作 %s 的时钟，跳过过期扫描", action.action_id)
            if not due:
                return

            def write(con):
                for action, record in due:
                    expired = action.model_copy(update={"status": "expired", "revision": action.revision + 1})
                    con.execute("UPDATE pending_actions SET status='expired', record=? WHERE plant_id=? AND action_id=? AND status='proposed' AND record=?",
                                (expired.model_dump_json(), self.plant_id, action.action_id, record))
            atomic(self.writer, write)

    def _now(self, kind: str, run_id: str | None) -> datetime:
        return self._action_clock(kind, run_id) if self._action_clock else self._clock()

    def _load(self, con: Any, action_id: str) -> tuple[PendingAction, str | None]:
        row = con.execute("SELECT record, result FROM pending_actions WHERE plant_id=? AND action_id=?", (self.plant_id, action_id)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"待确认动作 {action_id} 不存在")
        return PendingAction.model_validate_json(row[0]), row[1]

    def _record(self, action: PendingAction, event_type: str, trace_id: str, summary: str) -> None:
        """决定已提交后补写证据；证据写失败只记日志，不让已生效的决定对调用方报错。"""
        if self._evidence is None:
            return
        try:
            self._append_evidence(action, event_type, trace_id, summary)
        except Exception:  # noqa: BLE001
            log.exception("动作 %s 的证据事件写入失败", action.action_id)

    def _append_evidence(self, action: PendingAction, event_type: str, trace_id: str, summary: str) -> None:
        self._evidence.append(EvidenceEvent(
            event_id=f"{action.action_id}-r{action.revision}", seq=0, occurred_at=self._now(action.kind, action.proposed_by.run_id), agent_run_id=action.proposed_by.agent_run_id,
            run_id=action.proposed_by.run_id, trace_id=trace_id, action_id=action.action_id, event_type=event_type,
            result_status="ok" if action.status != "rejected" else "conflict", params={"kind": action.kind, "status": action.status},
            summary=summary[:2000]))


class OperatorAuth:
    """工作台登录接入前的本机运维身份：令牌只存在服务进程内存与本机令牌文件里。"""

    def __init__(self, reviewers: Sequence[str]) -> None:
        if isinstance(reviewers, str) or not all(isinstance(r, str) and r for r in reviewers):
            raise ValueError("actions.reviewers 必须是复核员 ID 列表")
        self.token = secrets.token_urlsafe(32)
        self._reviewers = frozenset(reviewers)

    def authenticate(self, headers: Mapping[str, str]) -> Actor:
        token, actor_id = headers.get(TOKEN_HEADER, ""), headers.get(ACTOR_HEADER, "")
        if not token or not hmac.compare_digest(token, self.token):
            raise PlantkgError(ErrorCode.FORBIDDEN, f"{WORKBENCH_LOGIN_PENDING}；本机请用 python -m plantkg actions")
        if actor_id not in self._reviewers:
            raise PlantkgError(ErrorCode.FORBIDDEN, f"{actor_id or '未提供身份'} 不在配置 actions.reviewers 中")
        return Actor(actor_id=actor_id, roles=(Role.reviewer,))


def rest_providers(store: SqlitePendingActionStore, auth: OperatorAuth) -> dict[tuple[str, str], Callable[..., Any]]:
    """GET 提供者签名 provider(context, path, query)；POST 提供者另收 body 与 headers（由提供者自行认证）。"""
    def actions(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> Page[PendingAction]:
        items, next_cursor = store.list(query.get("status"), query.get("kind"), query.get("cursor"), query.get("page_size"))
        return Page[PendingAction](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)

    def action(context: Context, path: Mapping[str, str], query: Mapping[str, str]) -> PendingAction:
        return store.get(path["action_id"])

    def decide(target: str):
        def provider(context: Context, path: Mapping[str, str], query: Mapping[str, str], *, body: Any, headers: Mapping[str, str]) -> PendingAction:
            actor = auth.authenticate(headers)
            request = ActionDecisionRequest.model_validate(body)
            if target == "confirm":
                return store.confirm(path["action_id"], actor, request.reason, request.expected_revision, request.idempotency_key,
                                     request.edits, trace_id=context.trace_id)
            return store.reject(path["action_id"], actor, request.reason, request.expected_revision, request.idempotency_key,
                                request.edits, trace_id=context.trace_id)
        return provider

    return {("GET", "/actions"): actions, ("GET", "/actions/{action_id}"): action,
            ("POST", "/actions/{action_id}/confirm"): decide("confirm"), ("POST", "/actions/{action_id}/reject"): decide("reject")}


def decision_key(action_id: str, decision: str, revision: int, actor_id: str) -> str:
    """命令行默认幂等键：同一人对同一修订的同一决定重试时得到同一键。"""
    return "cli-" + digest([action_id, decision, revision, actor_id])[:32]


# ---------------------------------------------------------------- 命令行（python -m plantkg actions ...）


def token_path(settings: Any) -> Path:
    """本机运维令牌文件；相对路径按安装包所在目录的父目录解析（与 storage 路径一致）。"""
    value = (settings.section("actions") or {}).get("token_file", "runtime/operator.token")
    path = Path(value)
    return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path


def write_token(settings: Any, auth: OperatorAuth) -> Path:
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(auth.token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def cli(argv: Sequence[str], settings: Any) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg actions", description="在工作台完成前确认或拒绝待确认动作（调用本机运行中的服务）")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--status", default="proposed")
    listing.add_argument("--kind")
    for name in ("confirm", "reject"):
        p = sub.add_parser(name)
        p.add_argument("action_id")
        p.add_argument("--actor", required=True, help="复核员 ID，须在配置 actions.reviewers 中")
        p.add_argument("--reason", required=True)
        p.add_argument("--expected-revision", type=int, help="缺省取当前修订")
        p.add_argument("--idempotency-key")
        if name == "confirm":
            p.add_argument("--edits", help="JSON 对象，仅复核结论")
    args = parser.parse_args(argv)
    base = f"http://{settings.server.host}:{settings.server.port}{API_PREFIX}"

    def call(method: str, path: str, body: Any = None) -> Any:
        headers = {"content-type": "application/json"}
        if method == "POST":
            try:
                headers |= {TOKEN_HEADER: token_path(settings).read_text(encoding="utf-8").strip(), ACTOR_HEADER: args.actor}
            except OSError:
                raise SystemExit(f"找不到运维令牌 {token_path(settings)}：请先在本机用同一配置启动服务（storage.enabled=true）")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error", {})
            except ValueError:
                error = {}
            raise SystemExit(f"{error.get('code', exc.code)}: {error.get('message', exc.reason)}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"连不上服务 {base}：{exc.reason}")

    if args.command == "list":
        query = urllib.parse.urlencode({k: v for k, v in (("status", args.status), ("kind", args.kind)) if v})
        for a in call("GET", "/actions?" + query)["items"]:
            print(f"{a['action_id']}  {a['kind']:<18} {a['status']:<9} rev={a['revision']}  target={a.get('target_ref') or '-'}  "
                  f"proposed={a['proposed_at']}  expires={a.get('expires_at') or '-'}")
        return 0
    revision = args.expected_revision
    if revision is None:
        revision = call("GET", f"/actions/{urllib.parse.quote(args.action_id)}")["revision"]
    body = {"reason": args.reason, "expected_revision": revision,
            "idempotency_key": args.idempotency_key or decision_key(args.action_id, args.command, revision, args.actor)}
    if getattr(args, "edits", None):
        body["edits"] = json.loads(args.edits)
    action = call("POST", f"/actions/{urllib.parse.quote(args.action_id)}/{args.command}", body)
    print(f"{action['action_id']} → {action['status']}（rev={action['revision']}，{action['decided_by']}）")
    return 0
