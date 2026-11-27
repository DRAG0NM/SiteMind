"""将人工复核的候选转为 B01 规则契约，仍须发布后才可使用。"""
from datetime import datetime, time
import re

from plantkg.core.contracts import Actor, ErrorCode, PlantkgError, Role, Rule, RuleCandidate
from plantkg.knowledge.rules.normalise import normalised_kind, normalised_view


def approve_rule(candidate: RuleCandidate, actor: Actor, *, subject_id: str, effective_from: datetime,
                 nature: str, condition: dict | None = None) -> Rule:
    if Role.reviewer not in actor.roles:
        raise PlantkgError(ErrorCode.FORBIDDEN, "规则复核需要复核员角色")
    draft = candidate.draft
    if draft.get("kind") not in ("compressor_pressure_drop", "zone_temperature_limit", "elevator_congestion_rule", "tariff_tou_partial", "workorder_rule_partial"):
        raise PlantkgError(ErrorCode.PRECONDITION, "没有支持此候选的规则编译器")
    details = dict(condition or {})
    details["source_text"] = draft["source_text"]
    if draft["kind"] == "workorder_rule_partial":
        advisory = draft.get("workorder_condition")
        if (not isinstance(advisory, dict) or set(advisory) != {"event_type", "observation", "recommendation"}
                or any(not isinstance(value, str) or not value.strip() for value in advisory.values())
                or not draft.get("workorder_citations")):
            raise PlantkgError(ErrorCode.PRECONDITION, "工单候选须补齐事件、观察条件、处置建议和原工单引用")
        details.update(advisory)
        details["workorder_citations"] = draft["workorder_citations"]
        details["original_candidate_id"] = draft["original_candidate_id"]
        return Rule(rule_id=candidate.candidate_id, kind="maintenance_advisory", subject_ids=(subject_id,),
                    condition=details, quantity="maintenance_signal", unit="1", value=1.0,
                    effective_from=effective_from, available_at=draft["available_at"],
                    source_asset_version=candidate.source_asset_version, locator=candidate.locator, nature=nature,
                    review_status="approved", reviewer=actor.actor_id, compiler_version="b30-1")
    if draft["kind"] == "tariff_tou_partial":
        if not isinstance(draft.get("tariff_condition"), dict) or not draft.get("calendar_refs"):
            raise PlantkgError(ErrorCode.PRECONDITION, "电价候选须由复核员补全时段表和可追溯的节日日历")
        details.update(draft["tariff_condition"])
        details["calendar_refs"] = draft["calendar_refs"]
        return Rule(rule_id=candidate.candidate_id, kind="tariff_tou", subject_ids=(subject_id,),
                    condition=details, quantity="tou_factor", unit="1", value=draft.get("flat_factor", 1.0),
                    effective_from=effective_from, available_at=draft["available_at"],
                    source_asset_version=candidate.source_asset_version, locator=candidate.locator, nature=nature,
                    review_status="approved", reviewer=actor.actor_id, compiler_version="b30-1")
    if draft["kind"] == "compressor_pressure_drop":
        details.update(window_minutes=draft["window_minutes"], comparison=draft["comparison"],
                       requires_continuous_supply="持续供气" in draft["source_text"])
    if draft["kind"] == "elevator_congestion_rule":
        span = draft.get("time_range")
        if (not isinstance(span, list) or len(span) != 2
                or any(not isinstance(item, str) or not re.fullmatch(r"\d{2}:\d{2}", item) for item in span)):
            raise PlantkgError(ErrorCode.PRECONDITION, "电梯分区规则缺少可核对的生效时段")
        try:
            times = [time.fromisoformat(item) for item in span]
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.PRECONDITION, "电梯分区规则时段格式无效") from None
        if any(value.tzinfo is not None or value.second or value.microsecond for value in times) or times[0] >= times[1]:
            raise PlantkgError(ErrorCode.PRECONDITION, "电梯分区规则须给出同一天内有效时段")
        details.update(queue_threshold=draft["queue_threshold"], action=draft["action"], combination="or", comparison="strictly_above")
        details.update(time_range=draft["time_range"], timezone="Asia/Shanghai")
        if draft.get("zones"):
            zones = draft["zones"]
            if not isinstance(zones, dict):
                raise PlantkgError(ErrorCode.PRECONDITION, "电梯楼层分区格式无效")
            low, high = zones.get("low"), zones.get("high")
            if (not isinstance(low, list) or not isinstance(high, list) or len(low) != 2 or len(high) != 2
                    or any(isinstance(n, bool) or not isinstance(n, int) for n in low + high)
                    or not (1 <= low[0] <= low[1] < high[0] <= high[1])):
                raise PlantkgError(ErrorCode.PRECONDITION, "电梯楼层分区无效或重叠")
            details["zones"] = zones
    return Rule(rule_id=candidate.candidate_id, kind=normalised_kind(draft), subject_ids=(subject_id,),
                condition=details,
                quantity=draft["quantity"], unit=draft["unit"], bounds=normalised_view(draft)["bounds"],
                value=draft.get("value"),
                effective_from=effective_from, available_at=draft["available_at"],
                source_asset_version=candidate.source_asset_version, locator=candidate.locator, nature=nature,
                review_status="approved", reviewer=actor.actor_id, compiler_version="b30-1")


def compile_reviewed_rule(store, actions, candidate_id: str, actor: Actor, *, subject_id: str,
                          effective_from: datetime, nature: str) -> Rule:
    """仅消费 B04 已落库的复核决定；拒绝和待确认项不能编译。"""
    decision = store.decision(candidate_id)
    if not decision or decision["decision"] not in ("accept", "revise"):
        raise PlantkgError(ErrorCode.PRECONDITION, "候选还没有通过人工复核")
    action = actions.get(decision["action_id"])
    if action.status != "confirmed" or action.decided_by != actor.actor_id or decision["reviewer"] != actor.actor_id:
        raise PlantkgError(ErrorCode.PRECONDITION, "复核决定尚未确认或复核人不匹配")
    candidate = store.get(candidate_id)
    revision = decision.get("revision")
    if decision["decision"] == "revise":
        if not isinstance(revision, dict) or not revision:
            raise PlantkgError(ErrorCode.PRECONDITION, "修订复核缺少修改内容")
        allowed = {"value", "bounds", "unit", "quantity", "window_minutes", "comparison", "queue_threshold", "action", "time_range",
                   "tariff_condition", "calendar_refs", "flat_factor", "zones", "workorder_condition",
                   "subject_id", "effective_from", "alias", "standard_ref", "standard_locator"}
        if set(revision) - allowed:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "修订含不允许修改的字段")
        candidate = candidate.model_copy(update={"draft": candidate.draft | {k: v for k, v in revision.items()
                                                                               if k not in {"subject_id", "effective_from", "alias", "standard_ref", "standard_locator"}}})
    return approve_rule(candidate, actor, subject_id=subject_id, effective_from=effective_from, nature=nature)
