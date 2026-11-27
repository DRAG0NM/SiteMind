"""Real B09/B10 solver reproduction of the missing rule-value prerequisite."""
from tests.packs.pv.test_pv import cap_rule, series, solve
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger, validate_balance
from plantkg.world.record.pv import apply_pv_cap
from tests.packs.pv.test_pv import CTX, H0, PV_ID, SLOTS
from datetime import timedelta
import pytest
from plantkg.core.contracts import PlantkgError


def test_zero_planned_limit_does_not_identify_actual_cap():
    low, _ = solve([5.0] * 96, rules=[cap_rule(8.0)])
    high, _ = solve([5.0] * 96, rules=[cap_rule(10.0)])
    assert low.solver_status == high.solver_status == "optimal"
    assert series(low.plan, "pv_limit") == series(high.plan, "pv_limit") == [0.0] * 96
    # B09 serializes only nonzero actions; neither plan includes a cap value.
    assert low.plan.actions == high.plan.actions
    # If actual PV is 9 kW, limits must be 1 and 0 kW respectively.
    assert max(9.0 - 8.0, 0.0) != max(9.0 - 10.0, 0.0)


def test_approved_rule_values_give_distinct_balanced_results():
    for cap, expected in ((8.0, 1.0), (10.0, 0.0)):
        limited = apply_pv_cap(9.0, PV_ID, H0, (cap_rule(cap),), complete=True)
        assert limited.limit_kw == expected
        meta = LedgerMeta("execution-test", "execution", CTX.run_id, "synthetic-plan", 1,
                          CTX.version_manifest_id, ("test://synthetic",))
        ledger = compute_ledger([SlotInput(interval=SLOTS[0], pv_available_kw=limited.usable_kw,
                                           load_kw=10.0, price_cny_per_kwh=1.0)], meta, None).ledger
        assert ledger.balanced and validate_balance(ledger) == []
        assert ledger.entries[0].pv_curtailed_kwh == 0.0


def test_caps_respect_entity_effective_time_and_review_status():
    first = cap_rule(8, "cap-8")
    second = cap_rule(6, "cap-6", effective_from=H0 + timedelta(minutes=15))
    unrelated = cap_rule(1, "other-pv", subject_ids=("entity://demo-electronics/pv/other",))
    assert apply_pv_cap(9, PV_ID, H0, (first, second, unrelated), complete=True).limit_kw == 1
    assert apply_pv_cap(9, PV_ID, H0 + timedelta(minutes=15), (first, second, unrelated), complete=True).limit_kw == 3
    with pytest.raises(PlantkgError):
        apply_pv_cap(9, PV_ID, H0, (first,), complete=False)
    with pytest.raises(PlantkgError):
        apply_pv_cap(9, PV_ID, H0, (first.model_copy(update={"review_status": "pending"}),), complete=True)
