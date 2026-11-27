"""登记实际复核批次成本；时间与 token 均由调用方提供实测值。"""
import math
from plantkg.core.contracts import ErrorCode, PlantkgError, Role, ToolResult
from plantkg.core.ports import AssetRepository
from plantkg.knowledge.rules.review import ReviewItem


def install_metrics(registry, candidates, templates):
    assets = registry.resolve(AssetRepository)

    def record(context, batch_id, candidate_ids, review_minutes, tokens, elapsed_seconds):
        if not context.can_write or not context.actor or Role.reviewer not in context.actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "批次指标须由复核员登记")
        if (not isinstance(batch_id, str) or not batch_id or not isinstance(candidate_ids, list)
                or not candidate_ids or not all(isinstance(cid, str) for cid in candidate_ids)
                or len(set(candidate_ids)) != len(candidate_ids)):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "批次编号和候选列表无效")
        if (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0
                       for x in (review_minutes, elapsed_seconds))):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核分钟、token 数与耗时须为非负数")
        items, aligned_count = [], 0
        for candidate_id in candidate_ids:
            candidate = candidates.get(candidate_id)
            assets.get(context, candidate.source_asset_version)
            decision = candidates.decision(candidate_id)
            if decision is None:
                raise PlantkgError(ErrorCode.PRECONDITION, "只能登记已经确认复核的批次")
            route = candidates.route(candidate_id) or {}
            items.append(ReviewItem(candidate, tuple(route.get("reasons", ())), None, 0))
            revision = decision.get("revision") or {}
            standard = revision.get("standard_ref") or route.get("standard_ref")
            locator = revision.get("standard_locator") or route.get("standard_locator")
            if standard and locator and decision["decision"] != "reject":
                assets.get(context, standard)
                aligned_count += 1
        try:
            result = templates.record_batch(batch_id, items, review_minutes=review_minutes,
                                            tokens=tokens, elapsed_seconds=elapsed_seconds, reviewer=context.actor.actor_id,
                                            aligned_count=aligned_count)
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核分钟、token 数与耗时须为非负数") from None
        return ToolResult(ok=True, business_status="done", data={"batch_id": batch_id, **result})

    registry.register_tool("record_onboarding_batch", record)
