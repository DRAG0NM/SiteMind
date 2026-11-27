"""Published correction is consumed by the bound run, then gated by B47."""
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from plantkg.core.contracts import Actor, AgentResultKind, LoadCategory, Role
from plantkg.core.ports import AgentResultSource, PendingActionStore, RuleMatcher, RunStore, VersionStore
from plantkg.core.registry import Registry
from plantkg.experts.energy.minute import install_minute_layer
from plantkg.evolution.service import EvolutionService, evolution_config
from plantkg.evolution.store import EvolutionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.runtime import matches_combo_rule
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
from plantkg.world.scenario.script import load_scenario


def test_correction_rule_is_consumed_and_gated_by_bound_version(tmp_path):
    settings = load_settings(profile='dev', overrides={
        'storage.enabled': True, 'storage.sqlite': str(tmp_path / 'catalog.db'),
        'storage.graph': str(tmp_path / 'graph'), 'storage.series': str(tmp_path / 'series')})
    services = build_services(settings)
    try:
        registry = services.registry
        scenario = load_scenario('configs/scenarios/factory_demo_day.yaml')
        world = build_world(scenario)
        runtime = default_registry(registry)
        runner = ScenarioRunner(world, ScenarioDB.from_writer(services.storage.writer, world.site.plant_id),
                                registry=runtime, mode='evaluation', group='coordinated')
        original_upload = runtime.hooks_for('scenario.document_upload')[-1]
        evidence = {}

        def upload_and_confirm(runner, at, payload):
            original_upload(runner, at, payload)
            if payload.get('event') != 'act4-combo':
                return
            ticket = next(t for t in runner.trace.tickets.values() if 'act4-combo' in t.get('event_ids', []))
            reviewer = Actor(actor_id='reviewer-260', roles=(Role.reviewer,))
            context = runner._context(runner.run_info.version_manifest_id).model_copy(
                update={'actor': reviewer, 'run_id': runner.run_info.run_id, 'as_of': at})
            evidence['old_run'] = runner.run_info.run_id
            evidence['old_manifest'] = runner.run_info.version_manifest_id
            correction = {'note': '照明与空压异常由生产总配电柜接触不良引起', 'event_type': 'unknown_combo',
                          'entity_id': runner.ids['LT-P'],
                          'autonomy_category': 'unknown_combo|lighting', 'autonomy_level': 'L2',
                          'condition': {'event_type': 'unknown_combo', 'observation_contains': '组合异常',
                                        'actions': [{'entity_id': runner.ids['LT-P'], 'command': 'repair'}]}}
            result = registry.hooks_for('evolution.record_correction')[-1](context, ticket['ticket_id'], correction)
            assert result.ok, result.to_agent_text()
            evidence['candidate'] = result.data['candidate_id']
            actions = registry.resolve(PendingActionStore)
            versions = registry.resolve(VersionStore)
            evolution = EvolutionService(EvolutionStore(versions.writer, versions.plant_id), versions, actions,
                                         RuleArtifacts(versions.writer, versions.plant_id), registry, context,
                                         config=evolution_config(services.settings))
            specs = [{'scenario_id': 'factory_demo_day', 'seed': scenario.seed,
                      'path': 'configs/scenarios/factory_demo_day.yaml'}]
            report = evolution.backtest(evidence['candidate'], specs)
            assert report.verdict == 'pass', (report.failures, report.metrics)
            proposal = registry.tools['propose_publish'].handler(context, candidate_id=evidence['candidate'], rationale='模拟复核')
            assert proposal.ok, proposal.to_agent_text()
            action = actions.get(proposal.data['action_id'])
            assert action.payload['backtest_report']['verdict'] == 'pass'
            assert action.payload['authorization'] == {'category': 'unknown_combo|lighting', 'level': 'L2'}
            actions.confirm(action.action_id, reviewer, '模拟人工确认发布', action.revision, 'issue-260-publish')
            evidence['new_run'] = runner.run_info.run_id
            evidence['new_manifest'] = runner.run_info.version_manifest_id
            evidence['fork_at'] = runner.run_info.forked_at

        runtime.register_hook('scenario.document_upload', upload_and_confirm)
        runner.start()
        install_minute_layer(runner)
        trace = runner.run_until(scenario.instant('+1 16:10'))
        assert evidence['fork_at'] == scenario.instant('16:10')
        assert evidence['old_manifest'] != evidence['new_manifest']
        first = next(n for n in trace.notes if n.get('event_id') == 'act4-combo' and n['kind'] == 'admission')
        assert first['channel'] == 'slow'
        assert not any(a.get('source', {}).get('ticket_id') == first['ticket_id'] for a in trace.actions)
        again = next(n for n in trace.notes if n.get('event_id') == 'day2-combo' and n['kind'] == 'admission')
        assert again['channel'] == 'fast'
        proposal = next(p for p in trace.proposals if p.ticket_id == again['ticket_id'] and p.expert == 'equipment')
        assert proposal.binding.run_id == evidence['new_run']
        assert proposal.actions and any(ref.startswith('rule:b32-') for ref in proposal.evidence_refs)
        execution = next(n for n in trace.notes if n.get('ticket_id') == again['ticket_id'] and n['kind'] == 'b47_execution')
        assert execution['level'] == 'L2' and execution['executed'] >= 1
        assert any(a.get('source', {}).get('ticket_id') == again['ticket_id'] for a in trace.actions)
        action = next(a for a in trace.actions if a.get('source', {}).get('ticket_id') == again['ticket_id'])
        from datetime import datetime
        effective = datetime.fromisoformat(action['action']['interval']['start'].replace('Z', '+00:00'))
        before = next(m for m in trace.ledger if m.entry.interval.start == effective - timedelta(minutes=1))
        after = next(m for m in trace.ledger if m.entry.interval.start == effective)
        assert before.balanced and after.balanced
        assert after.entry.load_breakdown[LoadCategory.lighting] > before.entry.load_breakdown[LoadCategory.lighting]
        matcher = runtime.resolve(RuleMatcher)
        event = next(e for e in trace.events if e.event_id == 'day2-combo')
        assert matcher.covers_for_run(event, evidence['new_run'])
        assert matcher.authorizes_for_run(event, evidence['new_run'], proposal.actions)
        assert not matcher.authorizes_for_run(event, evidence['new_run'],
                                              (proposal.actions[0], proposal.actions[0].model_copy(update={'entity_id': runner.ids['AC-2']})))
        assert not matcher.authorizes_for_run(event, evidence['new_run'],
                                              (proposal.actions[0].model_copy(update={'params': {'mode': 'unsafe'}}),))
        assert not matcher.covers_for_run(event, evidence['old_run'])
        assert not matcher.covers_for_run(event.model_copy(update={'entity_id': runner.ids['AC-2']}), evidence['new_run'])
        assert not matcher.covers_for_run(event.model_copy(update={'summary': '不匹配'}), evidence['new_run'])
        source = runtime.resolve_mode(AgentResultSource, 'deterministic', 'equipment')
        payload = {'event': event.model_dump(mode='json'), 'ids': runner.ids, 'knowledge': []}
        old_binding = proposal.binding.model_copy(update={
            'run_id': evidence['old_run'],
            'knowledge_version': registry.resolve(VersionStore).knowledge_version(evidence['old_manifest'])})
        old_proposal = source.get(AgentResultKind.expert, old_binding, payload)['proposal']
        assert not old_proposal['actions'] and not any(ref.startswith('rule:b32-') for ref in old_proposal['evidence_refs'])
        wrong_event = event.model_copy(update={'entity_id': runner.ids['AC-2']}).model_dump(mode='json')
        wrong_proposal = source.get(AgentResultKind.expert, proposal.binding, payload | {'event': wrong_event})['proposal']
        assert not wrong_proposal['actions']
        assert any(n.get('event_id') == 'day2-elevator' for n in trace.notes if n['kind'] == 'admission')
        autonomy = registry.hooks_for('evolution.autonomy_for_run')[-1]
        assert autonomy('unknown_combo|lighting', evidence['new_run'], runner.policy) == 'L2'
        assert autonomy('unknown_combo|lighting', evidence['old_run'], runner.policy) == 'L0'
        assert registry.resolve(VersionStore).get(evidence['new_manifest']).ever_active
        independent = runner.db.runs.create_run(
            runner._context(evidence['new_manifest']).model_copy(update={'as_of': scenario.instant('+1 16:00')}),
            'scenario', runner.run_info.clock)
        execution_service = runtime.hooks_for('router.execution')[-1].__self__
        independent_router = SimpleNamespace(run_id=independent.run_id, policy=runner.policy)
        assert execution_service._effective_level(independent_router, 'unknown_combo|lighting') == 'L2'
        versions = registry.resolve(VersionStore)
        versions.activate(evidence['new_manifest'], evidence['old_manifest'],
                          Actor(actor_id='reviewer-260', roles=(Role.reviewer,)), '模拟撤回新规则')
        assert versions.active().version_manifest_id == evidence['old_manifest']
        assert not matcher.covers_for_run(event, evidence['old_run'])
        assert matcher.authorizes_for_run(event, evidence['new_run'], proposal.actions)
    finally:
        services.close()


def test_one_rule_must_authorize_the_entire_proposal():
    from plantkg.evolution.service import PublishedRulesMatcher

    at = datetime(2026, 7, 2, 8, tzinfo=timezone.utc)
    event = SimpleNamespace(event_type='unknown_combo', entity_id='entity://plant/lighting/LT-P',
                            summary='组合异常', at=at)
    def rule(target):
        return SimpleNamespace(kind='unknown_combo', subject_ids=(event.entity_id,), condition={
            'event_type': 'unknown_combo', 'actions': [{'entity_id': target, 'command': 'repair'}]})
    light, compressor = event.entity_id, 'entity://plant/air_compressor/AC-1'
    rules = [rule(light), rule(compressor)]
    registry = Registry()
    registry.register(RunStore, SimpleNamespace(create_run=lambda *_args, **_kwargs: None,
                                                get_run=lambda _: SimpleNamespace(
                                                    as_of=at, forked_at=None, version_manifest_id='manifest')))
    registry.register_hook('knowledge.b30.rules', lambda *_: rules)
    registry.register_hook('knowledge.rules.matches_combo', matches_combo_rule)
    matcher = PublishedRulesMatcher(None, registry)
    actions = tuple(SimpleNamespace(entity_id=target, command='repair', params={}) for target in (light, compressor))
    assert matcher.authorizes_for_run(event, 'run', actions[:1])
    assert not matcher.authorizes_for_run(event, 'run', actions)


def test_authorization_is_idempotent_and_unrelated_publish_inherits_cap(tmp_path):
    from plantkg.agent.execution_policy.store import ExecutionStore
    from plantkg.storage.migration import MigrationRunner
    from plantkg.storage.sqlite import SQLiteWriter

    writer = SQLiteWriter(tmp_path / 'b47.db')
    try:
        MigrationRunner(writer).apply()
        store = ExecutionStore(writer, 'plant')
        at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        category = 'unknown_combo|lighting'
        store.set_level(f'@policy:old:{category}', 'L0', at)
        store.publish_manifest_caps('old', 'new', category, 'reviewed-action', at)
        assert store.level(f'@policy:old:{category}')[0] == 'L0'
        assert store.level(f'@policy:new:{category}')[0] == 'L3'
        store.set_level(f'@policy:new:{category}', 'L1', at + timedelta(minutes=1))
        store.publish_manifest_caps('old', 'new', category, 'reviewed-action', at + timedelta(minutes=2))
        assert store.level(f'@policy:new:{category}')[0] == 'L1'
        store.publish_manifest_caps('new', 'unrelated', None, 'other-action', at + timedelta(minutes=3))
        assert store.level(f'@policy:unrelated:{category}')[0] == 'L1'
        assert store.level(f'@release:unrelated:{category}')[0] == 'L3'
    finally:
        writer.close()


def test_advice_evidence_is_not_reused_for_a_merged_ticket():
    from plantkg.bootstrap import _trace_summary

    at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    event = SimpleNamespace(event_id='event-1', event_type='unknown_combo', at=at, source='script',
                            entity_id='entity://plant/lighting/LT-P', summary='组合异常')
    other = SimpleNamespace(event_id='event-2', event_type='unknown_combo', at=at, source='script',
                            entity_id='entity://plant/air_compressor/AC-2', summary='不同异常')
    binding = SimpleNamespace(run_id='run', knowledge_version=1, ticket_id='ticket', trigger_at=at, round=1)
    proposal = SimpleNamespace(expert='equipment', actions=(), binding=binding, ticket_id='ticket',
                               evidence_refs=('rule:advice',))
    trace = SimpleNamespace(tickets={'ticket': {'event_ids': ['event-1'], 'round': 1}}, events=[event],
                            proposals=[proposal], runs=[SimpleNamespace(run_id='run', forked_at=None, knowledge_version=1)],
                            notes=[], ledger=[], agent_runs=[], ledger_summary=lambda: {'cost_cny': 0, 'capacity_violation_minutes': 0})
    matcher = SimpleNamespace(hits={}, matches={}, action_for=lambda _: None)
    registry = Registry()
    assert _trace_summary(trace, matcher, registry)['events'][0]['advice_rule_ids'] == ['advice']
    trace.notes.extend([{'kind': 'b47_execution', 'ticket_id': 'ticket', 'level': 'L0'},
                        {'kind': 'b47_execution', 'ticket_id': 'ticket', 'level': 'L2'}])
    assert _trace_summary(trace, matcher, registry)['events'][0]['decision_levels'] == ['L0', 'L2']
    trace.tickets['ticket']['event_ids'].append('event-2')
    trace.events.append(other)
    merged = _trace_summary(trace, matcher, registry)['events']
    assert all(not row['advice_rule_ids'] and not row['decision_levels'] for row in merged)


def test_each_changed_policy_category_must_be_observed_across_scenarios():
    service = object.__new__(EvolutionService)
    candidate = SimpleNamespace(candidate_id='candidate', base_manifest_id='base', changes={})
    scenarios = [{'scenario_id': 'first', 'seed': 1}, {'scenario_id': 'second', 'seed': 1}]
    first = {'scenario_id': 'first', 'executed': True, 'ledger_verified': True,
             'policy_targets': True, 'policy_required': ['a', 'b'], 'policy_seen': ['a'], 'policy_met': ['a'],
             'metrics': {'target': {'base': 0, 'candidate': 1}}}
    missing = dict(first, scenario_id='second', policy_seen=[], policy_met=[],
                   metrics={'target': {'base': 0, 'candidate': 0}})
    report = service._backtest_report(candidate, scenarios, [first, missing])
    assert report.verdict == 'inconclusive' and any('b' in failure for failure in report.failures)
    rejected = dict(missing, policy_seen=['b'])
    assert service._backtest_report(candidate, scenarios, [first, rejected]).verdict == 'fail'
    covered = dict(missing, policy_seen=['b'], policy_met=['b'])
    assert service._backtest_report(candidate, scenarios, [first, covered]).verdict == 'pass'
    conflict = dict(covered, policy_seen=['a', 'b'], policy_failed=['a'])
    assert service._backtest_report(candidate, scenarios, [first, conflict]).verdict == 'fail'
    waiting = dict(covered, policy_undecided=['a'])
    assert service._backtest_report(candidate, scenarios, [first, waiting]).verdict == 'inconclusive'
