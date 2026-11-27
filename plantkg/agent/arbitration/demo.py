"""B28 first-act demo; all scenario inputs and actions are synthetic."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plantkg.agent.arbitration.service import ArbitrationService
from plantkg.core.contracts import (
    DayEvent,
    ExpertProposal,
    Interval,
    PolicyConfig,
    ProposalCost,
    ProposedAction,
    ResponseBudget,
    SimAction,
)
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.bench import TrialBench
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]


def first_act() -> dict[str, object]:
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    bench = TrialBench(lambda: build_world(scenario))
    world = bench.world
    tz = ZoneInfo(scenario.site_tz)
    at = datetime(2026, 7, 1, 8, 5, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    end = at + timedelta(minutes=15)
    event = DayEvent(event_id="b28-demo-peak", at=at, event_type="load_spike", source="script", severity=3,
                     summary="早高峰拥堵与用电突增", nature="synthetic")
    snapshot = bench.snapshot(event)
    interval = Interval(start=at, end=end)
    ids = world.ids()
    proposals = {
        "elevator": (SimAction(entity_id=ids["admin_low"], command="set_mode", params={"mode": "zone"}, interval=interval),),
        "hvac": (SimAction(entity_id=ids["zone:admin"], command="set_mode", params={"mode": "precool"}, interval=interval),),
        "energy": (SimAction(entity_id=ids["battery"], command="discharge", params={"power_kw": 200.0}, interval=interval),),
    }
    policy = PolicyConfig(policy_version="demo-policy", arbitration_weights={"cny": 1.0, "wait_min": 10.0, "comfort_min": 5.0},
                          priority_profiles={"default": ("safety", "hvac", "energy", "elevator")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3))
    service = ArbitrationService(None)
    candidates = tuple(
        ExpertProposal(expert=expert, ticket_id=event.event_id, cost=ProposalCost(cny=0.0), confidence=1.0,
                       actions=tuple(ProposedAction(entity_id=a.entity_id, command=a.command, params=a.params,
                                                    interval=a.interval) for a in actions))
        for expert, actions in proposals.items())
    selected, conversion = service.arbitrate(candidates, policy=policy, snapshot=snapshot, trial_runner=bench.trial)
    chosen = "+".join(p.proposal_id or p.expert for p in selected)
    return {"event": event, "scores": conversion.scores, "chosen": chosen, "score": conversion.scores[chosen],
            "nature": "synthetic"}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m plantkg.agent.arbitration.demo")
    parser.add_argument("scenario", choices=("第一幕剧本",))
    parser.parse_args()
    result = first_act()
    print(f"冲突：{result['event'].summary}；候选为电梯分区、暖通预冷、储能削峰")
    print("换算：")
    for candidate, score in sorted(result["scores"].items()):
        print(f"  {candidate}: {score:.2f} 元等值")
    print(f"裁决结果：选择 {result['chosen']}（{result['score']:.2f} 元等值，数据性质：{result['nature']}）")


if __name__ == "__main__":
    main()
