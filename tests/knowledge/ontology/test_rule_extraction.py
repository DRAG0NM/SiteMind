from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path

from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import Actor, AssetFragment, Context, Locator, OntologyCandidate, Role
from plantkg.experts.equipment.expert import EquipmentExpert
from plantkg.experts.elevator.expert import ElevatorExpert
from plantkg.world.site.model import SiteModel
from plantkg.world.sim.elevator import ElevatorConfig
from plantkg.knowledge.rules import DocumentRuleExtractor
from plantkg.knowledge.rules.compile import approve_rule
from plantkg.knowledge.rules.onboarding import ExceptionAgent, ExtractionTemplates, Source, batch_metrics, route
from plantkg.knowledge.rules.review import next_review_batch, route_candidate


def test_tariff_partial_keeps_source_and_cannot_be_compiled():
    import pytest
    from plantkg.core.contracts import PlantkgError
    root = Path(__file__).resolve().parents[3]
    at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    text = (root / "data/docs/energy/tariff-summary-2025.md").read_text(encoding="utf-8")
    ctx = Context(plant_id="factory", industry_pack="factory", actor=None, channel="offline", trace_id="tariff", as_of=at)
    fragment = AssetFragment(fragment_id="tariff", asset_version_id="asset://factory/12345678/doc/tariff",
                             locator=Locator(line_start=1), kind="text", text=text, available_at=at)
    candidate = DocumentRuleExtractor().extract(ctx, [fragment])[0]
    assert candidate.draft["kind"] == "tariff_tou_partial"
    assert len(candidate.draft["seasonal_periods"]) == 2
    assert candidate.draft["seasonal_periods"][0]["periods"][0]["ranges"] == ["14:00-22:00"]
    assert candidate.draft["ratios"]["peak"]["value"] == 0.8
    assert candidate.draft["ratios"]["deep_valley"]["value"] == -0.2
    assert "19:30-21:30" in candidate.draft["sharp_text"]
    assert "10:00-14:00" in candidate.draft["deep_valley_text"]
    periods = candidate.draft["suggested_periods"]
    assert len(periods) == 8
    assert periods[0]["months"] == [6, 7, 8, 12, 1, 2] and periods[0]["ratio"] == 0.8
    assert periods[6]["windows"] == [["14:00", "15:00"], ["19:30", "21:30"]]
    assert candidate.draft["missing"] and candidate.locator.line_start == 9
    with pytest.raises(PlantkgError):
        approve_rule(candidate, Actor(actor_id="reviewer", roles=(Role.reviewer,)), subject_id="factory",
                     effective_from=at, nature="derived")


def test_compressor_documents_extract_traceable_candidates_without_publishing():
    root = Path(__file__).resolve().parents[3]
    at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ctx = Context(plant_id="factory", industry_pack="factory", actor=None, channel="offline",
                  trace_id="extraction-test", as_of=at)
    fragments = []
    for number, filename in enumerate(("compressor-station-procedure-2024.md", "compressor-maintenance-manual.md")):
        text = (root / "data/docs/equipment" / filename).read_text(encoding="utf-8")
        fragments.append(AssetFragment(fragment_id=f"f{number}", asset_version_id=f"asset://factory/12345678/doc/{number}",
                                       locator=Locator(line_start=1), kind="text", text=text, available_at=at))
    old, new = DocumentRuleExtractor().extract(ctx, fragments)
    assert abs(old.draft["value"] - -1.2) < 1e-9
    assert abs(new.draft["value"] - -3.0) < 1e-9
    assert new.confidence is None
    assert new.locator.line_start == 7
    assert new.source_asset_version == fragments[1].asset_version_id
    assert "持续供气" in new.draft["source_text"]

    options = ("pressure", "temperature", "power")
    question = OntologyCandidate(candidate_id=new.candidate_id, layer="constraint", subject="air-header",
                                 options=options, recall_source="vector")
    result = FakeJudge().decide({"definitions": {key: key for key in options}, "vector_top": "pressure",
                                 "fake_distributions": [{"pressure": 9, "temperature": 1, "power": 1}] * 3},
                                question, options)
    item = route_candidate(new, result, has_template=True, entity_exists=True,
                           standard_aligned=True, conflicts=True)
    assert item.reasons == ("conflicting_rule",)
    assert next_review_batch([item], 1) == [item]
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    templates = ExtractionTemplates()
    first = route(new, result, templates, "compressor-manual", entity_exists=True, standard_aligned=True, conflicts=False)
    assert first.reasons == ("no_template",)
    templates.record("compressor-manual", new, "pressure", reviewer, alias="主管压力")
    second = route(new, result, templates, "compressor-manual", entity_exists=True, standard_aligned=True, conflicts=False)
    assert batch_metrics([second], review_minutes=0.1, tokens=10, elapsed_seconds=1)["agent_fraction"] < \
           batch_metrics([first], review_minutes=1, tokens=20, elapsed_seconds=2)["agent_fraction"]
    conflict = route(new, result, templates, "compressor-manual", entity_exists=True, standard_aligned=True, conflicts=True)
    assert conflict.reasons == ("conflicting_rule",)
    sources = [Source(old.source_asset_version, "2.1", old.draft["source_text"], "2024-06-01T00:00:00Z"),
               Source(new.source_asset_version, "5.1", new.draft["source_text"], "2025-11-01T00:00:00Z")]
    agent = ExceptionAgent(lambda _: '{"decision":"replace","rationale":"新版本","citations":["asset://factory/12345678/doc/1"]}')
    assert agent.investigate(conflict, sources, as_of=at).decision == "pending"
    compiled = approve_rule(new, reviewer, subject_id="entity://factory/air_header/AIR-HDR", effective_from=at,
                            nature="synthetic")
    assert compiled.condition["window_minutes"] == 10
    assert compiled.condition["requires_continuous_supply"] is True

    def diagnose(drop):
        samples = [{"at": at + timedelta(minutes=i), "pressure_bar": 7 - drop * i / 10,
                    "current_a": 5, "oil_c": 65, "on": True} for i in range(11)]
        state = {"ticket_id": "t1", "trigger_at": at + timedelta(minutes=10),
                 "readings": {"samples": samples}, "baseline": {"running_ratio": 1, "current_mean_a": 5},
                 "published_leak_rule": compiled.model_dump(mode="json"), "knowledge": ["compressor_leak_rule"]}
        return EquipmentExpert().propose({"event_type": "compressor_leak", "severity": 2,
                                          "entity_id": "entity://factory/air_compressor/AC-2"}, state)

    below = diagnose(0.4)
    assert "rule:" + compiled.rule_id not in below.evidence_refs
    assert [action.command for action in below.actions] == ["repair"]
    assert "手册规定窗口内压降未超过已发布阈值" in (below.diagnosis or "")
    assert "rule:" + compiled.rule_id in diagnose(0.6).evidence_refs

    elevator_text = (root / "plantkg/knowledge/rules/fixtures/elevator-dispatch.md").read_text(encoding="utf-8")
    elevator_fragment = AssetFragment(fragment_id="elevator", asset_version_id="asset://factory/12345678/doc/elevator",
                                      locator=Locator(line_start=1), kind="text", text=elevator_text, available_at=at)
    elevator = DocumentRuleExtractor().extract(ctx, [elevator_fragment])[0]
    elevator_rule = approve_rule(elevator, reviewer, subject_id="entity://demo-electronics/elevator_group/admin_low",
                                 effective_from=at, nature="synthetic")
    import pytest
    from plantkg.core.contracts import PlantkgError
    bad_elevator = elevator.model_copy(update={"draft": elevator.draft | {"time_range": ["25:00", "26:00"]}})
    with pytest.raises(PlantkgError):
        approve_rule(bad_elevator, reviewer, subject_id="entity://demo-electronics/elevator_group/admin_low",
                     effective_from=at, nature="synthetic")
    assert elevator_rule.bounds.upper == 60 and elevator_rule.condition["queue_threshold"] == 20
    assert elevator_rule.condition["combination"] == "or"
    assert elevator_rule.condition["zones"] == {"low": [1, 6], "high": [7, 12]}
    expert = ElevatorExpert(ElevatorConfig.from_site(SiteModel.load(root / "configs/sites/factory/site.yaml")))
    event = {"event_type": "elevator_congestion", "entity_id": "entity://demo-electronics/elevator_group/admin_low",
             "at": at.replace(hour=0, minute=5), "ticket_id": "elevator-test"}
    state = {"metrics": {"avg_wait_s": 60, "queue": 20}}
    assert expert.propose(event, state).actions
    after = expert.propose(event, state | {"published_elevator_rule": elevator_rule.model_dump(mode="json")})
    assert not after.actions and f"rule:{elevator_rule.rule_id}" in after.evidence_refs
    high_only = expert.propose(event, {"metrics": {"avg_wait_s": 30, "queue": 5, "high_avg_wait_s": 61},
                                       "published_elevator_rule": elevator_rule.model_dump(mode="json")})
    assert high_only.actions and all("admin_high" in action.entity_id for action in high_only.actions)
    high_equal = expert.propose(event, {"metrics": {"avg_wait_s": 30, "queue": 5, "high_avg_wait_s": 60, "high_queue": 20},
                                        "published_elevator_rule": elevator_rule.model_dump(mode="json")})
    assert not high_equal.actions
    incompatible = elevator_rule.model_dump(mode="json")
    incompatible["condition"]["zones"] = {"low": [1, 5], "high": [6, 12]}
    rejected = expert.propose(event, {"metrics": {"avg_wait_s": 90, "queue": 30}, "published_elevator_rule": incompatible})
    assert not rejected.actions and "配置不一致" in rejected.diagnosis
