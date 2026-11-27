"""Render the offline measurements without ranking incomparable runs."""
from __future__ import annotations

import html
import json
from pathlib import Path


_DISPLAY_ONLY = {'asset_activation_rate', 'standard_alignment_rate', 'ontology_rule_f1', 'build_efficiency_min_per_1k',
                 'harness_pass_rate', 'learning_agent_share'}


def _coverage_note(groups):
    """覆盖不足只提示，不改可比性判定、不设阈值。"""
    groups = [g for g in groups if 'cache' in g and 'coverage_gap' in g]
    modes = ', '.join(f"{g['group']}={g['cache']['mode']}" for g in groups)
    fell_back = [g['group'] for g in groups if g['coverage_gap']['cache_misses']]
    deterministic = [g['group'] for g in groups if g['coverage_gap']['deterministic_fallbacks']]
    note = f'cache mode: {modes}.'
    if fell_back:
        note += (f' Groups that missed the cache and fell back to the offline deterministic implementation: {", ".join(fell_back)}.')
    if deterministic:
        note += (f' Groups with runs recorded as deterministic (offline deterministic results, not Nexent output): {", ".join(deterministic)}.')
    if not fell_back and not deterministic:
        note += ' No deterministic fallbacks or cache misses.'
    return note + ' Comparability rules are unchanged.'


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # #238：config 组是配置限值（nature=configured），不是组结果，不参与改善率比较。
    # #235：带 variant 的指标与 B33 之外的展示指标（orchestration_/doc_qa_ 等）不属于三组改善率比较。
    compared = [m for m in report['metrics'] if m['group'] != 'config' and m.get('variant') is None
                and not m['metric'].startswith(('knowledge_', 'orchestration_', 'doc_qa_'))
                and m['metric'] not in _DISPLAY_ONLY]
    values = {(m['group'], m['metric']): m['value'] for m in compared}
    eligible = {g['group'] for g in report['groups'] if g['comparable']}
    comparisons = []
    for group in ('independent', 'coordinated'):
        for name in sorted({m['metric'] for m in compared}):
            baseline, candidate = values.get(('baseline', name)), values.get((group, name))
            if {'baseline', group} <= eligible and baseline is not None and candidate is not None:
                comparisons.append({'group': group, 'metric': name, 'delta': candidate - baseline,
                                    'percent': (candidate - baseline) / baseline * 100 if baseline else None})
    comparison = {'scenario': report['scenario'], 'groups': report['groups'],
                  'cache': {g['group']: g['cache'] for g in report['groups'] if 'cache' in g},
                  'coverage_gap': {g['group']: g['coverage_gap'] for g in report['groups'] if 'coverage_gap' in g},
                  'coverage_note': _coverage_note(report['groups']), 'comparisons': comparisons}
    (output / 'comparison.json').write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = []
    for g in report['groups']:
        value = next(m['value'] for m in report['metrics'] if m['metric'] == 'electricity_cost' and m['group'] == g['group'])
        rows.append((g['group'], value, g['comparable']))
    scale = max((v or 0 for _, v, _ in rows), default=1) or 1
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="760" height="210" viewBox="0 0 760 210">',
           '<rect width="760" height="210" fill="white"/>',
           '<text x="15" y="25" font-size="16">Scenario window electricity cost (CNY); incomparable runs are not ranked</text>']
    for index, (group, value, comparable) in enumerate(rows):
        y = 55 + index * 45
        svg.append(f'<text x="15" y="{y+14}" font-size="14">{html.escape(group)}</text>')
        svg.append(f'<rect x="140" y="{y}" width="{(value or 0)/scale*430:.1f}" height="20" fill="#5484a4"/>')
        svg.append(f'<text x="580" y="{y+14}" font-size="13">{value} {"" if comparable else "incomparable"}</text>')
    svg.append('</svg>')
    (output / 'cost.svg').write_text('\n'.join(svg), encoding='utf-8')
