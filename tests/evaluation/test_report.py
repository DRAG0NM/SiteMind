import json

from plantkg.evaluation.report import write_report


def report(comparable=None):
    groups = ('baseline', 'independent', 'coordinated')
    flags = dict.fromkeys(groups, True) | (comparable or {})
    return {'scenario': 'demo',
            'groups': [{'group': name, 'comparable': flags[name]} for name in groups],
            'metrics': [{'group': name, 'metric': 'electricity_cost', 'value': value, 'unit': 'CNY', 'note': None}
                        for name, value in zip(groups, (100.0, 90.0, 110.0))]}


def test_comparable_groups_get_deltas(tmp_path):
    write_report(report(), tmp_path)
    comparison = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))
    assert [(x['group'], x['metric'], x['delta']) for x in comparison['comparisons']] == [
        ('independent', 'electricity_cost', -10.0), ('coordinated', 'electricity_cost', 10.0)]
    svg = (tmp_path / 'cost.svg').read_text(encoding='utf-8')
    assert all(f'{value} incomparable' not in svg for value in (100.0, 90.0, 110.0))


def test_incomparable_groups_stay_out_of_comparisons(tmp_path):
    write_report(report({'baseline': False}), tmp_path)
    comparison = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))
    assert comparison['comparisons'] == []
    assert '100.0 incomparable' in (tmp_path / 'cost.svg').read_text(encoding='utf-8')

    write_report(report({'coordinated': False}), tmp_path / 'partial')
    comparison = json.loads((tmp_path / 'partial' / 'comparison.json').read_text(encoding='utf-8'))
    assert [x['group'] for x in comparison['comparisons']] == ['independent']
    assert '110.0 incomparable' in (tmp_path / 'partial' / 'cost.svg').read_text(encoding='utf-8')
