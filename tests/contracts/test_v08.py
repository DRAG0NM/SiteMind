"""v0.8 契约前置（B46）：“一天”样例、新旧契约并存、运行模式分派与启动自检、账本负荷分项。"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from plantkg.core import contracts as c
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger
from plantkg.core.ports import AgentResultSource, ForkHook, KnowledgePublisher, ProposalSink, TrialRunner
from plantkg.core.registry import Registry, RegistryError
from tests.contracts.conftest import CONTRACTS, ROOT, load_json

DAY = CONTRACTS / "examples" / "day.sample.json"
KINDS = tuple(k.value for k in c.AgentResultKind)


def test_day_sample_passes_schema_and_contract():
    data = load_json(DAY)
    validator = Draft202012Validator(load_json(CONTRACTS / "day.schema.json"), format_checker=FormatChecker())
    assert not [e.message for e in validator.iter_errors(data)]
    day = c.DayRecord.model_validate(data)
    assert day.run_id == "run-scn-20260701-f1" and day.runs[1].forked_at is not None
    proposal = day.proposals[0]
    assert proposal.timing.latency_ms == 12400 and proposal.timing.effective_at > proposal.timing.trigger_at
    assert len(day.state_series) == 2 and day.agent_runs[0].timing.latency_ms
    assert all(sum(e.load_breakdown.values()) == pytest.approx(e.load_kwh) for e in day.ledger_minutes)


def test_validate_command():
    out = subprocess.run([sys.executable, "-m", "plantkg.contracts.validate", str(DAY)], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ})
    assert out.returncode == 0, out.stdout + out.stderr


def test_day_rejects_dangling_ticket_and_unsaved_proposal():
    data = load_json(DAY)
    data["proposals"][0]["ticket_id"] = data["proposals"][0]["binding"]["ticket_id"] = "ticket://missing"
    with pytest.raises(ValueError, match="事件单"):
        c.DayRecord.model_validate(data)
    data = load_json(DAY)
    data["proposals"][0]["proposal_id"] = None
    with pytest.raises(ValueError, match="proposal_id"):
        c.DayRecord.model_validate(data)


def test_old_records_still_load_with_new_defaults():
    old = load_json(CONTRACTS / "examples" / "valid" / "run_info.json")["data"]
    run = c.RunInfo.model_validate(old)
    assert run.forked_at is None and run.energy_end_target_kwh is None and run.data_sources == ()
    ctx = c.Context.model_validate(load_json(CONTRACTS / "examples" / "valid" / "context_gateway.json")["data"])
    assert ctx.run_mode == "live"
    entry = c.LedgerEntry.model_validate(load_json(CONTRACTS / "examples" / "valid" / "ledger_entry.json")["data"])
    assert entry.load_breakdown is None


# ---- 运行模式分派


class Source:
    def __init__(self, name):
        self.name = name

    def get(self, kind, binding, payload):
        return {"by": self.name}


def test_live_and_trial_resolve_independently():
    reg = Registry()
    live, trial = Source("live"), Source("trial")
    reg.register_mode(AgentResultSource, "live", live, key="expert")
    reg.register_mode(AgentResultSource, "trial", trial, key="expert")
    assert [reg.resolve_mode(AgentResultSource, m, "expert") for m in ("live", "trial", "live")] == [live, trial, live]
    with pytest.raises(RegistryError, match="重复登记"):
        reg.register_mode(AgentResultSource, "live", Source("again"), key="expert")
    with pytest.raises(RegistryError, match="未知登记模式"):
        reg.register_mode(AgentResultSource, "shadow", live, key="expert")
    with pytest.raises(RegistryError, match="没有实现端口"):
        reg.register_mode(AgentResultSource, "live", object(), key="triage")


def test_trial_and_evaluation_fall_back_to_deterministic_but_live_does_not():
    reg = Registry()
    det = Source("det")
    reg.register_mode(AgentResultSource, "deterministic", det, key="arbitration")
    assert reg.resolve_mode(AgentResultSource, "trial", "arbitration") is det
    assert reg.resolve_mode(AgentResultSource, "evaluation", "arbitration") is det
    with pytest.raises(c.PlantkgError):
        reg.resolve_mode(AgentResultSource, "live", "arbitration")
    reg.live_fallback = True  # dev/mock
    assert reg.resolve_mode(AgentResultSource, "live", "arbitration") is det


def test_profile_self_check():
    need = {AgentResultSource: KINDS}
    reg = Registry()
    for k in KINDS:
        reg.register_mode(AgentResultSource, "deterministic", Source(f"det-{k}"), key=k)  # B27/B28/B29
    reg.check_modes("dev", need)
    reg.check_modes("evaluation", need)  # 只有确定性实现也能启动
    with pytest.raises(RegistryError, match="live"):
        reg.check_modes("production", need)
    for k in KINDS:
        reg.register_mode(AgentResultSource, "trial", Source(f"cache-{k}"), key=k)  # B33 缓存
        reg.register_mode(AgentResultSource, "evaluation", Source(f"cache-{k}"), key=k)
    reg.check_modes("evaluation", need)
    for k in KINDS:
        reg.register_mode(AgentResultSource, "live", Source(f"nexent-{k}"), key=k)  # B23
    reg.check_modes("production", need)
    reg.check_modes("prod", need)


def test_hooks_keep_registration_order():
    reg = Registry()
    calls = []
    reg.register_hook("fork", lambda *a: calls.append("tickets"))
    reg.register_hook("fork", lambda *a: calls.append("temp_rules"))
    for fn in reg.hooks_for("fork"):
        fn()
    assert calls == ["tickets", "temp_rules"] and reg.hooks_for("step") == ()


def test_new_ports_are_runtime_protocols():
    class Sink:
        def save(self, proposal, binding): ...
        def attach_reply(self, proposal_id, reply): ...
        def get(self, proposal_id): ...
        def list(self, ticket_id, round_=None): ...

    assert isinstance(Sink(), ProposalSink) and not isinstance(Sink(), TrialRunner)
    for port in (KnowledgePublisher, AgentResultSource, TrialRunner, ForkHook):
        assert getattr(port, "_is_runtime_protocol", False)


# ---- B08 账本负荷分项


def _slots(breakdown):
    start = datetime(2026, 7, 1, 0, 6, tzinfo=timezone.utc)
    return [SlotInput(interval=c.Interval(start=start, end=start + timedelta(minutes=1)), pv_available_kw=120.0, load_kw=900.0,
                      price_cny_per_kwh=0.95, load_breakdown_kw=breakdown)]


META = LedgerMeta("led-1", "execution", "run-1", "plan-1", 0, "vm-1", ("bundle-1",))


def test_ledger_accepts_load_breakdown():
    result = compute_ledger(_slots({"elevator": 90.0, "hvac": 240.0, "air_compressor": 180.0, "lighting": 60.0, "process": 270.0, "base": 60.0}), META, None)
    entry = result.ledger.entries[0]
    assert result.ledger.balanced and entry.load_breakdown["hvac"] == pytest.approx(4.0)
    assert sum(entry.load_breakdown.values()) == pytest.approx(entry.load_kwh)


@pytest.mark.parametrize("breakdown", [{"hvac": 100.0}, {"hvac": 900.0, "kitchen": 0.0}, {"hvac": 1000.0, "base": -100.0}])
def test_ledger_flags_bad_breakdown(breakdown):
    result = compute_ledger(_slots(breakdown), META, None)
    assert not result.ledger.balanced and result.ledger.entries[0].load_breakdown is None
    assert any("负荷分项" in v for v in result.ledger.violations)


def test_generated_day_schema_matches_model():
    assert json.loads((CONTRACTS / "day.schema.json").read_text(encoding="utf-8"))["title"] == "DayRecord"
