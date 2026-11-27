"""Offline scenario comparison assembled from the B17 runner and its trace."""
from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from plantkg.core.contracts import Interval, SimAction
from plantkg.evaluation.cache import ResultCache, fingerprint, install_cache
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.dispatch import DirectDispatcher
from plantkg.world.scenario.experts import EXPERTS
from plantkg.world.scenario.runner import ScenarioRunner, default_registry
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[2]


def knowledge_results(b32_path=None):
    """Read separately scoped evidence; never relabel a baseline as B30 output."""
    qa_path = ROOT / 'plantkg/knowledge/evidence/results/results.json'
    ontology_path = ROOT / 'plantkg/evaluation/ontology/demo_output/results.json'
    qa = json.loads(qa_path.read_text(encoding='utf-8'))
    ontology = json.loads(ontology_path.read_text(encoding='utf-8'))
    records = [
        {'metric': 'cross_document_qa', 'value': qa['summary'], 'source': str(qa_path.relative_to(ROOT)),
         'nature': 'synthetic', 'sample_count': len(qa['questions']), 'version': qa['version'], 'scope': qa['mode']},
        {'metric': 'ontology_entity_f1', 'value': ontology['entities']['f1'], 'source': str(ontology_path.relative_to(ROOT)),
         'nature': ontology['data_kind'], 'sample_count': ontology['items'], 'version': ontology['run_id'],
         'scope': 'B48 synthetic ontology baseline; not B30 rule extraction'},
        {'metric': 'ontology_relation_f1', 'value': ontology['relations']['f1'] if sum(ontology['relations'][k] for k in ('tp', 'fp', 'fn')) else None,
         'source': str(ontology_path.relative_to(ROOT)), 'nature': ontology['data_kind'], 'sample_count': sum(ontology['relations'][k] for k in ('tp', 'fp', 'fn')),
         'version': ontology['run_id'], 'scope': 'B48 synthetic baseline; no relation denominator means unavailable'},
        {'metric': 'ontology_review_curves', 'value': ontology['review_curves'], 'source': str(ontology_path.relative_to(ROOT)),
         'nature': ontology['data_kind'], 'sample_count': ontology['items'], 'version': ontology['run_id'], 'scope': 'B48 synthetic baseline'},
        {'metric': 'asset_activation', 'value': None, 'source': 'B26', 'nature': None, 'sample_count': None,
         'version': None, 'scope': 'unavailable'},
        {'metric': 'rule_extraction_f1', 'value': None, 'source': 'B48', 'nature': None, 'sample_count': None,
         'version': None, 'scope': 'unavailable'},
    ]
    if b32_path:
        data = json.loads(Path(b32_path).read_text(encoding='utf-8'))
        records.append({'metric': 'correction_before_after', 'value': data, 'source': str(b32_path),
                        'nature': data.get('nature'), 'sample_count': data.get('sample_count'),
                        'version': data.get('versions'), 'scope': 'B32 supplied artifact'})
    else:
        records.append({'metric': 'correction_before_after', 'value': None, 'source': 'B32', 'nature': None,
                        'sample_count': None, 'version': None, 'scope': 'pending_dependency'})
    return records


class IdleDispatcher(DirectDispatcher):
    """No event response or expert dispatch; scenario operator actions still apply."""
    def admit(self, event):
        pass

    def confirm(self, event_id, at):
        pass

    def close(self, event_id, at, summary):
        pass

    def redispatch(self, at):
        pass


class TerminalEnergyRunner(ScenarioRunner):
    """Restore the common terminal target with metered, capacity-limited charging."""
    def step(self):
        remaining = (self.world.scenario.end - self.state.at).total_seconds() / 60
        target = self.run_info.energy_end_target_kwh
        if target is not None and 0 < remaining <= 120 and self.energy < target - 1e-7:
            power = min(self.world.charge_kw, (target - self.energy) * 60 / (remaining * self.world.storage.eta_c))
            self.apply(SimAction(entity_id=self.world.index.battery_id, command="charge",
                                 params={"power_kw": power},
                                 interval=Interval(start=self.state.at, end=self.state.at + timedelta(minutes=1))),
                       {"policy": "b33_terminal_energy"})
        super().step()


class BaselineRunner(TerminalEnergyRunner):
    def _dispatcher(self, group):
        return IdleDispatcher(self, self.world.scenario.dispatch)


def metric(name, group, value, unit, note=None, *, variant=None, nature=None):
    result = {'metric': name, 'group': group, 'value': value, 'unit': unit, 'note': note}
    if variant is not None:
        result['variant'] = variant  # #235：版本/批次维度写在字段里，不再拼进指标名
    if nature is not None:
        result['nature'] = nature
    return result


def contract_metrics(knowledge, probe=None):
    """#235：展示页读取的指标名（登记在 docs/architecture/metrics.md）；产不出真实值的输出 null 加 note，不拿 synthetic 值冒充。"""
    by_name = {item['metric']: item for item in knowledge}
    qa = by_name['cross_document_qa']['value']
    out = []

    def add(group, name, value, unit, note, variant=None, nature=None):
        out.append(metric(name, group, value, unit, note, variant=variant, nature=nature))

    for name, source, reason in (
            ('asset_activation_rate', 'asset_activation', 'unavailable: B26 activation has no measured artifact; n=0'),
            ('standard_alignment_rate', None, 'unavailable: alignment is computed per onboarding run, no B33 artifact; n=0'),
            ('ontology_rule_f1', 'rule_extraction_f1', 'unavailable: B30 rule extraction not measured; B48 entity F1 is a separate metric; n=0'),
            ('build_efficiency_min_per_1k', None, 'unavailable: no measured artifact; n=0')):
        add('knowledge', name, by_name[source]['value'] if source else None,
            'min/1k' if name.startswith('build') else 'ratio', reason)
    for name, mode in (('doc_qa_accuracy', 'graph-first'), ('doc_qa_accuracy_rag', 'rag')):
        summary = qa[mode]
        add('knowledge', name, summary['correct'] / summary['questions'] if summary['questions'] else None, 'ratio',
            f'B31 synthetic {mode}; n={summary["questions"]}', nature='synthetic')
    routes = {r['route']: r for r in (probe or {}).get('routes', ())}
    pending = 'unavailable: run with --orchestration; n=0'
    for name, field, unit, scale in (('orchestration_accuracy', 'routing_accuracy', 'ratio', 1),
                                      ('orchestration_evidence_rate', 'evidence_completeness', 'ratio', 1),
                                      ('orchestration_latency_s', 'mean_wall_ms', 's', 1000)):
        for group, route, label in (('baseline', 'A_programmatic', 'baseline = all-programmatic dispatch'),
                                    ('coordinated', 'B_fast_slow', 'coordinated = fast/slow dual channel')):
            row = routes.get(route)
            add(group, name, row[field] / scale if row else None, unit,
                f'D54 synthetic probe; {label}; n={row["count"]}' if row else pending,
                nature='synthetic' if row else None)
    row = routes.get('B_fast_slow')
    add('coordinated', 'orchestration_cases', row['count'] if row else None, 'count',
        'D54 synthetic probe: unseen event cases' if row else pending, nature='synthetic' if row else None)
    cost = 'unavailable: no Harness regression artifact (needs cost authorization, #244); n=0'
    for variant in ('基线', '修复后'):
        add('coordinated', 'harness_pass_rate', None, 'ratio', cost, variant=variant)
    for variant in ('批次1', '批次2', '批次3'):
        add('knowledge', 'learning_agent_share', None, 'ratio', 'unavailable: no per-batch artifact; n=0', variant=variant)
    return out


def extract(trace, group, runner):
    ledger = trace.ledger_summary()
    energy = ledger['energy_kwh']
    pv = energy['pv_available']
    result = [
        metric('electricity_cost', group, ledger['cost_cny'], 'CNY', 'whole scenario window'),
        metric('peak_import', group, ledger['peak_minute_kw'], 'kW', 'maximum minute import'),
        metric('pv_self_consumption', group, energy['pv_used'] / pv if pv else None, 'ratio', 'used / available' if pv else 'zero denominator'),
        metric('pv_curtailed', group, energy['pv_curtailed'], 'kWh'),
        metric('load', group, energy['load'], 'kWh', 'minute ledger, whole scenario window'),
        metric('capacity_violation_minutes', group, ledger['capacity_violation_minutes'], 'min'),
        metric('ledger_unbalanced_minutes', group, ledger['minutes'] - ledger['minutes_balanced'], 'min'),
        metric('actions', group, sum(1 for a in trace.actions if a.get('executed', True) and not a['source'].get('policy')), 'count',
               'excludes terminal-energy charging'),
        metric('terminal_charge_minutes', group, sum(1 for a in trace.actions if a['source'].get('policy') == 'b33_terminal_energy'), 'min'),
        metric('work_orders', group, len(trace.work_orders), 'count'),
        metric('automatic_correctness', group, None, 'ratio', 'unavailable: no independent ground truth; n=unknown'),
        metric('correction_before_after', group, None, 'ratio', 'pending_dependency: B32; n=unknown'),
        *[metric(name, group, None, unit, reason) for name, unit, reason in (
            ('elevator_mean_wait', 's', 'unavailable: B17 queue model does not record passenger count; n=unknown'),
            ('elevator_max_wait', 's', 'unavailable: B17 queue model does not record individual waits; n=unknown'),
        )],
    ]
    elevator = runner.state.sims.get('elevator', {})
    thermal = runner.state.sims.get('thermal', {})
    comfort = thermal.get('comfort_minutes', {})
    result.extend([
        metric('congestion_duration', group, elevator.get('congestion_min'), 'min', 'B20 simulated queue threshold'),
        metric('ordinary_temperature_violation', group, sum(comfort.get(z, 0) for z in ('admin', 'production')) if comfort else None,
               'zone-min', 'B21 configured comfort bands; zone minutes'),
        metric('cleanroom_temperature_violation', group, comfort.get('cleanroom'), 'min', 'B21 configured hard band'),
        metric('hard_temperature_violation', group, sum(comfort.get(z, 0) for z in thermal.get('hard_zones', [])) if comfort else None,
               'zone-min', 'B21 configured hard bands'),
        metric('human_interruptions', group, len(trace.work_orders), 'count', 'work orders created; questions not separately recorded'),
    ])
    from plantkg.agent.execution_policy.store import ExecutionStore
    tasks = ExecutionStore(runner.db.writer, runner.world.site.plant_id).tasks()
    done = [(t.done_at - t.created_at).total_seconds() / 60 for t in tasks
            if t.work_order_id in trace.work_orders and t.done_at is not None]
    latency = sorted(r.timing.latency_ms for r in trace.agent_runs
                     if r.source == 'nexent' and r.timing.latency_ms is not None)
    def percentile(p):
        return latency[max(0, math.ceil(p * len(latency)) - 1)] if latency else None
    result.extend([
        metric('work_order_completion', group, sum(done) / len(done) if done else None, 'min',
               f'B47 done_at minus created_at; completed n={len(done)} / orders={len(trace.work_orders)}'),
        metric('nexent_latency_p50', group, percentile(.5), 'ms',
               f'expert, triage and investigate collections are timed; replayed cache hits are not; n={len(latency)} collections'),
        metric('nexent_latency_p95', group, percentile(.95), 'ms',
               f'expert, triage and investigate collections are timed; replayed cache hits are not; n={len(latency)} collections'),
    ])
    response = []
    automated = 0
    for tid, ticket in trace.tickets.items():
        origin = ticket.get('origin_event_at')
        starts = [datetime.fromisoformat(a['action']['interval']['start']) for a in trace.actions
                  if a['source'].get('ticket_id') == tid and a.get('executed', True)]
        orders = [w.created_at for w in trace.work_orders.values() if w.ticket_id == tid]
        if starts and not orders:
            automated += 1
        if origin and (starts or orders):
            response.append((min(starts + orders) - datetime.fromisoformat(origin)).total_seconds() / 60)
    result.extend([
        metric('event_response', group, sum(response) / len(response) if response else None, 'min', f'first action or order; n={len(response)}'),
        metric('automatic_handling', group, automated / len(trace.tickets) if trace.tickets else None, 'ratio',
               f'tickets with action and no order / all tickets; n={len(trace.tickets)}'),
    ])
    return result


def compare(groups):
    """Require declared D05 run provenance and terminal energy in every group."""
    signatures = [g['data_sources'] for g in groups]
    same_sources = bool(signatures[0]) and all(s == signatures[0] for s in signatures)
    same_target = all(g['energy_end_target_kwh'] == groups[0]['energy_end_target_kwh'] for g in groups)
    same_start = all(g['energy_start_kwh'] == groups[0]['energy_start_kwh'] for g in groups)
    for g in groups:
        reasons = []
        if not g['data_sources']:
            reasons.append('RunInfo.data_sources missing')
        elif not any(s.get('source', '').lower() == 'd05' and s.get('dataset_version') for s in g['data_sources']):
            reasons.append('D05 provenance not established')
        if any(s.get('source', '').lower() == 'hipe' for s in g['data_sources']):
            reasons.append('HIPE fallback is not comparable')
        if g.get('fork_source_mismatch'):
            reasons.append('run-chain data sources differ')
        if not same_sources:
            reasons.append('data sources missing or differ across groups')
        if not same_target:
            reasons.append('terminal energy targets differ')
        if not same_start:
            reasons.append('initial energy differs')
        if g['energy_end_kwh'] is None or g['energy_end_kwh'] + 1e-6 < g['energy_end_target_kwh']:
            reasons.append('terminal energy below target or unavailable')
        g['incomparable_reasons'] = reasons
        g['comparable'] = not reasons
    return groups


def cache_state(state):
    """State for the cache key without the per-run identities main #186 adds.

    #186 stamps the run id into sims.sensors and into every sensor snapshot, so the
    live state differs on each invocation and a replay would miss every key. Deep
    copy first: to_json() exposes the live structures, so popping them in place
    would break the sensor routing of the run still in progress.
    """
    data = copy.deepcopy(state.to_json())
    sensors = data.get('sims', {}).get('sensors') or {}
    sensors.pop('run_id', None)
    if isinstance(sensors.get('latest'), dict):
        sensors['latest'].pop('run_id', None)
    return data


def cache_status(hits, misses, collected):
    """每组的缓存状态：cold 没有命中；replay 全部命中；mixed 命中与采集或缺失并存；none 没有缓存调用。"""
    lookups = hits + misses + collected
    mode = ('none' if not lookups else 'cold' if not hits else 'replay' if not (misses or collected) else 'mixed')
    return {'hits': hits, 'misses': misses, 'collected': collected,
            'hit_rate': hits / (hits + misses) if hits + misses else None, 'mode': mode}


def evaluate(path, output, seed=None, cache_version='v1', b32_path=None, *, collector=None, refresh=False, model_version='', agents_dir=None, probe=None):
    scenario = load_scenario(str(path))
    if seed is not None:
        scenario = scenario.model_copy(update={'seed': seed})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    groups, metrics = [], []
    prompt_root = Path(agents_dir).resolve() if agents_dir else ROOT / 'nexent_assets/agents'
    agent_prompts = {f'agents/{p.name}': p.read_text(encoding='utf-8') for p in sorted(prompt_root.glob('*.yaml'))}
    skills = {str(p.relative_to(ROOT)): p.read_text(encoding='utf-8') for p in sorted((ROOT / 'nexent_assets/skills').glob('*/SKILL.md'))}
    prompt_version = fingerprint(agent_prompts | skills)
    target = scenario.initial.energy_end_target_kwh
    # E_ref defaults to the common initial energy when not configured.
    first_world = build_world(scenario)
    if target is None:
        target = scenario.initial.soc * first_world.storage.capacity_kwh
        scenario = scenario.model_copy(update={'initial': scenario.initial.model_copy(update={'energy_end_target_kwh': target})})
    for group in ('baseline', 'independent', 'coordinated'):
        world = build_world(scenario)
        # Fresh database guarantees group and invocation isolation.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='b33-') as tmp:
            db = open_db(Path(tmp) / 'scenario.db', world.site.plant_id)
            try:
                registry = default_registry()
                runner_cls = BaselineRunner if group == 'baseline' else TerminalEnergyRunner
                runner = runner_cls(world, db, registry=registry, mode='evaluation', group='independent' if group == 'baseline' else group)
                def cache_context(binding):
                    ticket = runner.tickets.tickets.get(binding.ticket_id)
                    event = ticket.event if ticket is not None else {}
                    return event.get('event_id'), {'world': cache_state(runner.state), 'energy_kwh': runner.energy,
                                                   'knowledge': sorted(runner.knowledge)}
                def cache_proposal(proposal_id):
                    return runner.dispatcher.router.sink.get(proposal_id)
                cache = ResultCache(output / 'agent-cache.json', cache_version)
                sources = install_cache(registry, cache, {'scenario': fingerprint({'sha256': scenario.sha256, 'seed': scenario.seed, 'group': group}), 'prompt_version': prompt_version,
                    'data_version': fingerprint([x.model_dump(mode='json') for x in world.inputs.data_source_refs()]), 'resolve': cache_context,
                    'collector': collector, 'refresh': refresh, 'model_version': model_version,
                    'resolve_proposals': cache_proposal},
                    list(dict.fromkeys([*EXPERTS, 'expert', 'triage', 'investigate', 'arbitration', 'follow_up'])))
                trace = runner.run()
                info = runner.run_info
                entries = trace.ledger
                groups.append({'group': group, 'run_id': trace.run_id, 'data_sources': [x.model_dump(mode='json') for x in info.data_sources],
                               'baseline_policy': 'no_event_actions' if group == 'baseline' else None,
                               'fork_source_mismatch': any(db.runs.get_run(x.run_id).data_sources != info.data_sources for x in trace.runs),
                               'energy_start_kwh': entries[0].entry.energy_start_kwh if entries else None,
                               'energy_end_kwh': entries[-1].entry.energy_end_kwh if entries else None,
                               'energy_end_target_kwh': target, 'actions': len(trace.actions), 'agent_runs': len(trace.agent_runs),
                               # 顶层 cache_misses 是全部未命中（含随后采集成功的）；coverage_gap.cache_misses 才是实际走回退的次数
                               'cache_hits': sum(x.hits for x in sources), 'cache_misses': sum(x.misses for x in sources),
                               'cache_collected': sum(x.collected for x in sources),
                               'cache': cache_status(sum(x.hits for x in sources), sum(x.misses for x in sources),
                                                     sum(x.collected for x in sources)),
                               'coverage_gap': {'deterministic_fallbacks': sum(1 for r in trace.agent_runs if r.coverage_gap),
                                                'cache_misses': sum(x.fallbacks for x in sources)},
                               'cache_miss_details': [{'source_key': x.registry_key, 'reasons': dict(x.miss_reasons)}
                                                      for x in sources if x.miss_reasons],
                               'cache_miss_differences': [difference for source in sources for difference in source.miss_differences],
                               'collection_failures': sum(x.collection_failures for x in sources),
                               'collection_errors': [error for source in sources for error in source.collection_errors],
                               'cache_version': cache_version})
                metrics.extend(extract(trace, group, runner))
            finally:
                db.close()
    compare(groups)
    details = [{'metric': m['metric'], 'group': m['group'], 'source':
                ('B17 minute ledger' if m['metric'] in {'electricity_cost', 'peak_import', 'pv_self_consumption',
                                                        'pv_curtailed', 'load', 'capacity_violation_minutes',
                                                        'ledger_unbalanced_minutes'} else 'scenario trace'),
                'nature': 'simulated', 'unit': m['unit'], 'note': m['note']}
               for m in metrics]
    knowledge = knowledge_results(b32_path)
    qa = next(item for item in knowledge if item['metric'] == 'cross_document_qa')['value']
    ontology = {item['metric']: item for item in knowledge}
    for group in ('baseline', 'independent', 'coordinated'):
        for mode in ('graph-first', 'rag'):
            summary = qa[mode]
            for field in ('correct', 'complete'):
                metrics.append(metric(f'knowledge_{mode}_{field}', group,
                                      summary[field] / summary['questions'] if summary['questions'] else None,
                                      'ratio', f'B31 synthetic {mode}; n={summary["questions"]}; same artifact for all groups'))
        for name in ('ontology_entity_f1', 'ontology_relation_f1', 'asset_activation', 'rule_extraction_f1'):
            item = ontology[name]
            metrics.append(metric('knowledge_' + name, group, item['value'], 'ratio',
                                  f'{item["source"]}; {item["scope"]}; n={item["sample_count"]}'))
        metrics.append(metric('knowledge_build_efficiency', group, None, 'ratio', 'unavailable: no measured artifact; n=0'))
    named = contract_metrics(knowledge, probe)
    metrics.extend(named)
    for m in metrics:
        if not m['metric'].startswith('knowledge_'):
            continue
        name = m['metric'].removeprefix('knowledge_')
        artifact = ontology.get(name) or (ontology['cross_document_qa'] if name.startswith(('graph-first_', 'rag_')) else None)
        details.append({'metric': m['metric'], 'group': m['group'], 'source': artifact['source'] if artifact else None,
                        'nature': artifact['nature'] if artifact else None, 'unit': m['unit'], 'note': m['note'],
                        'sample_count': artifact['sample_count'] if artifact else None,
                        'version': artifact['version'] if artifact else None})
    for m in named:
        details.append({'metric': m['metric'], 'group': m['group'], 'variant': m.get('variant'),
                        'source': ('D54 orchestration probe' if m['metric'].startswith('orchestration_') else
                                   'B31 cross-document QA' if m['metric'].startswith('doc_qa_') else 'unavailable'),
                        'nature': m.get('nature'), 'unit': m['unit'], 'note': m['note']})
    return {'schema_version': '1', 'scenario': scenario.scenario_id, 'scenario_sha256': scenario.sha256,
            'seed': scenario.seed, 'days': scenario.days, 'nature': 'simulated', 'scenario_nature': scenario.nature,
            'prompt_version': prompt_version,
            'groups': groups, 'metrics': metrics, 'metric_details': details, 'knowledge_metrics': knowledge}
