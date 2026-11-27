"""规划规则的反事实复核得分；求解和统一账本由调用方注入。"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from plantkg.core.contracts import FrozenSplit
from plantkg.core.versions import canonical


@dataclass(frozen=True)
class DayComparison:
    """A/B 均在同一评分日求解，两个成本均用备选 B 的规则与同一账本计算。"""
    status_a: str
    status_b: str
    cost_a_under_b: float
    cost_b_under_b: float
    objective_b: float
    violates_b: bool = False


@dataclass(frozen=True)
class ReviewScore:
    status: str  # scored / violation / incomparable / uncalibrated
    value: float | None
    days: tuple[str, ...]


class CounterfactualScorer:
    """compute 接收 (评分日, D-1 18:00 UTC)，必须使用预测输入，不写账本或运行。"""

    def __init__(self, compute: Callable[[str, datetime], DayComparison]):
        self.compute = compute
        self.cache: dict[str, ReviewScore] = {}

    def score(self, *, manifest_id: str, candidate_id: str, alternative: dict, probability_b: float | None,
              split: FrozenSplit, plant_timezone: str, mip_rel_gap: float) -> ReviewScore:
        if split.purpose != "scoring" or not 0 <= mip_rel_gap < 1:
            raise ValueError("须使用冻结的 scoring 划分与合法求解容差")
        if probability_b is None:
            return ReviewScore("uncalibrated", None, split.days)
        if not 0 <= probability_b <= 1:
            raise ValueError("备选解释概率须在 0～1")
        key = hashlib.sha256(canonical([manifest_id, candidate_id, alternative, probability_b,
                                       split.model_dump(mode="json"), plant_timezone, mip_rel_gap]).encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        zone, regrets = ZoneInfo(plant_timezone), []
        violation = False
        for day in split.days:
            local_day = date.fromisoformat(day)
            as_of = datetime.combine(local_day - timedelta(days=1), time(18), tzinfo=zone).astimezone(timezone.utc)
            result = self.compute(day, as_of)
            if result.status_a != "optimal" or result.status_b != "optimal":
                score = ReviewScore("incomparable", None, split.days)
                break
            if not all(math.isfinite(v) for v in (result.cost_a_under_b, result.cost_b_under_b, result.objective_b)):
                score = ReviewScore("incomparable", None, split.days)
                break
            violation |= result.violates_b
            regret = result.cost_a_under_b - result.cost_b_under_b
            regrets.append(0.0 if regret < mip_rel_gap * abs(result.objective_b) else regret)
        else:
            score = (ReviewScore("violation", None, split.days) if violation else
                     ReviewScore("scored", probability_b * sum(regrets) / len(regrets), split.days))
        self.cache[key] = score
        return score
