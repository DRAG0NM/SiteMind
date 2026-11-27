"""由 plantkg/core/contracts.py 与 ports.py 生成 contracts/ 下的机器可读契约。

    python contracts/build.py           # 重新生成
    python contracts/build.py --check   # 只校验，生成结果过期时退出码 1

生成物都是标准库 json 可读的文件；手写文件（README、events/nexent-sse-mapping.md、examples/）不由本脚本改动。
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from plantkg.core import contracts as c  # noqa: E402
from plantkg.core import ports  # noqa: E402

OUT = ROOT / "contracts"
QUERY_SCHEMAS = {
    "document_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
    "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
    "wait": {"type": "integer", "minimum": 0, "maximum": c.JOB_WAIT_MAX_S},
    "revision": {"type": "integer", "minimum": 0},
}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def resolve_model(name: str) -> type[BaseModel]:
    match = re.fullmatch(r"Page\[(\w+)\]", name)
    if match:
        return c.Page[getattr(c, match.group(1))]
    return getattr(c, name)


def build_schemas() -> dict[str, str]:
    files = {}
    for name, model in c.record_models().items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"plantkg:contracts/{c.CONTRACT_VERSION}/{name}"
        files[f"schemas/{name}.schema.json"] = _dump(schema)
    return files


def build_day() -> dict[str, str]:
    """“一天”数据格式（B46）：DayRecord 的完整 schema，开发工作台实时流与回放展示页共用。"""
    schema = c.DayRecord.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"plantkg:contracts/{c.CONTRACT_VERSION}/day"
    return {"day.schema.json": _dump(schema)}


def build_enums() -> dict[str, str]:
    enums = {name: [m.value for m in enum] for name, enum in c.enum_types().items()}
    constants = {
        "contract_version": c.CONTRACT_VERSION,
        "tool_result_schema_version": c.TOOL_RESULT_SCHEMA_VERSION,
        "api_prefix": c.API_PREFIX,
        "tool_prefix": c.TOOL_PREFIX,
        "tool_text_max_bytes": c.TOOL_TEXT_MAX_BYTES,
        "tool_deadline_s": c.TOOL_DEADLINE_S,
        "job_wait_max_s": c.JOB_WAIT_MAX_S,
        "error_prefix": c.ERROR_PREFIX,
        "notice_no_ctx_prefix": c.NOTICE_NO_CTX_PREFIX,
        "injected_param_names": sorted(c.INJECTED_PARAM_NAMES),
        "ledger_tolerance": c.LEDGER_TOLERANCE,
        "error_http_status": c.ERROR_HTTP_STATUS,
        "retryable_errors": sorted(c.RETRYABLE_ERRORS),
        "compilable_review_statuses": sorted(c.COMPILABLE_REVIEW_STATUSES),
        "solver_statuses_with_plan": sorted(c.SOLVER_STATUSES_WITH_PLAN),
        "undecidable_choices": sorted(c.UNDECIDABLE_CHOICES),
    }
    transitions = {
        "PendingAction": {k: sorted(v) for k, v in c.PENDING_ACTION_TRANSITIONS.items()},
        "Job": {k: sorted(v) for k, v in c.JOB_TRANSITIONS.items()},
        "VersionManifest": {k: sorted(v) for k, v in c.MANIFEST_TRANSITIONS.items()},
        "Candidate": {k: sorted(v) for k, v in c.CANDIDATE_TRANSITIONS.items()},
        "Plan": {k: sorted(v) for k, v in c.PLAN_TRANSITIONS.items()},
    }
    return {"enums.json": _dump({"enums": enums, "constants": constants, "transitions": transitions})}


def build_events() -> dict[str, str]:
    events = {
        "ui_event_types": [t.value for t in c.UIEventType],
        "payload_required_keys": {k: list(v) for k, v in c.UI_EVENT_PAYLOAD_KEYS.items()},
        "terminal_events": sorted(c.TERMINAL_UI_EVENTS),
        "terminal_reason_by_event": {k: sorted(v) for k, v in c.TERMINAL_REASON_BY_EVENT.items()},
        "evidence_event_types": [t.value for t in c.EvidenceEventType],
        "keepalive_interval_s": 15,
    }
    return {"events/events.json": _dump(events)}


def tool_input_schema(spec: c.ToolSpec) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for p in spec.params:
        props[p.name] = {"type": p.type, "description": p.description}
        if p.minimum is not None:
            props[p.name]["minimum"] = p.minimum
        if p.maximum is not None:
            props[p.name]["maximum"] = p.maximum
    if spec.accepts_ctx:
        # 可选、默认空：由网关经 tool_params 注入，模型看不到也改不了（07 Q05）
        props["ctx"] = {"type": "string", "default": "", "description": "网关注入的运行会话标识；不要填写"}
    return {
        "type": "object",
        "properties": props,
        "required": [p.name for p in spec.params if p.required],
        "additionalProperties": False,
    }


def build_mcp() -> dict[str, str]:
    tools = []
    for spec in c.TOOL_SPECS:
        tools.append(
            {
                "name": spec.nexent_name,
                "registry_name": spec.name,
                "plane": spec.plane,
                "provider": spec.provider,
                "extended_by": list(spec.extended_by),
                "mode": spec.mode,
                "accepts_ctx": spec.accepts_ctx,
                "requires_ctx": spec.requires_ctx,
                "exposed_to_agent": spec.exposed_to_agent,
                "optional": spec.optional,
                "paginated": spec.paginated,
                "description": spec.description,
                "inputSchema": tool_input_schema(spec),
                "outputSchemaRef": "schemas/ToolResult.schema.json",
            }
        )
    return {"mcp/tools.json": _dump({"contract_version": c.CONTRACT_VERSION, "tools": tools})}


def build_openapi() -> dict[str, str]:
    # 响应按序列化口径（带默认值的字段也列为 required），请求按校验口径
    wanted = {("ErrorResponse", "serialization")}
    for r in c.API_ROUTES:
        wanted.add((r.response, "serialization"))
        if r.request:
            wanted.add((r.request, "validation"))
    pairs = sorted(((resolve_model(n), mode) for n, mode in wanted), key=lambda p: (p[0].__name__, p[1]))
    key_map, top = models_json_schema(pairs, ref_template="#/components/schemas/{model}")

    def ref(name: str, mode: str = "serialization") -> dict[str, Any]:
        return key_map[(resolve_model(name), mode)]

    paths: dict[str, Any] = {}
    for r in c.API_ROUTES:
        params = [
            {"name": p, "in": "path", "required": True, "schema": {"type": "string"}} for p in c.route_path_params(r)
        ] + [{"name": q, "in": "query", "required": q in r.required_query, "schema": QUERY_SCHEMAS.get(q, {"type": "string"})} for q in r.query]
        if r.stream:
            ok_content = {"text/event-stream": {"schema": ref(r.response), "x-plantkg-sse": "每条 data 是一个 UIEvent"}}
        else:
            ok_content = {"application/json": {"schema": ref(r.response)}}
        op: dict[str, Any] = {
            "summary": r.summary,
            "operationId": _operation_id(r),
            "x-plantkg-provider": r.provider,
            "x-plantkg-role": r.role,
            "x-plantkg-write": r.write,
            "parameters": params,
            "responses": {
                "200": {"description": "成功", "content": ok_content},
                "default": {"description": "错误；状态码见 enums.json error_http_status", "content": {"application/json": {"schema": ref("ErrorResponse")}}},
            },
        }
        if r.request:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": ref(r.request, "validation")}}}
        if r.role != "public":
            op["security"] = [{"workbenchSession": []}]
        paths.setdefault(c.API_PREFIX + r.path, {})[r.method.lower()] = op

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "plantkg API",
            "version": c.CONTRACT_VERSION,
            "description": "REST 写入限于统一确认、网关和受控资料入库；入库不发布知识。错误体为 ErrorResponse，不靠 HTTP 200 判业务成功。",
        },
        "paths": paths,
        "components": {
            "schemas": top.get("$defs", {}),
            "securitySchemes": {
                "workbenchSession": {"type": "http", "scheme": "bearer", "description": "工作台登录会话；actor 由服务端认证，不接受模型自报"}
            },
        },
    }
    return {"openapi/plantkg.openapi.json": _dump(doc)}


def _operation_id(r: c.RouteSpec) -> str:
    parts = [p for p in re.split(r"[/{}-]+", r.path) if p]
    return r.method.lower() + "_" + "_".join(parts)


def _annotation_text(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "Any"
    return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))


def build_ports() -> dict[str, str]:
    result = {}
    for name, obj in vars(ports).items():
        if not (inspect.isclass(obj) and getattr(obj, "_is_protocol", False) and obj.__module__ == ports.__name__):
            continue
        members: dict[str, Any] = {"attributes": {}, "methods": {}}
        for attr, ann in getattr(obj, "__annotations__", {}).items():
            members["attributes"][attr] = _annotation_text(ann)
        for meth_name, meth in vars(obj).items():
            if meth_name.startswith("_"):
                continue
            func = meth.fget if isinstance(meth, property) else meth
            if not callable(func):
                continue
            sig = inspect.signature(func)
            members["methods"][meth_name] = {
                "params": {p.name: _annotation_text(p.annotation) for p in sig.parameters.values() if p.name != "self"},
                "returns": _annotation_text(sig.return_annotation),
                "property": isinstance(meth, property),
                # 只取自身文档：继承来的 Protocol/内置文档随 Python 版本变化，会让 --check 误报
                "doc": inspect.cleandoc(func.__doc__) if func.__doc__ else None,
            }
        own_doc = obj.__dict__.get("__doc__")
        members["doc"] = inspect.cleandoc(own_doc) if own_doc else None
        result[name] = members
    return {"ports.json": _dump({"contract_version": c.CONTRACT_VERSION, "ports": dict(sorted(result.items()))})}


def generate() -> dict[str, str]:
    files: dict[str, str] = {}
    for builder in (build_schemas, build_day, build_enums, build_events, build_mcp, build_openapi, build_ports):
        files.update(builder())
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验生成物是否最新")
    args = parser.parse_args()
    files = generate()
    stale = []
    for rel, text in files.items():
        path = OUT / rel
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(rel)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    expected = {OUT / rel for rel in files}
    orphans = [p for p in (OUT / "schemas").glob("*.schema.json") if p not in expected]
    if args.check:
        if stale or orphans:
            print("契约生成物过期：", *stale, *[str(p.relative_to(OUT)) for p in orphans], sep="\n  ")
            print("运行 python contracts/build.py 重新生成")
            return 1
        print(f"契约生成物最新（{len(files)} 个文件）")
        return 0
    for p in orphans:
        p.unlink()
    print(f"已生成 {len(stale)} 个变化文件，共 {len(files)} 个；删除过期 {len(orphans)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
