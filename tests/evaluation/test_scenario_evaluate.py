import shutil
from pathlib import Path

import yaml

from plantkg.evaluation.scenario import evaluate

ROOT = Path(__file__).resolve().parents[2]


def _totals(result):
    return {name: sum(group[f'cache_{name}'] for group in result['groups'])
            for name in ('hits', 'misses', 'collected')}


def _one_day_scenario(tmp_path):
    """Copy the demo day and its tariff input into tmp so the test can touch the input file."""
    tariff = tmp_path / 'jiangsu_2025_426.yaml'
    shutil.copyfile(ROOT / 'fixtures/packs/tariff/jiangsu_2025_426.yaml', tariff)
    data = yaml.safe_load((ROOT / 'configs/scenarios/factory_demo_day.yaml').read_text(encoding='utf-8'))
    data['days'] = 1  # one day is enough for the replay contract and keeps the test to ~3 minutes
    data.setdefault('sources', {})['tariff_rules'] = str(tariff)
    scenario = tmp_path / 'factory_demo_day.yaml'
    scenario.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return scenario, tariff


def test_evaluate_replays_cache_and_misses_after_tariff_change(tmp_path):
    """A second evaluation replays the collected results; touching an input file invalidates them.

    main #186 writes the run id into sims.sensors, so without stripping it from the cache
    key every replay key differs and the second evaluation misses everything.
    """
    scenario, tariff = _one_day_scenario(tmp_path)
    out = tmp_path / 'out'
    first = evaluate(scenario, out, collector='deterministic')
    assert _totals(first)['hits'] == 0
    assert {g['group']: g['cache']['mode'] for g in first['groups']} == {
        'baseline': 'none', 'independent': 'cold', 'coordinated': 'cold'}
    # #243：来源名统一小写（缓存键用 data_source_refs，与 data_sources 同口径）
    assert all(source['source'] == source['source'].lower() for g in first['groups'] for source in g['data_sources'])

    replay = evaluate(scenario, out)
    replay_totals = _totals(replay)
    assert replay_totals['hits'] > 0, f'expected cache hits on replay, got {replay_totals}'
    # #243：只剩零星命中的退化要测得出：命中率 >= 0.9 且缺失不超过 2 次
    hit_rate = replay_totals['hits'] / (replay_totals['hits'] + replay_totals['misses'])
    assert hit_rate >= 0.9 and replay_totals['misses'] <= 2, f'replay degraded: {replay_totals}'
    assert replay['groups'][2]['cache']['hit_rate'] >= 0.9 and replay['groups'][2]['coverage_gap']['cache_misses'] <= 2
    assert ([(metric['group'], metric['metric'], metric['value']) for metric in replay['metrics']] ==
            [(metric['group'], metric['metric'], metric['value']) for metric in first['metrics']])

    with tariff.open('a', encoding='utf-8') as handle:
        handle.write('\n# touched by test: content unchanged\n')
    changed = evaluate(scenario, out)
    changed_totals = _totals(changed)
    assert changed_totals['hits'] == 0 and changed_totals['misses'] > 0
