"""Deterministic UTC replay slots, after source time mapping by adapters."""

from datetime import timedelta

from plantkg.core.contracts import Interval


def replay_slots(window: Interval, *, step: timedelta = timedelta(minutes=15)) -> tuple[Interval, ...]:
    """Partition a mapped UTC window into exact, left-closed replay slots.

    This function does not perform source time-zone or daylight-saving mapping.
    An incomplete final slot is rejected instead of silently dropped.
    """
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    count, remainder = divmod(window.end - window.start, step)
    if remainder:
        raise ValueError("window must contain a whole number of slots")
    return tuple(
        Interval(start=window.start + index * step, end=window.start + (index + 1) * step)
        for index in range(count)
    )
