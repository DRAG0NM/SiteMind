"""Pure rule filtering for an already-authorized replay read.

Callers must first bind the rules and knowledge cutoff to a trusted run and its
fixed manifest. This helper does not resolve or authorize either of them.
"""

from collections.abc import Iterable
from datetime import datetime

from plantkg.core.contracts import Rule


def visible_applicable_rules(
    rules: Iterable[Rule], *, as_of: datetime, slot_start: datetime
) -> tuple[Rule, ...]:
    """Return approved, known rules applicable at the requested slot start."""
    if as_of.utcoffset() is None or slot_start.utcoffset() is None:
        raise ValueError("as_of and slot_start must be timezone-aware")
    return tuple(
        rule for rule in rules
        if rule.compilable and rule.is_visible(as_of) and rule.applies_to(slot_start)
    )
