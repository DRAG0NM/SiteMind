from plantkg.evaluation.cache import ResultCache
from plantkg.evaluation.scenario import compare


def test_comparability_rejects_missing_sources_and_low_terminal_energy():
    groups = [dict(group=name, data_sources=[{'source': 'D05', 'dataset_version': 'v1'}],
                   energy_start_kwh=400, energy_end_kwh=400, energy_end_target_kwh=400)
              for name in ('baseline', 'independent', 'coordinated')]
    groups[1]['energy_end_kwh'] = 399
    compare(groups)
    assert groups[0]['comparable'] and not groups[1]['comparable']
    groups[2]['data_sources'] = [{'source': 'HIPE', 'dataset_version': 'v1'}]
    compare(groups)
    assert all(not group['comparable'] for group in groups)


def test_cache_version_and_state_separate_entries(tmp_path):
    cache = ResultCache(tmp_path / 'cache.json', 'v1')
    args = dict(scenario='demo', event_id='e1', knowledge_version=1, prompt_version='p1',
                data_version='d1', proposal=[], state={'soc': 0.4}, kind='arbitration')
    key = cache.key(**args)
    cache.put(key, {'selected_proposal_id': 'p'}, kind='arbitration', source='nexent')
    assert ResultCache(cache.path, 'v1').get(key)['source'] == 'nexent'
    assert cache.key(**(args | {'state': {'soc': 0.3}})) != key
    assert ResultCache(cache.path, 'v2').key(**args) != key
    cache.refresh(key, {'selected_proposal_id': 'q'}, kind='arbitration', source='deterministic')
    assert cache.get(key)['result']['selected_proposal_id'] == 'q'
