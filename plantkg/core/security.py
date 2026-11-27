"""安全壳：ctx 解析、失败关闭的工具守卫、日志脱敏（ADR-0002）。

| 情况 | 写入/提议/后台任务工具 | 只读工具 |
| --- | --- | --- |
| 无 ctx | PLANTKG_E_NO_CTX | 降级结果，文本以 PLANTKG_NOTICE_NO_CTX: 开头 |
| 查不到或伪造 | PLANTKG_E_UNKNOWN_CTX | 同左 |
| 已过期 | PLANTKG_E_CTX_EXPIRED | 同左 |
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from plantkg.core.contracts import (
    NOTICE_NO_CTX_PREFIX,
    Actor,
    BusinessStatus,
    Context,
    ContextChannel,
    ErrorCode,
    PlantkgError,
    ToolResult,
    ToolSpec,
    contains_secret,
    safe_error_details,
    tool_error,
)
from plantkg.core.ports import RunSessionStore

log = logging.getLogger("plantkg.security")
NO_CTX_NOTICE = "本次运行没有网关会话（ctx），只读查看；写入请从工作台发起"

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_trace_id() -> str:
    return "tr-" + secrets.token_hex(8)


class SessionCtxResolver:
    """CtxResolver：按 ctx 查 RunSession 得到 Context。查不到返回 None；classify 给出失败原因。"""

    def __init__(self, store: RunSessionStore, clock: Clock = _utcnow) -> None:
        self._store = store
        self._clock = clock

    def classify(self, ctx: str) -> tuple[Context | None, ErrorCode | None]:
        session = self._store.resolve(ctx)
        if session is None:
            return None, ErrorCode.UNKNOWN_CTX
        if session.expires_at <= self._clock():
            return None, ErrorCode.CTX_EXPIRED
        context = Context(
            plant_id=session.plant_id,
            industry_pack=session.industry_pack,
            actor=session.actor,
            channel=ContextChannel.gateway if session.origin == "gateway" else ContextChannel.mock_default,
            agent_run_id=session.agent_run_id,
            trace_id=session.trace_id,
            as_of=session.as_of,
        )
        binding = self._store.resolve_binding(ctx)
        if binding:
            context = context.model_copy(update={"run_id": binding[0], "version_manifest_id": binding[1]})
        return context, None

    def resolve(self, ctx: str | None) -> Context | None:
        if not ctx:
            return None
        return self.classify(ctx)[0]


class ToolGuard:
    """把注册表里的业务处理函数包成给 Nexent 的工具：先解析 ctx、失败关闭，再转成 ≤ 8KB 文本。

    handler 签名：handler(context: Context, **params) -> ToolResult；可抛 PlantkgError。
    """

    def __init__(
        self,
        resolver: SessionCtxResolver,
        plant_id: str,
        industry_pack: str,
        default_as_of: datetime,
        default_ctx_actor: Actor | None = None,
        secrets_to_block: tuple[str, ...] = (),
    ) -> None:
        self._secrets = secrets_to_block  # 进程里的密钥值（Nexent/百炼 key），出站一并检查
        self._resolver = resolver
        self._plant_id = plant_id
        self._industry_pack = industry_pack
        self._default_as_of = default_as_of
        self._default_actor = default_ctx_actor  # 只有 mock/dev 且显式开启时非空
        # 工具调用观察者（B23 网关按 ctx 归属到 Agent 并合流）：observer(phase, call)；phase 为 started / finished
        self.observers: list[Callable[[str, dict[str, Any]], None]] = []

    def _notify(self, phase: str, call: dict[str, Any]) -> None:
        for observer in self.observers:
            try:
                observer(phase, call)
            except Exception:  # noqa: BLE001 — 观察者失败不影响工具本身
                log.exception("工具调用观察者出错")

    def context_for(self, spec: ToolSpec, ctx: str) -> Context | ToolResult:
        if ctx:
            context, error = self._resolver.classify(ctx)
            if error is not None:
                return tool_error(error, "会话不存在、已过期或不是网关签发的；请从工作台重新发起")
            return context
        if self._default_actor is not None:
            return self._base_context(ContextChannel.mock_default, self._default_actor)
        if spec.requires_ctx:
            return tool_error(ErrorCode.NO_CTX, "写入、提议与后台任务需要经工作台网关发起的运行")
        return self._base_context(ContextChannel.none, None)

    def anonymous_read_context(self) -> Context:
        """工作台登录接入前的 REST 只读上下文：无身份、只读、按默认查看时点过滤。"""
        return self._base_context(ContextChannel.none, None)

    def _base_context(self, channel: ContextChannel, actor: Actor | None) -> Context:
        return Context(
            plant_id=self._plant_id,
            industry_pack=self._industry_pack,
            actor=actor,
            channel=channel,
            trace_id=new_trace_id(),
            as_of=self._default_as_of,
        )

    def call(self, spec: ToolSpec, handler: Callable[..., ToolResult], ctx: str, params: dict[str, Any]) -> str:
        context = self.context_for(spec, ctx)
        if isinstance(context, ToolResult):
            return context.to_agent_text()
        call = {"call_id": "call-" + secrets.token_hex(8), "ctx": ctx, "tool": spec.name, "params": dict(params),
                "context": context, "started_at": _utcnow()}
        self._notify("started", call)
        text, result = self._run(spec, handler, ctx, context, params)
        call.update(finished_at=_utcnow(), result=result, text=text)
        self._notify("finished", call)
        return text

    def _run(self, spec: ToolSpec, handler: Callable[..., ToolResult], ctx: str, context: Context,
             params: dict[str, Any]) -> tuple[str, ToolResult]:
        try:
            result = handler(context, **params)
        except PlantkgError as exc:
            result = tool_error(exc.code, exc.message)
        except ValidationError as exc:
            result = tool_error(ErrorCode.INVALID_REQUEST, "参数或结果不符合契约", errors=safe_error_details(exc))
        except Exception:  # noqa: BLE001 — 工具边界兜底；不记录参数原文
            log.exception("工具 %s 内部错误", spec.name)
            result = tool_error(ErrorCode.INTERNAL, f"内部错误，trace={context.trace_id}")
        if context.channel == "none" and result.ok and result.business_status == "done":
            result = result.model_copy(update={"business_status": BusinessStatus.degraded, "notice": NO_CTX_NOTICE})
        try:
            text = result.to_agent_text()
        except PlantkgError as exc:  # 超 8KB 等：观察者也只拿替换后的结果
            result = tool_error(exc.code, exc.message)
            text = result.to_agent_text()
        if leaks_secret(text, (ctx, *self._secrets)):
            log.error("工具 %s 的结果回显了 ctx 或密钥，已拦截", spec.name)
            result = tool_error(ErrorCode.INTERNAL, f"结果未通过出站检查，trace={context.trace_id}")  # 观察者（SSE、落库）也只拿拦截后的结果
            text = result.to_agent_text()
        return text, result


_SEPARATORS = re.compile(r"[\s\-_]+")


def leaks_secret(text: str, secret_values: tuple[str, ...]) -> bool:
    """出站检查：不区分大小写，并去掉空白、连字符、下划线后再比对，防大写或拆分绕过。"""
    if contains_secret(text, tuple(s for s in secret_values if s)):
        return True
    folded = _SEPARATORS.sub("", text).lower()
    return any(s and _SEPARATORS.sub("", s).lower() in folded for s in secret_values)


# ---------------------------------------------------------------- 日志脱敏

_REDACTIONS = (
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"), "<ctx>"),  # ctx；64 位内容散列不受影响
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "<api-key>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "<jwt>"),
)


def redact(text: str, secret_values: tuple[str, ...] = ()) -> str:
    for value in secret_values:
        if value:
            text = text.replace(value, "<secret>")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """日志出口兜底：消息与异常堆栈格式化后统一脱敏；secret_values 为进程里的真实密钥值。"""

    def __init__(self, secret_values: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = secret_values

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, self._secrets)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            # Formatter 只在 exc_text 为空时才渲染堆栈，这里先渲染并脱敏
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


__all__ = ["NOTICE_NO_CTX_PREFIX", "RedactingFilter", "SessionCtxResolver", "ToolGuard", "new_trace_id", "redact"]
