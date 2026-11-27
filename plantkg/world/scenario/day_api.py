"""Read-only access to explicitly exported day records."""
from pathlib import Path

from plantkg.core.contracts import DayRecord, ErrorCode, PlantkgError

# export-day output is copied to runtime/days/<run_id>.json by the operator.
DAY_DIR = Path(__file__).resolve().parents[3] / "runtime" / "days"


def install(registry):
    def get_day(context, path, query):
        run_id = path["run_id"]
        if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "无效的运行编号")
        file = DAY_DIR / f"{run_id}.json"
        if not file.is_file():
            raise PlantkgError(ErrorCode.NOT_FOUND, "未找到导出的一天")
        day = DayRecord.model_validate_json(file.read_text(encoding="utf-8"))
        if day.run_id != run_id:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"文件与运行编号不一致：文件中的 run_id 为 {day.run_id}，请用该编号命名并加载")
        return day

    registry.register_rest("GET", "/demo/day/{run_id}", get_day)
