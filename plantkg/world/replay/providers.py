"""Registration of B07's existing contract ports and entry points."""
from datetime import datetime

from plantkg.core.contracts import ErrorCode, Interval, PlantkgError, ToolResult
from plantkg.core.ports import StateProvider, TimeSeriesQuery
from plantkg.world.replay.clock import ReplayClock
from plantkg.world.replay.series import ReplaySeries


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, AttributeError):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要有效的带时区时间") from None


def install(registry, writer, runs, series):
    clock = ReplayClock(writer, runs, registry)
    def initialize_run(con, run):
        if run.forked_from is None:
            con.execute("INSERT INTO replay_clock_history VALUES (?,?,?,?,?)",
                        (run.plant_id, run.run_id, 0, run.clock.start.isoformat(), run.version_manifest_id))

    runs.creation_hook = initialize_run
    query = ReplaySeries(clock, series, runs)
    registry.register(StateProvider, clock)
    registry.register(TimeSeriesQuery, query)

    def ts_query(context, quantity, start, end, cursor=None, page_size=None):
        window = Interval(start=_instant(start), end=_instant(end))
        size = 50 if page_size is None else page_size
        while size >= 1:
            bundle = query.query(context, quantity, window, cursor, page_size=size)
            result = ToolResult(ok=True, business_status="done", data=bundle.model_dump(mode="json"), run_id=context.run_id,
                                version_manifest_id=context.version_manifest_id,
                                page={"page_size": size, "next_cursor": bundle.next_cursor})
            try:
                result.to_agent_text()
                return result
            except PlantkgError as exc:
                if exc.code != ErrorCode.RESULT_TOO_LARGE or size == 1:
                    raise PlantkgError(ErrorCode.RESULT_TOO_LARGE, "单条记录过大，无法通过工具返回") from None
                size //= 2

    def view(context, path, params):
        if "at" not in params:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要 at 参数")
        run = runs.get_run(path["run_id"])
        if run.plant_id != context.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        bound = context.model_copy(update={"run_id": run.run_id, "version_manifest_id": run.version_manifest_id, "as_of": run.clock.start})
        _, _, current = clock.current(bound)
        bound = bound.model_copy(update={"as_of": min(context.as_of, current)})
        return clock.at(bound, _instant(params["at"]))

    registry.register_tool("ts_query", ts_query)
    registry.register_rest("GET", "/runs/{run_id}/view", view)
    return clock, query
