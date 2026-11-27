"""#238：config 组是配置限值，不参与 B33 改善率。"""
import json

from plantkg.evaluation.report import write_report


def test_config_metrics_stay_out_of_improvement_rates(tmp_path):
    report = {
        'scenario': 'demo',
        'groups': [{'group': name, 'comparable': True} for name in ('baseline', 'independent', 'coordinated')],
        'metrics': [
            {'group': name, 'metric': 'electricity_cost', 'value': value, 'unit': 'CNY', 'note': None}
            for name, value in zip(('baseline', 'independent', 'coordinated'), (100.0, 90.0, 110.0))
        ] + [
            {'group': 'config', 'metric': name, 'value': value, 'unit': unit, 'note': '配置值、不参与改善率'}
            for name, value, unit in (('transformer_limit_pct', 85.0, '%'), ('contract_capacity_kw', 1200.0, 'kW'),
                                      ('battery_soc_floor_pct', 10.0, '%'), ('hvac_supply_setpoint_c', 22.0, 'degC'))
        ],
    }
    write_report(report, tmp_path)
    comparison = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))
    assert [x['metric'] for x in comparison['comparisons']] == ['electricity_cost'] * 2
    assert not any(name in comparison['comparisons'][0] for name in ('transformer', 'contract', 'battery', 'hvac'))
    assert 'transformer_limit_pct' not in (tmp_path / 'comparison.json').read_text(encoding='utf-8')
