"""Read-only access to committed, visible execution ledgers."""
import base64
import binascii
import hashlib
import json

from plantkg.core.contracts import ErrorCode, ExecutionResult, Ledger, Page, PlantkgError, ToolResult
from plantkg.core.ledger import ledger_summary


PLAN_LEDGER_HOOK = "plan_ledger"  # 领域包登记 fn(writer, context, run_id, cursor, page_size) -> Page，提供计划账本


def install(registry, writer, clock, runs):
    def query(context, run_id, kind, cursor, page_size, *, public=False):
        if kind not in ("plan", "execution"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "kind 须为 plan 或 execution")
        if isinstance(page_size, bool):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "page_size 须在 1～50")
        try:
            size = 50 if page_size is None else int(page_size)
            if str(size) != str(page_size) and page_size is not None:
                raise ValueError()
            if not 1 <= size <= 50:
                raise ValueError()
        except (ValueError, TypeError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "page_size 须在 1～50") from None
        if not public and (context.run_id != run_id or not context.version_manifest_id):
            raise PlantkgError(ErrorCode.PRECONDITION, "会话必须绑定所查询的运行及清单")
        run = runs.get_run(run_id)
        if run.plant_id != context.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        if not public and context.version_manifest_id != run.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "运行清单不匹配")
        bound = context.model_copy(update={"run_id": run_id, "version_manifest_id": run.version_manifest_id,
                                           "as_of": run.clock.start})
        with writer.read_only() as con:
            fixed_run = clock._run(con, bound)
            _, current = clock._latest(con, fixed_run)
            cutoff = min(context.as_of, current) if public else context.as_of
            if cutoff > current:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "知识截止晚于回放游标")
            if kind == "plan":
                providers = registry.hooks_for(PLAN_LEDGER_HOOK)
                if not providers:
                    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "计划账本提供者尚未接入")
                return providers[-1](writer, bound, run_id, cursor, size), size
            key = json.dumps([run.plant_id, run_id, run.version_manifest_id, kind, cutoff.isoformat(),
                              "seq_after_asc_v1"], separators=(",", ":"))
            binding = hashlib.sha256(key.encode()).hexdigest()
            after = 0
            if cursor is not None:
                try:
                    payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                    after = payload["after"]
                    if payload["binding"] != binding or type(after) is not int or after < 0:
                        raise ValueError()
                except (ValueError, TypeError, KeyError, binascii.Error):
                    raise PlantkgError(ErrorCode.INVALID_REQUEST, "分页游标无效或与查询不匹配") from None
            rows = con.execute(
                "SELECT seq_after,result FROM b16_executions WHERE plant_id=? AND run_id=? AND manifest_id=? "
                "AND seq_after>? AND visible_after<=? ORDER BY seq_after LIMIT ?",
                (run.plant_id, run_id, run.version_manifest_id, after, cutoff.isoformat(), size + 1)).fetchall()
        items = tuple(ExecutionResult.model_validate_json(row[1]).ledger for row in rows[:size])
        next_cursor = None
        if len(rows) > size:
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                {"binding": binding, "after": rows[size - 1][0]}, separators=(",", ":")).encode()).decode()
        return Page[Ledger](items=items, next_cursor=next_cursor, as_of=cutoff,
                            version_manifest_id=run.version_manifest_id), size

    def ledger(context, run_id, kind, cursor=None, page_size=None):
        size = 50 if page_size is None else page_size
        while True:
            page, size = query(context, run_id, kind, cursor, size)
            result = ToolResult(ok=True, business_status="done", data={"items": [ledger_summary(item) for item in page.items]},
                                run_id=run_id, version_manifest_id=page.version_manifest_id,
                                page={"page_size": size, "next_cursor": page.next_cursor})
            try:
                result.to_agent_text()
                return result
            except PlantkgError as exc:
                if exc.code != ErrorCode.RESULT_TOO_LARGE or size == 1:
                    raise
                size //= 2

    def rest(context, path, params):
        return query(context, path["run_id"], params.get("kind"), params.get("cursor"),
                     params.get("page_size"), public=True)[0]

    registry.register_tool("ledger", ledger)
    registry.register_rest("GET", "/runs/{run_id}/ledger", rest)
