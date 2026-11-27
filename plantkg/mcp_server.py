"""MCP 薄入口：把注册表里的工具按契约 ToolSpec 生成 FastMCP 工具。

只做参数绑定、ctx 解析与结果转换（ToolGuard）；不写策略，新增工具只改注册表登记，不改本文件。
Nexent 侧的工具名 = plantkg_ + 注册名；ctx 可选、默认空，由网关经 tool_params 注入（ADR-0002）。
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from plantkg.core.registry import ToolBinding

CTX_DESCRIPTION = "由系统注入，不要填写"
_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list}
_P = inspect.Parameter


def _signature(binding: ToolBinding) -> inspect.Signature:
    spec = binding.spec
    required = [p for p in spec.params if p.required]
    optional = [p for p in spec.params if not p.required]
    params = []
    for p in required:
        params.append(_P(p.name, _P.POSITIONAL_OR_KEYWORD, annotation=Annotated[_TYPES[p.type], Field(description=p.description, ge=p.minimum, le=p.maximum)]))
    for p in optional:
        ann = Annotated[_TYPES[p.type] | None, Field(description=p.description, ge=p.minimum, le=p.maximum)]
        params.append(_P(p.name, _P.POSITIONAL_OR_KEYWORD, annotation=ann, default=None))
    if spec.accepts_ctx:
        params.append(_P("ctx", _P.POSITIONAL_OR_KEYWORD, annotation=Annotated[str, Field(description=CTX_DESCRIPTION)], default=""))
    return inspect.Signature(params, return_annotation=str)


def _tool_function(binding: ToolBinding, guard: Any):
    sig = _signature(binding)

    def tool(*args: Any, **kwargs: Any) -> str:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        ctx = arguments.pop("ctx", "") or ""
        params = {k: v for k, v in arguments.items() if v is not None}
        return guard.call(binding.spec, binding.handler, ctx, params)

    tool.__signature__ = sig
    tool.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()} | {"return": str}
    tool.__name__ = binding.spec.nexent_name
    return tool


def create_mcp_server(services: Any) -> FastMCP:
    mcp = FastMCP("plantkg")
    for binding in services.registry.tools.values():
        if not binding.spec.exposed_to_agent:
            continue
        mcp.tool(_tool_function(binding, services.guard), name=binding.spec.nexent_name, description=binding.spec.description)
    return mcp
