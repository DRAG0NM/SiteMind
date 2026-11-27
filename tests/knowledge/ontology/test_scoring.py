from datetime import datetime, timezone

from plantkg.core.contracts import FrozenSplit
from plantkg.knowledge.rules.scoring import CounterfactualScorer, DayComparison


def test_counterfactual_uses_prior_evening_and_fixed_alternative_costs():
    calls = []
    def compute(day, as_of):
        calls.append((day, as_of))
        return DayComparison("optimal", "optimal", cost_a_under_b=120, cost_b_under_b=100, objective_b=100)
    scorer = CounterfactualScorer(compute)
    split = FrozenSplit(split_id="scoring", purpose="scoring", days=("2026-07-01",),
                        frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content_hash="sha256:" + "0" * 64)
    result = scorer.score(manifest_id="v1", candidate_id="c1", alternative={"value": 10}, probability_b=0.2,
                          split=split, plant_timezone="Asia/Shanghai", mip_rel_gap=0.01)
    assert result.status == "scored" and result.value == 4
    assert calls == [("2026-07-01", datetime(2026, 6, 30, 10, tzinfo=timezone.utc))]
