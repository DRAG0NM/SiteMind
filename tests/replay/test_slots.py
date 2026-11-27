from datetime import datetime, timedelta, timezone

import pytest

from plantkg.core.contracts import Interval
from plantkg.world.replay.slots import replay_slots


@pytest.mark.parametrize(("days", "count"), [(1, 96), (7, 672)])
def test_mapped_utc_slots_cover_window_exactly(days, count):
    start = datetime(2025, 3, 24, tzinfo=timezone.utc)
    window = Interval(start=start, end=start + timedelta(days=days))
    slots = replay_slots(window)
    assert len(slots) == count
    assert slots[0].start == start
    assert slots[-1].end == window.end
    assert all(left.end == right.start for left, right in zip(slots, slots[1:]))


def test_partial_or_nonpositive_steps_are_rejected():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    window = Interval(start=start, end=start + timedelta(minutes=16))
    with pytest.raises(ValueError):
        replay_slots(window)
    with pytest.raises(ValueError):
        replay_slots(window, step=timedelta(0))
