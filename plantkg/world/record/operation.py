"""Slot projection for explicitly frozen B13 operation profiles."""
import math
from datetime import datetime, timedelta

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.core.ledger import ShiftedOperation, replace_shifted_load


def shift_slot(plan, slot_start, total_kw, config):
    """Use B08's profile replacement; do not read future observations.

    Non-current slots contain only the known original operation profiles for
    the conservation calculation. Their derived totals are never published or
    treated as observations; the actual current total is checked independently.
    """
    n = (plan.valid_interval.end - plan.valid_interval.start) / timedelta(minutes=15)
    index = (slot_start - plan.valid_interval.start) / timedelta(minutes=15)
    if n != int(n) or index != int(index) or not 0 <= index < n:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "工序时段网格不一致")
    n, index = int(n), int(index)
    if not isinstance(config, dict) or config.get("complete") is not True or not isinstance(config.get("items"), list):
        raise PlantkgError(ErrorCode.PRECONDITION, "缺少完整工序依据")
    shifts, expected_entities = [], set()
    base = [0.0] * n
    for item in config["items"]:
        try:
            entity, ident = item["entity_id"], item["operation_id"]
            values = tuple(item["profile_kw"])
            origin = datetime.fromisoformat(item["original_start"])
            original = (origin - plan.valid_interval.start) / timedelta(minutes=15)
            starts = [a for a in plan.actions if a.quantity == "operation_start" and a.object == entity]
            if len(starts) != 1 or starts[0].unit != "count" or starts[0].value != 1:
                raise ValueError("operation start")
            target = (starts[0].interval.start - plan.valid_interval.start) / timedelta(minutes=15)
            if (not values or original != int(original) or target != int(target) or entity in expected_entities
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in values)
                    or int(original) < 0 or int(target) < 0 or int(original) + len(values) > n or int(target) + len(values) > n):
                raise ValueError("profile or interval")
            expected_entities.add(entity)
            powers = [0.0] * n
            for action in plan.actions:
                if action.quantity != "operation_power" or action.object != entity:
                    continue
                pos = (action.interval.start - plan.valid_interval.start) / timedelta(minutes=15)
                if (action.unit != "kW" or pos != int(pos) or not 0 <= pos < n
                        or action.interval.end - action.interval.start != timedelta(minutes=15) or powers[int(pos)]):
                    raise ValueError("operation power")
                powers[int(pos)] = action.value
            expected = [0.0] * n
            for k, value in enumerate(values):
                base[int(original) + k] += value
                expected[int(target) + k] = value
            if any(abs(a - b) > 1e-6 for a, b in zip(powers, expected)):
                raise ValueError("operation plan/profile mismatch")
            shifts.append(ShiftedOperation(ident, values, int(original), int(target)))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, "工序依据与计划不一致") from exc
    for action in plan.actions:
        if action.quantity in ("operation_start", "operation_power") and action.object not in expected_entities:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划工序缺少固定配置")
    if total_kw < base[index] - 1e-6:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "原工序负荷大于当前总表")
    base[index] = total_kw
    report = replace_shifted_load(base, shifts, .25)
    states = {shift.operation_id: {"status": "pending" if index < shift.new_start else
              "running" if index < shift.new_start + len(shift.profile_kw) else "finished",
              "start_index": shift.new_start} for shift in shifts}
    return report.total_kw[index], states
