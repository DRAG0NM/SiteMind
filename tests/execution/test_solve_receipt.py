"""Synthetic B09/B10 evidence, not source verification or approval."""
from dataclasses import replace

import pytest

from plantkg.decision.planner import SharedPlanner
from plantkg.packs.pv import PvPack
from tests.packs.pv.test_pv import CONFIG, SNAP, FakeTariff, bundle_with, cap_rule, prices, request


def test_per_call_receipt_preserves_exact_applied_rule():
    bundles = bundle_with([5.0] * 96)
    rules = [cap_rule(8.0)]
    pv, tariff = PvPack(CONFIG), FakeTariff(prices())
    planner = SharedPlanner({"pv": pv, "tariff": tariff}, lambda _: SNAP, lambda _: bundles,
                            lambda _: tuple(rules))
    req = request(packs=("pv", "tariff"), rule_ids=(rules[0].rule_id,))
    low, low_receipt = planner.solve_with_receipt(req)
    rules[:] = [cap_rule(10.0)]
    high, high_receipt = planner.solve_with_receipt(req)
    assert low.solver_status == high.solver_status == "optimal"
    assert low.plan.actions == high.plan.actions
    low_body, high_body = low_receipt.read(), high_receipt.read()
    assert low_body["rules_by_pack"]["pv"][0]["bounds"]["upper"] == 8.0
    assert high_body["rules_by_pack"]["pv"][0]["bounds"]["upper"] == 10.0
    assert low_body["plan"]["plan_id"] == low.plan.plan_id
    assert high_body["plan"]["plan_id"] == high.plan.plan_id
    assert low_receipt.sha256 != high_receipt.sha256
    with pytest.raises(ValueError, match="digest"):
        replace(low_receipt, canonical_json=high_receipt.canonical_json).read()
