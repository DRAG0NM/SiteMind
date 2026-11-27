"""Pure execution-side PV cap selection over already verified rule material.

The caller must establish the complete rule set and its approval binding. This
module does not verify provenance, persist facts, or authorize execution.
"""
import math
from dataclasses import dataclass
from datetime import datetime

from plantkg.core.contracts import ErrorCode, PlantkgError, Rule


@dataclass(frozen=True)
class PvLimit:
    cap_kw: float | None
    limit_kw: float
    usable_kw: float
    rule_ids: tuple[str, ...]


def apply_pv_cap(raw_kw: float, entity_id: str, instant: datetime, rules: tuple[Rule, ...], *, complete: bool) -> PvLimit:
    """Use actual generation and the minimum applicable, already verified cap."""
    if complete is not True:
        raise PlantkgError(ErrorCode.PRECONDITION, "缺少完整规则枚举证明")
    if isinstance(raw_kw, bool) or not isinstance(raw_kw, (int, float)) or not math.isfinite(raw_kw) or raw_kw < 0:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "实际光伏必须是非负有限功率")
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要带时区的槽起点")
    if len({r.rule_id for r in rules}) != len(rules):
        raise PlantkgError(ErrorCode.PRECONDITION, "规则标识重复")
    applicable = []
    for rule in rules:
        if rule.kind != "pv_output_cap" or entity_id not in rule.subject_ids or not rule.applies_to(instant):
            continue
        upper = rule.bounds.upper if rule.bounds else None
        if (not rule.compilable or rule.quantity != "pv_output" or rule.unit != "kW"
                or rule.subject_ids != (entity_id,) or upper is None or not math.isfinite(upper) or upper < 0
                or rule.bounds.lower is not None):
            raise PlantkgError(ErrorCode.PRECONDITION, "光伏限发规则无效或未审核")
        applicable.append((upper, rule.rule_id))
    if not applicable:
        return PvLimit(None, 0.0, float(raw_kw), ())
    cap = min(upper for upper, _ in applicable)
    limited = max(float(raw_kw) - cap, 0.0)
    return PvLimit(cap, limited, float(raw_kw) - limited, tuple(sorted(rule_id for _, rule_id in applicable)))
