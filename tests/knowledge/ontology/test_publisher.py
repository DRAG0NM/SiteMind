from datetime import datetime, timezone

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import Actor, ComponentRef, Context, Locator, PlantkgError, Role, Rule
from plantkg.core.versions import SqliteVersionStore
from plantkg.core.registry import Registry
from plantkg.experts.hvac.expert import HvacExpert
from plantkg.experts.hvac.install import _knowledge_expert
from plantkg.knowledge.rules.publisher import FallbackPublisher, PublishHandler, RuleArtifacts, candidate_for
from plantkg.knowledge.rules.runtime import rules_for_manifest
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


def test_publish_requires_review_and_confirmation_and_keeps_old_manifest(tmp_path):
    writer = SQLiteWriter(tmp_path / "b30.db")
    try:
        MigrationRunner(writer).apply()
        versions = SqliteVersionStore(writer, "factory")
        versions.register_verifier("initial:", lambda _: "complete")
        reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
        operator = Actor(actor_id="operator", roles=(Role.operator,))
        base = versions.prepare({"rules": ComponentRef(ref="initial:rules", content_hash="sha256:" + "0" * 64)}, "initial")
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, "initial")
        artifacts = RuleArtifacts(writer, "factory")
        versions.register_verifier("b30-rules:", artifacts.verify)
        rule = Rule(rule_id="c1", source_asset_version="asset://factory/12345678/doc/1", locator=Locator(line_start=7),
                    kind="compressor_pressure_drop", subject_ids=("entity://factory/air_header/AIR-HDR",),
                    quantity="pressure_drop_rate", unit="bar/h", value=-3.0, nature="synthetic",
                    effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc), available_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    review_status="approved", reviewer="reviewer", compiler_version="b30-1")
        temperature = Rule(rule_id="c2", source_asset_version="asset://factory/12345678/doc/2", locator=Locator(line_start=1),
                           kind="zone_temperature_limit", subject_ids=("entity://factory/zone/admin",), quantity="temperature",
                           unit="degC", bounds={"lower": 24, "upper": 25}, nature="synthetic",
                           effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc), available_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                           review_status="approved", reviewer="reviewer", compiler_version="b30-1")
        with pytest.raises(PlantkgError):
            artifacts.prepare([rule], operator)
        component = artifacts.prepare([rule, temperature], reviewer)
        handlers = {"publish": PublishHandler(versions, artifacts)}
        actions = SqlitePendingActionStore(writer, "factory", handlers)
        ctx = Context(plant_id="factory", industry_pack="factory", actor=operator, channel="offline",
                      trace_id="b30-test", as_of=datetime.now(timezone.utc))
        publisher = FallbackPublisher(versions, actions, artifacts, ctx)
        candidate = candidate_for(component, base.version_manifest_id, "b30-c1")
        action = publisher.propose_publish(candidate)
        assert versions.active().version_manifest_id == base.version_manifest_id
        registry = Registry()
        registry.register_hook("knowledge.b30.rules", lambda manifest_id, at, as_of: rules_for_manifest(versions, artifacts, manifest_id, at, as_of))
        expert = HvacExpert()
        event = {"event_type": "load_spike", "entity_id": "entity://factory/zone/admin"}
        state = {"at": datetime(2026, 7, 1, tzinfo=timezone.utc), "plant_id": "factory",
                 "zones": {"admin": {"temperature": 24.8, "power_kw": 20}}}
        old, _ = _knowledge_expert(registry, expert, base.version_manifest_id, state["at"])
        assert old.propose(event, state).actions
        actions.confirm(action.action_id, reviewer, "确认规则", 0, "confirm-b30")
        active = versions.active()
        assert active.version_manifest_id != base.version_manifest_id
        assert active.components["rules"] == component
        assert versions.get(base.version_manifest_id).status == "retired"
        assert artifacts.load(component)["rules"] == [r.model_dump(mode="json") for r in (rule, temperature)]
        new, refs = _knowledge_expert(registry, expert, active.version_manifest_id, state["at"])
        assert new.zones["admin"].comfort_c == (24, 25)
        assert not new.propose(event, state).actions
        assert refs == ("rule:c2",)
        before_visible, _ = _knowledge_expert(registry, expert, active.version_manifest_id, state["at"],
                                              datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert before_visible.propose(event, state).actions
        old_again, _ = _knowledge_expert(registry, expert, base.version_manifest_id, state["at"])
        assert old_again.propose(event, state).actions
    finally:
        writer.close()
