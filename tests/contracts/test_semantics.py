"""时间语义、状态机与错误码表的内部一致性。"""

from datetime import datetime, timezone

import pytest

from plantkg.core import contracts as c
from tests.contracts.conftest import load_json, CONTRACTS


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_rule_visibility_and_applicability_are_separate():
    """已发布但未生效的新政策在规划时可见，只作用于生效后的时段（09 D28）。"""
    data = load_json(CONTRACTS / "examples" / "valid" / "rule_tariff_policy.json")["data"]
    rule = c.Rule.model_validate({**data, "available_at": "2026-06-15T00:00:00Z", "effective_from": "2026-07-01T04:00:00Z"})
    as_of = utc(2026, 6, 30, 10)
    assert rule.is_visible(as_of)
    assert not rule.applies_to(utc(2026, 7, 1, 3, 45))
    assert rule.applies_to(utc(2026, 7, 1, 4, 0))
    late = c.Rule.model_validate({**data, "available_at": "2026-07-01T00:00:00Z"})
    assert not late.is_visible(as_of)
    bounded = c.Rule.model_validate({**data, "effective_to": "2026-08-01T00:00:00Z"})
    assert not bounded.applies_to(utc(2026, 8, 1))  # 左闭右开


def test_compilable_only_reviewed_or_trusted():
    data = load_json(CONTRACTS / "examples" / "valid" / "rule_tariff_policy.json")["data"]
    assert c.Rule.model_validate(data).compilable
    pending = c.Rule.model_validate({**data, "review_status": "pending", "reviewer": None})
    auto = c.Rule.model_validate({**data, "review_status": "auto_accepted"})
    assert not pending.compilable and not auto.compilable


def test_interval_left_closed_right_open():
    iv = c.Interval(start=utc(2026, 7, 1), end=utc(2026, 7, 1, 0, 15))
    assert iv.contains(utc(2026, 7, 1)) and not iv.contains(utc(2026, 7, 1, 0, 15))


def test_utc_normalized():
    assert c.Interval.model_validate({"start": "2026-07-01T00:00:00+00:00", "end": "2026-07-01T00:15:00Z"}).start.tzinfo == timezone.utc


@pytest.mark.parametrize("table", [c.PENDING_ACTION_TRANSITIONS, c.JOB_TRANSITIONS, c.MANIFEST_TRANSITIONS, c.CANDIDATE_TRANSITIONS, c.PLAN_TRANSITIONS])
def test_transition_tables_closed(table):
    for targets in table.values():
        assert targets <= set(table)


def test_state_enums_match_transition_tables():
    assert set(c.PENDING_ACTION_TRANSITIONS) == {s.value for s in c.PendingActionStatus}
    assert set(c.JOB_TRANSITIONS) == {s.value for s in c.JobStatus}
    assert set(c.MANIFEST_TRANSITIONS) == {s.value for s in c.ManifestStatus}
    assert set(c.CANDIDATE_TRANSITIONS) == {s.value for s in c.CandidateStatus}
    assert set(c.PLAN_TRANSITIONS) == {s.value for s in c.PlanStatus}


def test_can_transition():
    assert c.can_transition(c.MANIFEST_TRANSITIONS, "retired", "active")
    assert not c.can_transition(c.MANIFEST_TRANSITIONS, "discarded", "active")
    assert not c.can_transition(c.PENDING_ACTION_TRANSITIONS, "confirmed", "rejected")
    assert not c.can_transition(c.JOB_TRANSITIONS, "unknown", "running")


def test_schema_time_pattern_matches_pydantic():
    """标准库消费方用 JSON Schema 时，时间口径与 pydantic 一致（对抗审查发现 pattern 未锚定）。"""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(c.Interval.model_json_schema())
    for bad in ("notatimeZ", "2026-07-01T00:00:00", "2026-07-01T08:00:00+08:00", "2026-07-01T00:00:00z"):
        sample = {"start": bad, "end": "2026-07-01T01:00:00Z"}
        assert list(validator.iter_errors(sample)), bad
        with pytest.raises(ValueError):
            c.Interval.model_validate(sample)


def test_rollback_only_to_previously_active():
    assert c.MANIFEST_TRANSITIONS["retired"] == {"active"}
    assert "active" not in c.MANIFEST_TRANSITIONS["discarded"]


def test_error_codes_mapped():
    assert {e.value for e in c.ErrorCode} == set(c.ERROR_HTTP_STATUS)
    assert all(code.startswith(c.ERROR_PREFIX) for code in c.ERROR_HTTP_STATUS)
    assert c.RETRYABLE_ERRORS <= set(c.ERROR_HTTP_STATUS)


def test_ui_event_payload_table_complete():
    assert set(c.UI_EVENT_PAYLOAD_KEYS) == {t.value for t in c.UIEventType}
    assert set(c.TERMINAL_REASON_BY_EVENT) == c.TERMINAL_UI_EVENTS
    reasons = set().union(*c.TERMINAL_REASON_BY_EVENT.values())
    assert reasons == {r.value for r in c.TerminalReason}


def test_bounds_rejects_bool_but_keeps_int():
    """非 strict 模式下 True 会被收成 1.0：B10 限发、B11 功率上限因此静默变 1 kW（#129）。"""
    assert c.Bounds(upper=1).upper == 1.0
    assert c.Bounds(lower=0, upper=2.5).lower == 0.0
    with pytest.raises(ValueError, match="bool"):
        c.Bounds(upper=True)
    with pytest.raises(ValueError, match="bool"):
        c.Bounds(lower=False)


def test_ledger_tolerance_registered():
    tol = c.LEDGER_TOLERANCE
    assert tol["energy_abs_kwh"] == 1e-3 and tol["energy_rel"] == 1e-6 and tol["soc_abs"] == 1e-6 and tol["power_zero_kw"] == 1e-6


def test_tool_deadline_below_platform_limit():
    assert c.TOOL_DEADLINE_S < 60 and c.JOB_WAIT_MAX_S <= 40
