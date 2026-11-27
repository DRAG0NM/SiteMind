"""入库流程：先抽取并分流，只为例外项提交后台调查。"""
from plantkg.core.contracts import ErrorCode, PlantkgError, ToolResult
from plantkg.core.versions import digest


def _handler(registry, name: str):
    """取工具处理函数；缺工具时抛依赖不可用，不让 KeyError 漏给调用方（#205）。"""
    try:
        return registry.tools[name].handler
    except KeyError:
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"入库流程依赖的工具 {name} 尚未登记") from None


def install_flow(registry):
    def process(context, asset_version_id):
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "资料入库需要可信写上下文")
        extracted = _handler(registry, "extract_rules")(context, asset_version_id=asset_version_id)
        jobs = []
        for item in extracted.data["routing"]:
            if item["stage"] != "agent":
                continue
            # 幂等键由 investigate_onboarding 按候选 + 知识版本统一生成（#146）
            submitted = _handler(registry, "investigate_onboarding")(context, candidate_id=item["candidate_id"])
            jobs.append({"candidate_id": item["candidate_id"], "job_id": submitted.job_id})
        return ToolResult(ok=True, business_status="done", data={**extracted.data, "investigation_jobs": jobs},
                          notice="例外调查只生成建议；规则仍需人工复核和发布确认")

    registry.register_tool("process_onboarding", process)
