"""运行会话（B04）：网关每次 Agent 运行登记用户、角色、厂区、as_of、可见文档与过期时间，以 128 位随机 ctx 索引。

只放在服务进程内存里：ctx 不落库、不进日志；服务重启后旧 ctx 一律查不到（失败关闭）。
运行结束调用 expire / expire_agent_run 即失效；过期的会话在过期后保留一段时间，供解析器区分“已过期”与“查不到”。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from plantkg.core.contracts import Actor, ErrorCode, PlantkgError, RunSession
from plantkg.core.versions import utcnow

MAX_TTL_S = 24 * 3600
EXPIRED_GRACE = timedelta(hours=1)


class MemoryRunSessionStore:
    """RunSessionStore 端口实现。"""

    def __init__(self, industry_pack: str, clock: Callable[[], datetime] = utcnow) -> None:
        self._industry_pack, self._clock = industry_pack, clock
        self._sessions: dict[str, RunSession] = {}
        self._run_bindings: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def register(self, actor: Actor, plant_id: str, as_of: datetime, visible_docs: Sequence[str], ttl_s: int, agent_run_id: str, trace_id: str,
                 industry_pack: str | None = None, origin: str = "gateway", *, run_id: str | None = None,
                 version_manifest_id: str | None = None) -> RunSession:
        if bool(run_id) != bool(version_manifest_id):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "运行和清单须同时绑定")
        if not 0 < ttl_s <= MAX_TTL_S:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"会话有效期须在 1～{MAX_TTL_S} 秒")
        now = self._clock()
        session = RunSession(ctx=secrets.token_hex(16), actor=actor, plant_id=plant_id, industry_pack=industry_pack or self._industry_pack,
                             as_of=as_of, visible_document_paths=tuple(visible_docs), agent_run_id=agent_run_id, trace_id=trace_id,
                             created_at=now, expires_at=now + timedelta(seconds=ttl_s), origin=origin)
        with self._lock:
            self._purge(now)
            self._sessions[session.ctx] = session
            if run_id and version_manifest_id:
                self._run_bindings[session.ctx] = (run_id, version_manifest_id)
        return session

    def resolve_binding(self, ctx: str) -> tuple[str, str] | None:
        with self._lock:
            return self._run_bindings.get(ctx)

    def resolve(self, ctx: str) -> RunSession | None:
        """返回登记过的会话，包括刚过期的（由 SessionCtxResolver 判定 CTX_EXPIRED）。"""
        with self._lock:
            return self._sessions.get(ctx)

    def expire(self, ctx: str) -> None:
        with self._lock:
            self._sessions.pop(ctx, None)
            self._run_bindings.pop(ctx, None)

    def expire_agent_run(self, agent_run_id: str) -> int:
        """Agent 运行结束：使该运行登记的全部会话失效，返回失效个数。"""
        with self._lock:
            ctxs = [k for k, s in self._sessions.items() if s.agent_run_id == agent_run_id]
            for k in ctxs:
                del self._sessions[k]
                self._run_bindings.pop(k, None)
        return len(ctxs)

    def _purge(self, now: datetime) -> None:
        for k in [k for k, s in self._sessions.items() if s.expires_at + EXPIRED_GRACE <= now]:
            del self._sessions[k]
            self._run_bindings.pop(k, None)
