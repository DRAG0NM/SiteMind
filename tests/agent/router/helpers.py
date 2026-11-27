"""B27 值守层的测试夹具与合成数据（synthetic）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import (
    Actor, Context, DayEvent, ExpertProposal, Interval, ProposalCost, ProposedAction, RunBinding, RunInfo,
)

PLANT = "p1"
T0 = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
AIR = "entity://p1/air_system/AC-2"
LIGHT = "entity://p1/lighting/LT-A03"
TRANSFORMER = "entity://p1/transformer/TX1"

# 测试用的严重程度参数：默认不停产、金额线高，严重程度行为单独用其它配置测
SEVERITY = {"horizon_min": 60, "major_loss_cny": 50_000, "line_value_cny_per_min": 180, "stop_minutes": 30,
            "production_lines": ["LINE-1", "LINE-2", "LINE-3"], "stop_event_types": [], "safety_event_types": [],
            "rule_covered_events": [], "equipment_damage_cny": {"default": 0}}


def make_event(event_id: str, minutes: int, event_type: str, entity_id: str | None = AIR, *, severity: int = 3,
               source: str = "script", summary: str | None = None, nature: str = "synthetic") -> DayEvent:
    return DayEvent(event_id=event_id, at=T0 + timedelta(minutes=minutes), event_type=event_type, source=source,
                    entity_id=entity_id, severity=severity, summary=summary or event_type, payload={}, nature=nature)


def make_proposal(expert: str, ticket_id: str, *, confidence: float = 0.8, diagnosis: str | None = None,
                  evidence: tuple[str, ...] = ()) -> ExpertProposal:
    action = ProposedAction(entity_id=AIR, command="isolate_leak", params={},
                            interval=Interval(start=T0, end=T0 + timedelta(minutes=1)))
    return ExpertProposal(expert=expert, ticket_id=ticket_id, actions=(action,), cost=ProposalCost(cny=-3.0),
                          confidence=confidence, diagnosis=diagnosis or f"{expert} 合成诊断", evidence_refs=evidence)


def binding(ticket_id: str, *, round_: int = 1, run_id: str = "run-1", knowledge_version: int = 3) -> RunBinding:
    return RunBinding(run_id=run_id, knowledge_version=knowledge_version, ticket_id=ticket_id, round=round_,
                      state_version=7, trigger_at=T0)


def context(plant_id: str = PLANT) -> Context:
    return Context(plant_id=plant_id, industry_pack="manufacturing", actor=Actor(actor_id="duty", roles=("operator",)),
                   channel="offline", trace_id="t", as_of=T0, run_id="run-1")


def run_info(run_id: str) -> RunInfo:
    return RunInfo(run_id=run_id, plant_id=PLANT, kind="scenario", version_manifest_id="vm-1",
                   clock=Interval(start=T0, end=T0 + timedelta(hours=1)), as_of=T0, status="running", created_at=T0)


class CountingJudge:
    """包一层 FakeJudge，数调用次数，用来验证直通事件不调用 Jev。"""

    backend = "fake"
    model = "counting"

    def __init__(self) -> None:
        self.inner = FakeJudge()
        self.calls = 0

    def decide(self, state, question, options):
        self.calls += 1
        return self.inner.decide(state, question, options)


class CountingTriage:
    """AgentResultSource（deterministic，键 triage）的计数替身。"""

    name = "triage"

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[dict] = []

    def get(self, kind, binding, payload):
        self.calls += 1
        self.payloads.append(dict(payload))
        return {"status": "finished", "agent_run_id": None, "final_answer": "计数用研判", "weights": {}}
