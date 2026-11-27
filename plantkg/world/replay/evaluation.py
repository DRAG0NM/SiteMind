"""Internal evaluation-only reader; never registered as an agent tool."""
from plantkg.core.contracts import ErrorCode, PlantkgError


class EvaluationTruth:
    def __init__(self, runs, series):
        self._runs, self._series = runs, series

    def query(self, context, quantity, window):
        if not context.can_write or not context.run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "评测读取需要可信运行上下文")
        if context.plant_id != self._runs.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        run = self._runs.get_run(context.run_id)
        if run.kind not in ("evaluation", "backtest") or run.version_manifest_id != context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "评测运行或清单不匹配")
        allowed = self._runs.run_inputs(run.run_id)["inputs"].get("evaluation_source_ids")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(value, str) for value in allowed):
            raise PlantkgError(ErrorCode.PRECONDITION, "评测来源未固定")
        bundle = self._series.query(context, quantity, window)
        if bundle.source_id not in allowed:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "来源不属于评测输入")
        return bundle
