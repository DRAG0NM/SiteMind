"""HTTP 薄入口：按契约 API_ROUTES 生成 /api/v1 路由，并在同一进程挂载 MCP（ADR-0006）。

GET 交给注册表里的提供者；未接入的接口返回 503 并说明由哪个分支提供。写入限统一确认、网关与受控资料入库：
登记了提供者的写入路由把请求体与请求头交给提供者自行认证（B04 确认端点在工作台登录接入前只认本机运维令牌），
其余写入一律拒绝。提供者在线程池执行，不阻塞事件循环（get_job 最多等 40 秒）。错误体为 ErrorResponse，状态码按 ERROR_HTTP_STATUS。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.convertors import Convertor, register_url_convertor
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from plantkg import __version__
from plantkg.core.actions import WORKBENCH_LOGIN_PENDING
from plantkg.core.contracts import (
    API_PREFIX,
    API_ROUTES,
    CONTRACT_VERSION,
    ERROR_HTTP_STATUS,
    ErrorCode,
    ErrorResponse,
    HealthResponse,
    RETRYABLE_ERRORS,
    PlantkgError,
    RouteSpec,
    ToolError,
    safe_error_details,
)

log = logging.getLogger("plantkg.api")


def error_response(code: ErrorCode, message: str, trace_id: str | None = None, **details: Any) -> JSONResponse:
    body = ErrorResponse(error=ToolError(code=code.value, message=message, retryable=code.value in RETRYABLE_ERRORS, details=details), trace_id=trace_id)
    return JSONResponse(body.model_dump(mode="json"), status_code=ERROR_HTTP_STATUS[code.value])


def _endpoint(route: RouteSpec, services: Any):
    key = (route.method, route.path)

    async def handle(request: Request) -> JSONResponse:
        if route.path == "/health":
            return JSONResponse(_health(services).model_dump(mode="json"))
        provider = services.registry.rest.get(key)
        if route.write:
            if route.path.startswith("/agent/") and not services.settings.nexent.enabled:
                return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "Nexent 未启用（nexent.enabled=false）")
            if provider is None:
                return error_response(ErrorCode.FORBIDDEN,
                                      f"写入默认拒绝：{WORKBENCH_LOGIN_PENDING}。本地演示请用 "
                                      "python -m plantkg --profile demo 启动（开启存储，令牌由后端写入 "
                                      "runtime/demo/operator.token），再用 python -m plantkg --profile demo actions list|confirm "
                                      "带本机运维令牌完成确认")
        elif provider is None:
            return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, f"{route.method} {route.path} 由 {route.provider} 提供，尚未接入")
        context = services.guard.anonymous_read_context()
        try:
            args = (context, dict(request.path_params), dict(request.query_params))
            if route.write:
                try:
                    body = await request.json()
                except ValueError:
                    return error_response(ErrorCode.INVALID_REQUEST, "请求体必须是 JSON", context.trace_id)
                result = await run_in_threadpool(provider, *args, body=body, headers=dict(request.headers))
                if route.stream:  # 网关（B23）：提供者返回 events（UIEvent 异步迭代器）与响应头，按 SSE 发出
                    idle_s = services.settings.nexent.run_deadline_s + 30  # 合流停止推进时的消费兜底
                    return StreamingResponse(_sse(result.events, idle_s), media_type="text/event-stream",
                                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **result.headers})
            else:
                result = await run_in_threadpool(provider, *args)
            body = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
            return JSONResponse(body)  # 序列化失败（NaN、非 JSON 类型）也落进下面的统一错误体
        except PlantkgError as exc:
            return error_response(exc.code, exc.message, context.trace_id)
        except ValidationError as exc:
            return error_response(ErrorCode.INVALID_REQUEST, "请求或结果不符合契约", context.trace_id, errors=safe_error_details(exc))
        except Exception:  # noqa: BLE001 — 入口兜底，不回显内部细节
            log.exception("%s %s 内部错误", route.method, route.path)
            return error_response(ErrorCode.INTERNAL, "内部错误", context.trace_id)

    return handle


async def _sse(events: Any, idle_s: float):
    """按事件流发出；合流线程停住时按截止时间兜底断开，不让工作台永久转圈。"""
    iterator = aiter(events)
    while True:
        try:
            event = await asyncio.wait_for(anext(iterator), timeout=idle_s)
        except StopAsyncIteration:
            return
        except TimeoutError:
            yield ": 事件流空闲超时\n\n"
            return
        yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _health(services: Any) -> HealthResponse:
    """只报告真实知道的状态：未做连通检查的外部依赖记 unchecked，占位包记 placeholder，Fake 顶替记 fake。"""
    settings = services.settings
    registry = services.registry
    deps: dict[str, str] = {
        "nexent": "unchecked" if settings.nexent.enabled else "disabled",
        "judge": "fake" if settings.judge.backend == "fake" else "unchecked",
    }
    if services.storage is not None:
        deps["storage"] = "unchecked" if services.storage.enabled else "disabled"
    for name in settings.packs:
        manifest = registry.packs.get(name)
        deps[f"pack.{name}"] = "unavailable" if manifest is None else "placeholder" if manifest.status == "placeholder" else "ok"
    for name in registry.fake_bindings():
        deps[f"fake.{name}"] = "fake"
    for name in registry.placeholder_bindings():
        deps[f"placeholder.{name}"] = "unavailable"
    degraded = any(v != "ok" and v != "disabled" for v in deps.values())  # 只有全部已验证或已关闭才算 ok
    return HealthResponse(
        status="degraded" if degraded else "ok",
        profile=settings.profile,
        service_version=__version__,
        contract_version=CONTRACT_VERSION,
        dependencies=deps,
    )


class _AssetVersionIdConvertor(Convertor):
    """asset://{行业}/{哈希8位}/{类型}/{定位}：恰好四段、定位不含 "/"，
    所以 …/policy/fragments 是资料 fragments 的明细，…/policy/x/fragments 才是资料 x 的片段子路由，与路由顺序无关。"""

    regex = r"asset://[a-z0-9_-]+/[0-9a-f]{8}/[a-z0-9_-]+/[^/]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("asset_version_id", _AssetVersionIdConvertor())
_TYPED_PARAMS = ("asset_version_id",)


def _route_path(path: str) -> str:
    for name in _TYPED_PARAMS:
        path = path.replace("{%s}" % name, "{%s:%s}" % (name, name))
    return API_PREFIX + path


def create_app(services: Any, mcp: Any | None = None) -> Starlette:
    routes: list[Any] = [
        Route(_route_path(r.path), _endpoint(r, services), methods=[r.method], name=f"{r.method} {r.path}") for r in API_ROUTES
    ]
    mcp_lifespan = None
    if mcp is not None:
        # 与探针实测配置一致：容器经 host.docker.internal 访问时 Host 头不是回环地址，不做 Host 来源限制
        try:
            mcp_app = mcp.http_app(transport="http", path=services.settings.server.mcp_path, host_origin_protection=False)
        except BaseException:
            services.close()
            raise
        routes.append(Mount("/", app=mcp_app))
        mcp_lifespan = mcp_app.lifespan

    @asynccontextmanager
    async def lifespan(app):
        try:
            if mcp_lifespan is None:
                yield
            else:
                async with mcp_lifespan(app):
                    yield
        finally:
            services.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.services = services
    return app
