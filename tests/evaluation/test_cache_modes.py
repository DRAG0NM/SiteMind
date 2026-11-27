from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import time

import pytest

from plantkg.core.contracts import AgentResultKind, PolicyConfig, ResponseBudget, RunBinding
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.evaluation.cache import ArbitrationCollector, ResultCache, install_cache
from plantkg.world.scenario.coordinator import TimeCoordinator


class Source:
    def __init__(self, value):
        self.value = value

    def get(self, kind, binding, payload):
        return {'status': 'finished', 'value': self.value}


def test_modes_cache_fallback_and_live_are_isolated(tmp_path):
    registry = Registry()
    keys = [x.value for x in AgentResultKind]
    for key in keys:
        registry.register_mode(AgentResultSource, 'deterministic', Source('fallback-' + key), key=key)
        registry.register_mode(AgentResultSource, 'live', Source('live-' + key), key=key)
    cache = ResultCache(tmp_path / 'cache.json', 'v1')
    context = {'scenario': 'sha', 'prompt_version': 'p1', 'data_version': 'd1'}
    install_cache(registry, cache, context, keys)
    binding = SimpleNamespace(knowledge_version=1)
    payload = {'event_id': 'event-1', 'state': {'soc': 0.5}}
    for key in keys:
        provider = registry.resolve_mode(AgentResultSource, 'evaluation', key)
        missing = provider.get(AgentResultKind(key), binding, payload)
        assert missing['coverage_gap'] and missing['value'] == 'fallback-' + key
        identity = cache.key(scenario='sha', event_id='event-1', knowledge_version=1, prompt_version='p1',
                             data_version='d1', proposal=None, state={'expert_key': key, 'expert': None, 'value': payload['state'],
                                                                       'trigger_at': '', 'round': None, 'state_version': None,
                                                                       'event': {}, 'reason': None} | ({'query': None} if key in ('follow_up', 'investigate') else {}),
                             kind=key)
        cache.put(identity, {'value': 'cached-' + key}, kind=key, source='nexent')
        assert provider.get(AgentResultKind(key), binding, payload)['value'] == 'cached-' + key
        assert provider.get(AgentResultKind(key), binding, payload | {'state': {'soc': 0.2}})['coverage_gap']
        assert registry.resolve_mode(AgentResultSource, 'live', key).get(AgentResultKind(key), binding, payload)['value'] == 'live-' + key


def test_cache_key_includes_full_resolved_state(tmp_path):
    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='investigate')
    cache = ResultCache(tmp_path / 'cache.json', 'v1')
    runtime = {'state': {'devices': {'x': 1}}}
    source = install_cache(registry, cache, {'scenario': 'sha', 'prompt_version': 'p', 'data_version': 'd',
                           'resolve': lambda binding: ('event', runtime['state'])}, ['investigate'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at')
    payload = {'state': {'brief': 1}}
    state = {'payload': payload['state'], 'scenario_state': runtime['state']}
    key = cache.key(scenario='sha', event_id='event', knowledge_version=1, prompt_version='p', data_version='d',
                    proposal=None, state={'expert_key': 'investigate', 'expert': None, 'query': None, 'value': state, 'trigger_at': 'at',
                                          'round': None, 'state_version': None, 'event': {}, 'reason': None}, kind='investigate')
    cache.put(key, {'value': 'cached'}, kind='investigate', source='nexent')
    assert source.get(AgentResultKind.investigate, binding, payload)['value'] == 'cached'
    runtime['state'] = {'devices': {'x': 2}}
    assert source.get(AgentResultKind.investigate, binding, payload)['coverage_gap']


def test_cache_key_includes_expert_name(tmp_path):
    """会诊各专家共用 EXPERT 注册键：缓存键不带专家名时，先收集的能源提案会被回放给暖通。"""
    class Live:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'proposal': {'expert': payload.get('expert'), 'ticket_id': binding.ticket_id}}

    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='expert')
    source = install_cache(registry, ResultCache(tmp_path / 'expert.json', 'v1'),
                           {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd'}, ['expert'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-1', ticket_id='ticket-1')
    payload = {'event_id': 'e', 'state': {'soc': 0.5}}
    source.collect(AgentResultKind.expert, binding, payload | {'expert': 'energy'}, Live())
    energy = source.get(AgentResultKind.expert, binding, payload | {'expert': 'energy'})
    assert (energy['source'], energy['coverage_gap'], energy['proposal']['expert']) == ('cache', False, 'energy')
    misses = source.misses
    hvac = source.get(AgentResultKind.expert, binding, payload | {'expert': 'hvac'})
    assert source.misses == misses + 1 and hvac['coverage_gap']
    assert 'proposal' not in hvac


def test_explicit_collection_refresh_and_bound_result_rejection(tmp_path):
    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='follow_up')
    cache = ResultCache(tmp_path / 'cache.json', 'v1')
    source = install_cache(registry, cache, {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd'}, ['follow_up'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-1', ticket_id='ticket-1')
    payload = {'event_id': 'e1', 'state': {'soc': 0.5}}

    class Live:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'agent_run_id': 'original-agent-run', 'final_answer': '结果'}

    source.collect(AgentResultKind.follow_up, binding, payload, Live())
    assert source.get(AgentResultKind.follow_up, binding, payload)['final_answer'] == '结果'
    with pytest.raises(ValueError, match='explicitly refresh'):
        source.collect(AgentResultKind.follow_up, binding, payload, Live())
    source.collect(AgentResultKind.follow_up, binding, payload, Live(), refresh=True)
    assert source.get(AgentResultKind.follow_up, binding, payload)['source'] == 'cache'


def test_arbitration_rebinds_selected_proposal_after_replay(tmp_path):
    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='arbitration')
    cache = ResultCache(tmp_path / 'cache.json', 'v1')
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-1', ticket_id='ticket-1')
    payload = {'event_id': 'event', 'state': {'soc': 0.5}, 'proposals': [
        {'proposal_id': 'old', 'ticket_id': 'ticket-1', 'diagnosis': 'x'}]}

    class Live:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'agent_run_id': 'ar-1',
                    'final_answer': '{"selected_proposal_id":"old","summary":"ok"}'}

    collector = ArbitrationCollector(Live(), max_calls=1)
    source = install_cache(registry, cache, {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd',
                           'collector': collector}, ['arbitration'])[0]
    assert source.get(AgentResultKind.arbitration, binding, payload)['selected_proposal_id'] == 'old'
    replay = payload | {'proposals': [{'proposal_id': 'new', 'ticket_id': 'ticket-2', 'diagnosis': 'x'}]}
    assert source.get(AgentResultKind.arbitration, binding, replay)['selected_proposal_id'] == 'new'
    assert collector.remaining == 0


def test_all_kinds_offline_collection_persists_refreshes_and_rejects_other_versions(tmp_path):
    class CompleteSource(Source):
        def get(self, kind, binding, payload):
            result = super().get(kind, binding, payload)
            if kind == AgentResultKind.expert:
                result['proposal'] = {'expert': 'equipment', 'ticket_id': binding.ticket_id}
            elif kind == AgentResultKind.investigate:
                result.update(diagnosis='diagnosis', experts=['equipment'], evidence_refs=[])
            elif kind in (AgentResultKind.triage, AgentResultKind.follow_up):
                result['final_answer'] = 'answer'
            return result

    registry = Registry()
    for kind in AgentResultKind:
        registry.register_mode(AgentResultSource, 'deterministic', CompleteSource(kind.value), key=kind.value)
    path = tmp_path / 'all-kinds.json'
    context = {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'collector': 'deterministic'}
    cache = ResultCache(path, 'v1')
    install_cache(registry, cache, context, [k.value for k in AgentResultKind])
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='r', ticket_id='t')
    payload = {'event_id': 'event', 'state': {'soc': 0.5}}
    for kind in AgentResultKind:
        evaluation = registry.resolve_mode(AgentResultSource, 'evaluation', kind.value)
        assert evaluation.get(kind, binding, payload)['value'] == kind.value
        assert (evaluation.hits, evaluation.misses, evaluation.collected) == (0, 1, 1)
    assert len(ResultCache(path, 'v1').entries) == len(AgentResultKind)
    for kind in AgentResultKind:
        trial = registry.resolve_mode(AgentResultSource, 'trial', kind.value)
        assert trial.get(kind, binding, payload)['value'] == kind.value
        assert trial.hits == 1
        assert trial.collected == 0
    assert ResultCache(path, 'v2').key(scenario='s', event_id='event', knowledge_version=1, prompt_version='p',
                                      data_version='d', proposal=None, state={}, kind='triage') not in cache.entries


def test_triage_batch_rebinds_ticket_identifiers(tmp_path):
    class Triage:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'final_answer': 'TK-001: a; TK-002: b'}

    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Triage(), key='triage')
    context = {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'collector': 'deterministic'}
    source = install_cache(registry, ResultCache(tmp_path / 'batch.json', 'v1'), context, ['triage'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-1', ticket_id='TK-001')
    payload = {'event_id': 'e', 'state': {}, 'items': [{'ticket_id': 'TK-001', 'summary': 'a'}, {'ticket_id': 'TK-002', 'summary': 'b'}]}
    assert source.get(AgentResultKind.triage, binding, payload)['final_answer'] == 'TK-001: a; TK-002: b'
    next_binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-2', ticket_id='TK-011')
    next_payload = payload | {'items': [{'ticket_id': 'TK-011', 'summary': 'a'}, {'ticket_id': 'TK-012', 'summary': 'b'}]}
    assert source.get(AgentResultKind.triage, next_binding, next_payload)['final_answer'] == 'TK-011: a; TK-012: b'


def test_triage_key_uses_proposal_contents_not_runtime_ids(tmp_path):
    class Proposal:
        def __init__(self, diagnosis):
            self.diagnosis = diagnosis

        def model_dump(self, **kwargs):
            return {'proposal_id': 'volatile', 'ticket_id': 'volatile', 'diagnosis': self.diagnosis}

    registry = Registry()
    values = {'old': Proposal('a'), 'new': Proposal('a'), 'different': Proposal('b')}
    context = {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'resolve_proposals': values.get}
    source = install_cache(registry, ResultCache(tmp_path / 'triage.json', 'v1'), context, ['triage'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at')
    payload = {'event_id': 'e', 'state': {}, 'items': [{'ticket_id': 't', 'proposals': ['old']}]}
    same = payload | {'items': [{'ticket_id': 'other', 'proposals': ['new']}]}
    different = payload | {'items': [{'ticket_id': 'other', 'proposals': ['different']}]}
    assert source._key(AgentResultKind.triage, binding, payload) == source._key(AgentResultKind.triage, binding, same)
    assert source._key(AgentResultKind.triage, binding, payload) != source._key(AgentResultKind.triage, binding, different)


def test_concurrent_missing_key_collects_once(tmp_path):
    class Collector:
        cache_source = 'deterministic'
        calls = 0

        def get(self, kind, binding, payload):
            self.calls += 1
            time.sleep(.05)
            return {'status': 'finished', 'final_answer': 'ok'}

    collector = Collector()
    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='follow_up')
    source = install_cache(registry, ResultCache(tmp_path / 'concurrent.json', 'v1'),
                           {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'collector': collector}, ['follow_up'])[0]
    binding = SimpleNamespace(knowledge_version=1, trigger_at='at', run_id='run-1', ticket_id='ticket-1')
    payload = {'event_id': 'e', 'state': {}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: source.get(AgentResultKind.follow_up, binding, payload), range(2)))
    assert all(result['final_answer'] == 'ok' for result in results)
    assert collector.calls == source.collected == 1


def test_live_collection_is_recorded_as_nexent_and_replay_as_cache(tmp_path):
    """The latency metric only sees runs the trace labels 'nexent'; a live collection must say so."""
    class Live:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'proposal': {
                'expert': 'equipment', 'ticket_id': binding.ticket_id,
                'cost': {'cny': 1.0}, 'confidence': 0.8}}

    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Source('fallback'), key='equipment')
    install_cache(registry, ResultCache(tmp_path / 'live.json', 'v1'),
                  {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'collector': Live()}, ['equipment'])
    policy = PolicyConfig(policy_version='p1', arbitration_weights={'cny': 1.0},
                          priority_profiles={'default': ('safety', 'cny')},
                          response_budget=ResponseBudget(layer12_min=2, layer3_min=1))
    coordinator = TimeCoordinator(registry, 'evaluation', policy)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    binding = RunBinding(run_id='run-1', knowledge_version=1, ticket_id='ticket-1', round=1,
                         state_version=0, trigger_at=at)
    payload = {'event': {'event_id': 'event-1'}, 'state': {'soc': 0.5}}
    first = coordinator.request('equipment', binding, payload, at)
    assert (first.record.source, first.record.coverage_gap) == ('nexent', False)
    assert first.record.timing.latency_ms is not None
    assert coordinator.request('equipment', binding, payload, at).record.source == 'cache'


def test_expert_cache_miss_is_recorded_as_deterministic_run_mode(tmp_path):
    """#243：专家回退与主 Agent 同口径：run_mode=deterministic、source=deterministic、coverage_gap=true。"""
    class Offline:
        source_kind = 'deterministic'

        def get(self, kind, binding, payload):
            return {'status': 'finished', 'proposal': {'expert': 'equipment', 'ticket_id': binding.ticket_id,
                                                       'cost': {'cny': 1.0}, 'confidence': 0.8}}

    registry = Registry()
    registry.register_mode(AgentResultSource, 'deterministic', Offline(), key='equipment')
    install_cache(registry, ResultCache(tmp_path / 'miss.json', 'v1'),
                  {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd'}, ['equipment'])
    policy = PolicyConfig(policy_version='p1', arbitration_weights={'cny': 1.0},
                          priority_profiles={'default': ('safety', 'cny')},
                          response_budget=ResponseBudget(layer12_min=2, layer3_min=1))
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    binding = RunBinding(run_id='run-1', knowledge_version=1, ticket_id='ticket-1', round=1, state_version=0, trigger_at=at)
    record = TimeCoordinator(registry, 'evaluation', policy).request(
        'equipment', binding, {'event': {'event_id': 'event-1'}, 'state': {'soc': 0.5}}, at).record
    assert (record.run_mode, record.source, record.coverage_gap) == ('deterministic', 'deterministic', True)
