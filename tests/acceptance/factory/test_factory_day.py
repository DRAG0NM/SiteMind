"""Check two previously generated B36 runs; set B36_RUN_A and B36_RUN_B."""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from plantkg.contracts.validate import validate, validate_chain_file
from scripts.demo_factory import is_display_metric


@pytest.fixture(scope='module')
def outputs():
    paths = [os.environ.get('B36_RUN_A'), os.environ.get('B36_RUN_B')]
    if not all(paths):
        pytest.skip('set B36_RUN_A and B36_RUN_B to generated output directories')
    return [Path(p) for p in paths]


@pytest.fixture(scope='module')
def real_output():
    path = os.environ.get('B36_REAL_RUN')
    if not path:
        pytest.skip('set B36_REAL_RUN to a real-path run, without the #260 substitute')
    directory = Path(path)
    if (directory / 'mock-issue-260.json').exists():
        pytest.fail('a mock run cannot be used for real #260 acceptance')
    return directory


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def canonical_business(day, key, correction):
    """Normalize only run/version IDs, proposal allocation and wall-clock timing.

    Proposal numbers may depend on concurrent expert completion; replace them
    with their ticket, expert and round, keeping all business values and links.
    """
    replacements = {run['run_id']: f'run[{i}]' for i, run in enumerate(day['runs'])}
    replacements.update({version['version_manifest_id']: f'manifest[{i}]'
                         for i, version in enumerate(day['knowledge_versions'])})
    replacements[correction['candidate']['candidate_id']] = 'correction-candidate'
    replacements[correction['backtest']['report_id']] = 'correction-report'
    replacements[correction['confirmation']['action_id']] = 'correction-approval'
    for order in day['work_orders']:
        if order.get('pending_action_id'):
            replacements[order['pending_action_id']] = f"approval[{order['work_order_id']}]"
    labels = set()
    for proposal in day['proposals']:
        binding = proposal.get('binding') or {}
        if proposal.get('proposal_id'):
            label = f"proposal[{proposal['ticket_id']}:{proposal['expert']}:{binding.get('round')}:{binding.get('knowledge_version')}]"
            assert label not in labels, 'ambiguous proposal normalization'
            labels.add(label)
            replacements[proposal['proposal_id']] = label
    excluded = {'requested_at', 'returned_at', 'latency_ms'}

    def clean(value):
        if isinstance(value, dict):
            return {clean(k): clean(v) for k, v in value.items() if k not in excluded}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
        return value

    return sorted((json.dumps(clean(item), ensure_ascii=False, sort_keys=True) for item in day[key]))


def test_visitor_instruction_reaches_device_and_replay():
    path = os.environ.get('B36_RUN_A')
    if not path:
        pytest.skip('set B36_RUN_A to the offline demo output')
    out = Path(path)
    day, trace = read(out / 'day.json'), read(out / 'trace.json')
    event, = [e for e in day['events'] if e['event_type'] == 'operator_instruction']
    assert event['at'] == '2026-07-01T04:30:00Z'
    rules = [r for r in day['temp_rules'] if r['source_event_id'] == event['event_id']]
    assert len(rules) == 1, 'the 12:30 instruction must produce one replay rule'
    rule, = rules
    admin = 'entity://demo-electronics/zone/admin'
    window = {'start': '2026-07-01T05:00:00Z', 'end': '2026-07-01T09:00:00Z'}
    assert rule['effective'] == window and rule['entity_ids'] == [admin]
    assert rule['created_at'] == event['at'] and rule['estimated_cost_cny'] > 0
    assert trace['temp_rules'][rule['rule_id']] == rule
    action, = [a for a in trace['actions'] if a['source'].get('temp_rule') == rule['rule_id']]
    assert action['executed'] and action['source']['event_id'] == event['event_id']
    assert action['action']['command'] == 'set_setpoint' and action['action']['entity_id'] == admin
    assert action['action']['interval'] == window and action['action']['params']['setpoint_c'] == 24

    frames = {frame['at']: frame['devices'][admin] for frame in day['state_series']}
    active = [state for at, state in frames.items() if window['start'] <= at < window['end']]
    assert len(active) == 16 and all(s['setpoint'] == 24 and s['temp_rule'] == rule['rule_id'] for s in active)
    before, after = frames['2026-07-01T04:45:00Z'], frames[window['end']]
    assert before['setpoint'] == after['setpoint'] != 24
    assert before['temp_rule'] is after['temp_rule'] is None
    with sqlite3.connect((out / 'scenario.db').resolve().as_uri() + '?mode=ro', uri=True) as con:
        rows = con.execute('SELECT run_id,status,estimated_cost_cny FROM b47_temp_rules WHERE source_event_id=?',
                           (event['event_id'],)).fetchall()
    assert rows == [(day['runs'][-1]['run_id'], 'expired', rule['estimated_cost_cny'])]


def test_two_day_export_and_repeatability(outputs):
    corrections = [read(path / 'correction.json') for path in outputs]
    for filename in ('day.json', 'day-2026-07-02.json'):
        a, b = (read(path / filename) for path in outputs)
        for directory, day in zip(outputs, (a, b)):
            assert validate(directory / filename) == []
            assert len(day['ledger_minutes']) == 1440 and len(day['state_series']) == 96
            assert len({m['interval']['start'] for m in day['ledger_minutes']}) == 1440
            assert day['runs'][-1]['run_id'] == day['run_id']
            assert validate_chain_file(directory / 'scenario.db', day['run_id']) == []
            assert day['agent_runs'] and all(q['answer'].startswith('【模拟预设回答】') for q in day['qa'])
            assert day['cache_version'] == 'v1'
        # These are business records without run UUIDs or wall-clock timestamps.
        for key in ('events', 'state_series', 'ledger_minutes'):
            assert a[key] == b[key], key
        for key in ('tickets', 'proposals', 'decisions', 'work_orders', 'qa', 'knowledge_versions', 'runs'):
            assert canonical_business(a, key, corrections[0]) == canonical_business(b, key, corrections[1]), key
        assert [(m['metric'], m['group'], m['value']) for m in a['metrics']] == [
            (m['metric'], m['group'], m['value']) for m in b['metrics']]
    first, second = (read(outputs[0] / f) for f in ('day.json', 'day-2026-07-02.json'))
    assert {'act1-elevator', 'act1-spike', 'act4-combo'} <= {e['event_id'] for e in first['events']}
    assert 'day2-combo' in {e['event_id'] for e in second['events']}
    assert any(r['forked_at'] and r['forked_at'].startswith('2026-07-01T08:10') for r in first['runs'])
    assert {m['group'] for m in first['metrics']} >= {'baseline', 'independent', 'coordinated'}
    for key in ('request', 'rules', 'checks', 'knowledge_version', 'fork_at'):
        assert corrections[0][key] == corrections[1][key], key
    for key in ('seed', 'split_ids', 'metrics', 'constraint_violations', 'failures', 'thresholds', 'verdict', 'scope'):
        assert corrections[0]['backtest'][key] == corrections[1]['backtest'][key], key
    policies = []
    for directory, correction in zip(outputs, corrections):
        digest = correction['candidate']['changes']['policy']['ref'].removeprefix('policy:')
        with sqlite3.connect((directory / 'scenario.db').resolve().as_uri() + '?mode=ro', uri=True) as con:
            policy = json.loads(con.execute('SELECT body FROM b32_policy_artifacts WHERE digest=?', (digest,)).fetchone()[0])
        policy.pop('policy_version')  # derived identifier includes the run's manifest UUID
        policies.append(policy)
    assert policies[0] == policies[1], 'published policy semantics'
    traces = [read(path / 'trace.json') for path in outputs]
    full_days = []
    for directory in outputs:
        day = read(directory / 'day.json')
        second_day = read(directory / 'day-2026-07-02.json')
        day['proposals'] += second_day['proposals']
        day['work_orders'] += second_day['work_orders']
        full_days.append(day)
    for key in ('actions', 'admissions', 'executions'):
        for day, trace in zip(full_days, traces):
            day[key] = trace['actions'] if key == 'actions' else [n for n in trace['notes'] if
                n['kind'] == ('admission' if key == 'admissions' else 'b47_execution')]
        assert canonical_business(full_days[0], key, corrections[0]) == canonical_business(
            full_days[1], key, corrections[1]), key


def test_agent_run_full_repeatability(outputs):
    """Report #262 as xfail only after proving it is solely text ordering."""
    def verify_same_minute(directory, answer, trigger):
        minute = datetime.fromisoformat(trigger.replace('Z', '+00:00')).replace(second=0, microsecond=0)
        with sqlite3.connect((directory / 'scenario.db').resolve().as_uri() + '?mode=ro', uri=True) as con:
            for clause in re.split(r'；(?=ticket://)', answer):
                ticket, separator, summary = clause.partition('：')
                assert separator and ticket.startswith('ticket://'), clause
                rows = con.execute('SELECT at,record FROM b27_pending WHERE ticket_id=?', (ticket,)).fetchall()
                assert any(datetime.fromisoformat(at.replace('Z', '+00:00')).replace(second=0, microsecond=0) == minute
                           and json.loads(record).get('summary') == summary for at, record in rows), clause

    differences = []
    corrections = [read(path / 'correction.json') for path in outputs]
    for filename in ('day.json', 'day-2026-07-02.json'):
        a, b = (read(path / filename) for path in outputs)
        records = [canonical_business(day, 'agent_runs', correction)
                   for day, correction in zip((a, b), corrections)]
        first, second = ({item['agent_run_id']: item for item in map(json.loads, rows)} for rows in records)
        assert len(first) == len(records[0]) and len(second) == len(records[1]), 'duplicate agent run IDs'
        assert first.keys() == second.keys()
        for identity in first:
            x, y = first[identity], second[identity]
            if x == y:
                continue
            assert x['kind'] == y['kind'] == 'triage', (identity, x, y)
            answer_x, answer_y = x['final_answer'], y['final_answer']
            assert {k: v for k, v in x.items() if k != 'final_answer'} == {
                k: v for k, v in y.items() if k != 'final_answer'}, (identity, x, y)
            assert sorted(re.split(r'；(?=ticket://)', answer_x)) == sorted(re.split(r'；(?=ticket://)', answer_y)), (identity, answer_x, answer_y)
            verify_same_minute(outputs[0], answer_x, x['binding']['trigger_at'])
            verify_same_minute(outputs[1], answer_y, y['binding']['trigger_at'])
            differences.append((filename, identity, answer_x, answer_y))
    if differences:
        pytest.xfail('Issue #262 尚未修复，完整业务记录确定性验收未通过，当前差异仅限同分钟合批顺序。'
                     + repr(differences))


def test_fourth_act_requires_real_automatic_execution(real_output):
    from plantkg.core.contracts import BacktestReport, Candidate, PendingAction, PolicyConfig, Rule

    proof = read(real_output / 'correction.json')
    candidate = Candidate.model_validate(proof['candidate'])
    report = BacktestReport.model_validate(proof['backtest'])
    approval = PendingAction.model_validate(proof['confirmation'])
    assert proof['scenario_nature'] == proof['reviewer_nature'] == 'synthetic'
    assert proof['business_logic'] == 'B32/B47'
    assert candidate.status == 'published' and report.verdict == 'pass'
    assert candidate.backtest_report_id == report.report_id and report.candidate_id == candidate.candidate_id
    assert candidate.base_manifest_id == report.base_manifest_id == proof['parent_manifest_id']
    assert candidate.publish_action_id == approval.action_id and approval.target_ref == candidate.candidate_id
    assert approval.status == 'confirmed' and approval.decided_by == 'b36-synthetic-reviewer'
    assert proof['fork_at'].startswith('2026-07-01T08:10')
    with sqlite3.connect((real_output / 'scenario.db').resolve().as_uri() + '?mode=ro', uri=True) as con:
        stored_candidate = con.execute('SELECT body FROM b32_candidates WHERE candidate_id=?',
                                       (candidate.candidate_id,)).fetchone()[0]
        assert Candidate.model_validate_json(stored_candidate) == candidate
        stored_report = con.execute('SELECT body FROM b32_reports WHERE report_id=?', (report.report_id,)).fetchone()[0]
        assert BacktestReport.model_validate_json(stored_report) == report
        policy_ref = candidate.changes['policy']
        policy = PolicyConfig.model_validate_json(con.execute('SELECT body FROM b32_policy_artifacts WHERE digest=?',
            (policy_ref.ref.removeprefix('policy:'),)).fetchone()[0])
        assert policy.autonomy_levels['unknown_combo'] == 'L0'
        assert policy.autonomy_levels['unknown_combo|lighting'] == proof['request']['autonomy_level']
    rules = [Rule.model_validate(rule) for rule in proof['rules']]
    rule = next(rule for rule in rules if rule.kind == 'unknown_combo')
    assert rule.nature == 'synthetic' and rule.review_status == 'approved'
    assert rule.condition == proof['request']['condition']
    trace = read(real_output / 'trace.json')
    first = next(n for n in trace['notes'] if n.get('event_id') == 'act4-combo' and n['kind'] == 'admission')
    assert first['channel'] == 'slow'
    assert not any(a['source'].get('ticket_id') == first['ticket_id'] for a in trace['actions'])
    admission = next(n for n in trace['notes'] if n.get('event_id') == 'day2-combo' and n['kind'] == 'admission')
    ticket_id = admission['ticket_id']
    assert admission['channel'] == 'fast'
    executions = [n for n in trace['notes'] if n.get('ticket_id') == ticket_id and n['kind'] == 'b47_execution']
    second = read(real_output / 'day-2026-07-02.json')
    proposal = next(p for p in second['proposals'] if p['ticket_id'] == ticket_id and p['expert'] == 'equipment')
    assert proposal['binding']['run_id'] == proof['child_run_id']
    assert proposal['binding']['knowledge_version'] == proof['knowledge_version']
    assert f'rule:{rule.rule_id}' in proposal['evidence_refs']
    assert any(n['proposal_id'] == proposal['proposal_id'] and n['level'] == policy.autonomy_levels['unknown_combo|lighting']
               and n['executed'] > 0 for n in executions)
    execution = next(n for n in executions if n['proposal_id'] == proposal['proposal_id'])
    answer = next(q for q in second['qa'] if q['question'] == 'B47 如何判断和执行？')
    assert all(value in answer['answer'] for value in (execution['level'], str(execution['confidence']),
        str(execution['executed']), str(execution['queued']), proposal['proposal_id']))
    action = next(a for a in trace['actions'] if a['source'].get('ticket_id') == ticket_id and a['executed'])
    instant = datetime.fromisoformat(action['action']['interval']['start'].replace('Z', '+00:00'))
    answer_at = datetime.fromisoformat(answer['at'].replace('Z', '+00:00'))
    assert answer_at >= instant
    assert answer_at >= datetime.fromisoformat(proposal['timing']['effective_at'].replace('Z', '+00:00'))
    assert all(answer_at >= datetime.fromisoformat(r['timing']['effective_at'].replace('Z', '+00:00'))
               for r in second['agent_runs'] if proposal['proposal_id'] in r['proposal_ids'] and r['timing']['effective_at'])
    assert not any(q['question'] == answer['question'] and datetime.fromisoformat(q['at'].replace('Z', '+00:00')) < answer_at
                   for q in second['qa'])
    before, after = (next(m for m in second['ledger_minutes'] if datetime.fromisoformat(
        m['interval']['start'].replace('Z', '+00:00')) == at) for at in (instant - timedelta(minutes=1), instant))
    assert after['load_breakdown']['lighting'] > before['load_breakdown']['lighting']
    frame = next(f for f in second['state_series'] if datetime.fromisoformat(f['at'].replace('Z', '+00:00')) >= instant)
    assert frame['devices'][action['action']['entity_id']]['fault'] is False
    assert proof['checks'] == {'new_version_matches': True, 'old_version_matches': False,
        'other_device_matches': False, 'other_condition_matches': False,
        'proposal_authorized': True, 'extra_action_authorized': False}
    assert any(n['kind'] == 'b47_execution' and n['queued'] > 0 for n in trace['notes'])


def test_onboarding_publication_reaches_second_act(real_output):
    proof = read(real_output / 'correction.json')
    records = {r['kind']: r for r in proof['onboarding'] if r['status'] == 'published'}
    assert {'elevator_congestion_rule', 'zone_temperature_limit', 'compressor_pressure_drop'} <= records.keys()
    assert records['elevator_congestion_rule']['at'].startswith('2026-06-30T23:30')
    assert records['zone_temperature_limit']['at'].startswith('2026-06-30T23:30')
    assert records['compressor_pressure_drop']['at'].startswith('2026-07-01T01:30')
    day = read(real_output / 'day.json')
    assert all(any(v['version_manifest_id'] == r['manifest_id'] for v in day['knowledge_versions'])
               for r in records.values())
    event = next(ticket for ticket in day['tickets'] if 'act2-leak' in ticket['event_ids'])
    proposal = next(p for p in day['proposals'] if p['ticket_id'] == event['ticket_id'] and p['expert'] == 'equipment')
    assert 'knowledge:compressor_leak_rule' in proposal['evidence_refs']
    assert '现场缺少泄漏判别规则' not in proposal['diagnosis']
    assert proposal['binding']['knowledge_version'] == records['compressor_pressure_drop']['knowledge_version']




def test_b33_metrics_and_cache_provenance(outputs):
    from plantkg.world.scenario import load_scenario

    scenario = load_scenario('configs/scenarios/factory_demo_day.yaml')
    for directory in outputs:
        report = read(directory / 'evaluation/metrics.json')
        cache = read(directory / 'demo-cache-stats.json')
        day = read(directory / 'day.json')
        assert (directory / 'evaluation/agent-cache.json').exists()
        assert cache['version'] == day['cache_version'] == 'v1'
        assert cache['source'] == 'B36 演示重放缓存' and set(cache['b33_groups']) == {'baseline', 'independent', 'coordinated'}
        assert cache['hits'] >= 0 and cache['misses'] >= 0 and cache['hits'] + cache['misses'] > 0
        assert (report['scenario_sha256'], report['seed'], report['days']) == (scenario.sha256, scenario.seed, scenario.days)
        groups = {g['group']: g for g in report['groups']}
        assert set(groups) == {'baseline', 'independent', 'coordinated'}
        eligible = all(group['comparable'] and group['data_sources'] == day['runs'][-1]['data_sources']
                       for group in groups.values())
        source_metrics = {(m['metric'], m['group'], m.get('variant')): m for m in report['metrics']
                          if m['group'] in groups and not is_display_metric(m['metric'])}
        # 展示页指标（knowledge 组、编排对照、Harness）不进回放日的三组对照
        b33_metrics = [m for m in day['metrics'] if m['group'] in groups]
        assert len(b33_metrics) == len(source_metrics)
        for metric in b33_metrics:
            source = source_metrics[(metric['metric'], metric['group'], metric.get('variant'))]
            assert metric['unit'] == source['unit']
            assert metric['value'] == (source['value'] if eligible else None)
            assert groups[metric['group']]['run_id'] in metric['note']
            assert 'B33 独立两日对照' in metric['note']
