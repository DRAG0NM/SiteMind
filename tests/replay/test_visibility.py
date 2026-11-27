from datetime import datetime, timedelta, timezone

import pytest

from plantkg.core.contracts import Rule
from plantkg.world.replay import visible_applicable_rules


T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def make_rule(**changes):
    values = dict(
        rule_id="rule-1", kind="tariff_period", subject_ids=("entity://plant/device/1",),
        quantity="price", unit="CNY/kWh", value=1,
        effective_from=T0 + timedelta(hours=1), effective_to=T0 + timedelta(hours=2),
        available_at=T0, config_ref="config://rule", nature="configured",
        review_status="trusted_config",
    )
    values.update(changes)
    return Rule(**values)


def test_known_future_rule_applies_only_inside_effective_interval():
    rule = make_rule()
    assert not visible_applicable_rules([rule], as_of=T0, slot_start=T0)
    assert visible_applicable_rules([rule], as_of=T0, slot_start=T0 + timedelta(hours=1)) == (rule,)
    assert not visible_applicable_rules([rule], as_of=T0, slot_start=T0 + timedelta(hours=2))


def test_unavailable_or_unreviewed_rule_is_excluded():
    future = make_rule(available_at=T0 + timedelta(seconds=1))
    pending = make_rule(review_status="pending")
    assert not visible_applicable_rules([future, pending], as_of=T0, slot_start=T0 + timedelta(hours=1))


def test_naive_times_are_rejected():
    with pytest.raises(ValueError):
        visible_applicable_rules([], as_of=T0.replace(tzinfo=None), slot_start=T0)
