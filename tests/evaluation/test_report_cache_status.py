"""#243：报告自己说清缓存状态与覆盖不足；#235：指标名与 variant 符合契约。"""
import json

import pytest

from plantkg.core.contracts import DayMetric
from plantkg.evaluation.report import write_report
from plantkg.evaluation.scenario import cache_status, contract_metrics, knowledge_results


@pytest.mark.parametrize('hits,misses,collected,mode,rate', [
    (0, 0, 0, 'none', None), (0, 20, 20, 'cold', 0.0), (0, 21, 0, 'cold', 0.0),
    (25, 0, 0, 'replay', 1.0), (24, 1, 0, 'mixed', 0.96), (10, 0, 3, 'mixed', 1.0)])
def test_cache_status_modes(hits, misses, collected, mode, rate):
    status = cache_status(hits, misses, collected)
    assert status['mode'] == mode and status['hit_rate'] == rate
    assert (status['hits'], status['misses'], status['collected']) == (hits, misses, collected)


def test_comparison_summarizes_cache_and_coverage_gap(tmp_path):
    groups = [{'group': name, 'comparable': True, 'cache': cache_status(*counts),
               'coverage_gap': {'deterministic_fallbacks': gap, 'cache_misses': 1 if name == 'coordinated' else 0}}
              for name, counts, gap in (('baseline', (0, 0, 0), 0), ('independent', (11, 0, 0), 0), ('coordinated', (20, 1, 0), 21))]
    report = {'scenario': 'demo', 'groups': groups,
              'metrics': [{'group': g['group'], 'metric': 'electricity_cost', 'value': 1.0, 'unit': 'CNY', 'note': None} for g in groups]}
    write_report(report, tmp_path)
    comparison = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))
    assert comparison['cache']['coordinated']['mode'] == 'mixed'
    assert comparison['coverage_gap']['coordinated'] == {'deterministic_fallbacks': 21, 'cache_misses': 1}
    assert 'coordinated' in comparison['coverage_note'] and 'Comparability rules are unchanged' in comparison['coverage_note']
    assert all(g['comparable'] for g in comparison['groups'])  # 覆盖不足不改可比性判定


def test_contract_metrics_are_valid_unique_day_metrics():
    probe = {'routes': [{'route': 'A_programmatic', 'count': 10, 'routing_accuracy': 1.0, 'evidence_completeness': 0.0, 'mean_wall_ms': 2000.0},
                        {'route': 'B_fast_slow', 'count': 10, 'routing_accuracy': 0.9, 'evidence_completeness': 0.5, 'mean_wall_ms': 4000.0}]}
    for given in (probe, None):
        metrics = contract_metrics(knowledge_results(), given)
        validated = [DayMetric.model_validate(m) for m in metrics]
        assert len({(m.group, m.metric, m.variant) for m in validated}) == len(validated)
        assert not any('@' in m.metric for m in validated)
        by_key = {(m.group, m.metric, m.variant): m for m in validated}
        assert {m.variant for m in validated if m.metric == 'harness_pass_rate'} == {'基线', '修复后'}
        assert {m.variant for m in validated if m.metric == 'learning_agent_share'} == {'批次1', '批次2', '批次3'}
        assert all(m.value is None for m in validated if m.variant)  # 没有真实产物就是 null，不用 synthetic 冒充
        assert by_key[('knowledge', 'doc_qa_accuracy', None)].value is not None
        assert (by_key[('coordinated', 'orchestration_latency_s', None)].value == (4.0 if given else None))


def _report(coordinated_cache, coordinated_gap):
    groups = [{'group': 'coordinated', 'comparable': True, 'cache': cache_status(*coordinated_cache),
               'coverage_gap': coordinated_gap}]
    return {'scenario': 'demo', 'groups': groups,
            'metrics': [{'group': 'coordinated', 'metric': 'electricity_cost', 'value': 1.0, 'unit': 'CNY', 'note': None}]}


def test_cold_collection_is_not_reported_as_fallback(tmp_path):
    """冷跑：20 次未命中随后都采集成功，没有走回退，提示语不能写成回退到确定性实现。"""
    write_report(_report((0, 20, 20), {'deterministic_fallbacks': 0, 'cache_misses': 0}), tmp_path)
    note = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))['coverage_note']
    assert 'fell back' not in note and 'No deterministic fallbacks' in note


def test_uncollected_misses_are_reported_as_fallback(tmp_path):
    write_report(_report((10, 3, 0), {'deterministic_fallbacks': 3, 'cache_misses': 3}), tmp_path)
    note = json.loads((tmp_path / 'comparison.json').read_text(encoding='utf-8'))['coverage_note']
    assert 'fell back to the offline deterministic implementation: coordinated' in note
