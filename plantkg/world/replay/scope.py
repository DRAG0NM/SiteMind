"""Reusable trusted run scope for downstream rule and graph consumers."""
from datetime import datetime

from plantkg.core.contracts import Context, ErrorCode, PlantkgError
from plantkg.world.replay.visibility import visible_applicable_rules


class VisibilityContext:
    def __init__(self, clock, context: Context):
        # Validate plant, run, fixed manifest, initialized clock and cutoff.
        clock.current(context)
        self.context = context
        self._inputs = clock.runs.run_inputs(context.run_id)["inputs"]

    def filter(self, rules, slot_start: datetime):
        allowed = self._allowed("online_rule_ids")
        return visible_applicable_rules((rule for rule in rules if rule.rule_id in allowed), as_of=self.context.as_of, slot_start=slot_start)

    def filter_rules(self, rules, slot_start: datetime):
        return self.filter(rules, slot_start)

    def known(self, objects, *, input_key, id_attribute):
        if input_key not in ("online_document_ids", "online_graph_ids"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "不支持该在线引用类型")
        allowed = self._allowed(input_key)
        return tuple(item for item in objects if getattr(item, id_attribute) in allowed and item.available_at <= self.context.as_of)

    def _allowed(self, key):
        values = self._inputs.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线引用清单")
        return frozenset(values)
