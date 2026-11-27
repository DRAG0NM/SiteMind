import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from plantkg.core.config import load_settings
from plantkg.core.contracts import DayEvent, ExpertProposal, Interval, ProposalCost
from plantkg.core.ports import AgentResultSource, StateProvider, TimeSeriesQuery
from plantkg.bootstrap import build_services
from plantkg.agent.consultation.preview import PREVIEW_SIGNALS_HOOK, PREVIEW_STATE_HOOK, PreviewSignal
from plantkg.agent.consultation.consilium import _replay_plan_signals
from plantkg.agent.router import ROUTER_HOOK
from tests.versions.conftest import AS_OF, context
from tests.versions.test_versions import publish


def test_replay_preview_does_not_invent_missing_state():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        run = services.version_run.runs.create_run(
            context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id),
            "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
        ctx = context(plant_id=settings.plant_id, run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)
        clock = services.registry.resolve(StateProvider)
        instant = AS_OF.replace(hour=23, minute=45)
        clock.advance(ctx, 0, instant + timedelta(minutes=15))

        consultation = services.registry.hooks_for("consultation.service")[-1]
        router = next(router for router, _ in consultation._bindings if router.run_id == run.run_id)
        state_provider = services.registry.hooks_for(PREVIEW_STATE_HOOK)[-1]
        preview_state = state_provider(router, instant + timedelta(minutes=15))
        assert preview_state == {}
        with services.storage.writer.read_only() as con:
            tickets = con.execute("SELECT count(*) FROM b27_tickets WHERE plant_id=? AND run_id=?",
                                  (settings.plant_id, run.run_id)).fetchone()[0]
        assert tickets == 0
    finally:
        services.close()


def test_replay_advances_bind_isolated_router_per_run():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        clock = services.registry.resolve(StateProvider)
        default_router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        run_ids = []
        for _ in range(2):
            run = services.version_run.runs.create_run(
                context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id),
                "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
            run_ids.append(run.run_id)
            ctx = context(plant_id=settings.plant_id, run_id=run.run_id,
                          version_manifest_id=manifest.version_manifest_id)
            handled = default_router.handle_event(
                DayEvent(event_id=f"EV-{run.run_id}", at=AS_OF, event_type="operator_instruction", source="script",
                         severity=3, summary="replay start", nature="synthetic"),
                run_id=run.run_id,
            )
            assert handled.result == "instruction_unhandled"
            replay_router = default_router._run_router(run.run_id)
            assert replay_router.store.event(f"EV-{run.run_id}") is not None
            clock.advance(ctx, 0, AS_OF + timedelta(minutes=15))

        consultation = services.registry.hooks_for("consultation.service")[-1]
        bound = [router for router, runner in consultation._bindings if router.run_id in run_ids]
        assert {router.run_id for router in bound} == set(run_ids)
        assert len({id(router) for router in bound}) == 2
        assert {router.store.plant_id for router in bound} == {f"{settings.plant_id}#{run_id}" for run_id in run_ids}
        assert len({id(router.runs) for router in bound}) == 2
    finally:
        services.close()


def test_approved_operation_and_peak_price_slots_create_replay_signals():
    start = AS_OF + timedelta(minutes=15)
    action = SimpleNamespace(action_id="start-op", quantity="operation_start", object="entity://p1/operation/O1",
                             interval=Interval(start=start, end=start + timedelta(minutes=15)))
    plan = SimpleNamespace(actions=(action,))
    config = {"operations": {"items": [{"operation_id": "O1", "entity_id": action.object}]},
              "tariff": {"slots": [
                  {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(), "price_cny_per_kwh": 1.0},
                  {"start": (start + timedelta(minutes=15)).isoformat(),
                   "end": (start + timedelta(minutes=30)).isoformat(), "price_cny_per_kwh": 2.0}]}}

    signals = _replay_plan_signals([{"plan": plan, "config": config}], AS_OF, 30)

    assert {signal.kind for signal in signals} == {"plan_start", "peak_start"}
    operation = next(signal for signal in signals if signal.kind == "plan_start")
    assert operation.at == start and operation.payload["operation_id"] == "O1"


def test_replay_preview_uses_fixed_soc_and_tx1_measurement():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        run = services.version_run.runs.create_run(
            context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id),
            "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
        ctx = context(plant_id=settings.plant_id, run_id=run.run_id,
                      version_manifest_id=manifest.version_manifest_id)
        clock = services.registry.resolve(StateProvider)
        at = AS_OF + timedelta(minutes=15)
        clock.advance(ctx, 0, at)
        consultation = services.registry.hooks_for("consultation.service")[-1]
        router = next(router for router, _ in consultation._bindings if router.run_id == run.run_id)
        preview_state = services.registry.hooks_for(PREVIEW_STATE_HOOK)[-1]

        class SnapshotProvider:
            pack_states = {"storage": {"energy_kwh": 20, "capacity_kwh": 100}}

            def at(self, context, instant):
                return self

        measurement = SimpleNamespace(value=900, available_at=at, event_time=at,
                                      locator=SimpleNamespace(meter="transformer_load:TX1"),
                                      transform_steps=(SimpleNamespace(params={"limit_kw": 1000}),))
        unrelated = SimpleNamespace(value=10_000, available_at=at, event_time=at,
                                    locator=SimpleNamespace(meter="transformer_load:TX2"),
                                    transform_steps=(SimpleNamespace(params={"limit_kw": 20_000}),))

        class Series:
            def query(self, context, quantity, window):
                assert quantity == "active_power"
                return SimpleNamespace(records=(unrelated, measurement))

        services.registry.ports[StateProvider] = SnapshotProvider()
        services.registry.ports[TimeSeriesQuery] = Series()
        observed = preview_state(router, at)
        assert observed == {"soc": 0.2, "transformer_ratio": 0.9, "nature": "derived"}
        preview = consultation._previews[id(router)]
        assert preview._triggered(observed, router.policy)

        SnapshotProvider.pack_states = {"storage": {"energy_kwh": 80, "capacity_kwh": 100}}
        measurement.value = 400
        calm = preview_state(router, at)
        assert calm["soc"] == 0.8 and calm["transformer_ratio"] == 0.4
        assert not preview._triggered(calm, router.policy)
    finally:
        services.close()


def test_replay_preview_uses_b18_transformer_meter_through_replay_series():
    from plantkg.agent.consultation.preview import PREVIEW_STATE_HOOK
    from plantkg.core.contracts import Actor
    from plantkg.core.ports import TimeSeriesQuery
    from plantkg.world.site import SiteModel
    from plantkg.world.site.loads import synthesize
    from plantkg.world.site.sources import d05
    from plantkg.world.replay.series import online_record_hash

    root = Path(__file__).resolve().parents[2]
    site = SiteModel.load(root / "configs/sites/factory/site.yaml")
    data = d05.load_d05(root / "fixtures/sites/factory/d05_week.parquet")
    bundle = synthesize(site, data, 1).to_bundle()
    records = [record for record in bundle.records if record.locator.meter == "transformer_load:TX1"]
    assert records and all(record.quantity == "active_power" for record in records)
    limits = [step.params["limit_kw"] for step in records[0].transform_steps if "limit_kw" in step.params]
    assert limits
    limit = limits[-1]
    ratios = [record.value / limit for record in records if record.value is not None]
    high, low = max(ratios), min(ratios)
    assert low < high

    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        ref = services.storage.series.write_partition(bundle)
        source_map = services.version_run.integration_artifacts.prepare_series_map([
            {"quantity": "active_power", "ref": ref, "source_id": bundle.source_id,
             "source_version": bundle.source_version, "basis": "B18 generated D05 fixture"},
        ])
        manifest = versions.prepare({"data_sources": source_map}, "B18 replay preview integration")
        manifest = versions.check_integrity(manifest.version_manifest_id)
        manifest = versions.activate(None, manifest.version_manifest_id,
                                     Actor(actor_id="preview-test", roles=("reviewer",)), "B18 preview test")

        start = records[0].interval.start
        end = records[-1].interval.end
        inputs = {"online_source_ids": [bundle.source_id],
                  "online_record_hashes": [online_record_hash(record) for record in records]}
        run = services.version_run.runs.create_run(
            context(plant_id=settings.plant_id, as_of=start, version_manifest_id=manifest.version_manifest_id),
            "replay_day", Interval(start=start, end=end), inputs=inputs)
        ctx = context(plant_id=settings.plant_id, run_id=run.run_id, as_of=start,
                      version_manifest_id=manifest.version_manifest_id)
        clock = services.registry.resolve(StateProvider)
        high_record = max(records, key=lambda record: record.value if record.value is not None else float("-inf"))
        low_record = min(records, key=lambda record: record.value if record.value is not None else float("inf"))
        high_at, low_at = high_record.interval.end, low_record.interval.end
        clock.advance(ctx, 0, max(high_at, low_at))

        consultation = services.registry.hooks_for("consultation.service")[-1]
        replay_router = next(router for router, _ in consultation._bindings if router.run_id == run.run_id)
        threshold = (high + low) / 2
        replay_router.policy = replay_router.policy.model_copy(update={"preview_transformer_high": threshold})
        state_provider = services.registry.hooks_for(PREVIEW_STATE_HOOK)[-1]
        query = services.registry.resolve(TimeSeriesQuery)

        def state_at(instant):
            view = state_provider(replay_router, instant)
            return view, query.query(
                ctx.model_copy(update={"as_of": instant}), "active_power",
                Interval(start=instant - timedelta(minutes=15), end=instant),
            )

        high_state, high_bundle = state_at(high_at)
        low_state, low_bundle = state_at(low_at)
        assert any(record.locator.meter == "transformer_load:TX1" for record in high_bundle.records)
        assert any(record.locator.meter == "transformer_load:TX1" for record in low_bundle.records)
        assert high_state["transformer_ratio"] == high
        assert low_state["transformer_ratio"] == low
        assert consultation._previews[id(replay_router)]._triggered(high_state, replay_router.policy)
        assert not consultation._previews[id(replay_router)]._triggered(low_state, replay_router.policy)
    finally:
        services.close()


def test_replay_event_uses_same_run_router_and_writes_preview_check():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        run = services.version_run.runs.create_run(
            context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id),
            "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
        ctx = context(plant_id=settings.plant_id, run_id=run.run_id,
                      version_manifest_id=manifest.version_manifest_id)
        now = AS_OF + timedelta(minutes=15)
        signal_at = now + timedelta(minutes=15)
        services.registry.register_hook(PREVIEW_STATE_HOOK, lambda router, at: {"soc": 0.2})
        services.registry.register_hook(
            PREVIEW_SIGNALS_HOOK,
            lambda router, at: [PreviewSignal(kind="plan_start", at=signal_at, event_type="load_spike",
                                               experts=("energy",), summary="test", entity_id=None)]
            if router.run_id == run.run_id else [],
        )
        services.registry.resolve(StateProvider).advance(ctx, 0, now)
        consultation = services.registry.hooks_for("consultation.service")[-1]
        assert consultation.wait_preview(2)
        default_router = services.registry.hooks_for(ROUTER_HOOK)[-1]
        event = DayEvent(event_id="EV-RP", at=signal_at, event_type="load_spike", source="script",
                         severity=3, summary="test", nature="synthetic")
        handled = default_router.handle_event(event, run_id=run.run_id)
        assert handled.ticket_id
        replay_router = default_router._run_router(run.run_id)
        assert json.loads(replay_router.store.ticket_state(handled.ticket_id, "preview_check_1"))["matched"]
        assert replay_router.store.run_of_ticket(handled.ticket_id) == run.run_id
    finally:
        services.close()


def test_replay_preview_expert_uses_visible_check_time_not_future_signal():
    settings = load_settings(profile="mock", overrides={"storage.enabled": True})
    services = build_services(settings)
    seen = []

    class Source:
        def get(self, kind, binding, payload):
            seen.append((binding.trigger_at, payload["as_of"]))
            return {"proposal": ExpertProposal(expert="energy", ticket_id=binding.ticket_id,
                    actions=(), cost=ProposalCost(cny=0), confidence=0.5,
                    diagnosis="test").model_dump(mode="json")}
    services.registry.mode_ports[(AgentResultSource, "deterministic", "expert")] = Source()
    try:
        versions = services.version_run.versions
        versions.register_verifier("t:", lambda _: "complete")
        manifest = publish(versions, "t:rules@v1", None)
        run = services.version_run.runs.create_run(
            context(plant_id=settings.plant_id, version_manifest_id=manifest.version_manifest_id),
            "replay_day", Interval(start=AS_OF, end=AS_OF + timedelta(days=1)))
        ctx = context(plant_id=settings.plant_id, run_id=run.run_id,
                      version_manifest_id=manifest.version_manifest_id)
        now = AS_OF + timedelta(minutes=15)
        signal_at = now + timedelta(minutes=15)
        services.registry.register_hook(PREVIEW_STATE_HOOK, lambda router, at: {"soc": 0.2})
        services.registry.register_hook(
            PREVIEW_SIGNALS_HOOK,
            lambda router, at: [PreviewSignal(kind="plan_start", at=signal_at, event_type="load_spike",
                                               experts=("energy",), summary="test")]
            if router.run_id == run.run_id else [],
        )
        services.registry.resolve(StateProvider).advance(ctx, 0, now)
        consultation = services.registry.hooks_for("consultation.service")[-1]
        assert consultation.wait_preview(2)
        assert seen == [(now, now.isoformat())]
    finally:
        services.close()

