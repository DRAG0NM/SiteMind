"""D54 synthetic unseen-event routing probe using the two scenario dispatchers."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from statistics import mean

from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.runner import ScenarioRunner, default_registry
from plantkg.world.scenario.script import ScriptEvent, load_scenario
from plantkg.evaluation.cache import ResultCache, fingerprint, install_cache
from plantkg.world.scenario.experts import EXPERTS


class TimedRunner(ScenarioRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_elapsed = {}

    def _inject(self, event, at):
        started = time.perf_counter()
        super()._inject(event, at)
        self.event_elapsed[event.id] = (time.perf_counter() - started) * 1000


def run_probe(path, *, collector=None, model_version=''):
    base = load_scenario(str(path))
    # These are configured routing and topology expectations, not observed faults.
    cases = [
        ('AC-1', 'equipment', ('P-AIR', 'TX1'), '空压机振动与电流出现不规则波动'),
        ('AC-2', 'equipment', ('P-AIR', 'TX1'), '空压机排气温度和电压同步异常'),
        ('AHU-P1', 'hvac', ('P-HVAC-P', 'TX1'), '车间空调箱送风量下降且温度升高'),
        ('AHU-P2', 'hvac', ('P-HVAC-P', 'TX1'), '车间空调箱风机运行噪声突然增大'),
        ('FE-1', 'elevator', ('P-LIFT-F', 'TX1'), '货梯开门时间异常并伴随电流波动'),
        ('FE-2', 'elevator', ('P-LIFT-F', 'TX1'), '货梯运行频繁中断但未发现困人'),
        ('LT-P', 'energy', ('P-LT-P', 'TX1'), '生产照明亮度反复降低'),
        ('LT-A03', 'energy', ('P-ADMIN-LT', 'TX2'), '行政楼三层照明间歇闪烁'),
        ('PE-1', 'elevator', ('P-ADMIN-LIFT', 'TX2'), '人梯停层不稳定且没有困人'),
        ('CH-A1', 'hvac', ('P-ADMIN-HVAC', 'TX2'), '行政楼冷水机出水温度持续升高'),
    ]
    event_types = {'equipment': 'equipment_fault', 'hvac': 'heat_wave',
                   'elevator': 'elevator_congestion', 'energy': 'lighting_fault'}
    events = [ScriptEvent(at=f'00:{5 + i * 5:02}', id=f'unseen-{i:02}', type=event_types[domain],
                          entity_id=entity, severity=2, summary=summary)
              for i, (entity, domain, _, summary) in enumerate(cases)]
    scenario = base.model_copy(update={'scenario_id': 'b33-unseen-probe', 'events': events, 'bursts': [],
                                       'operator_actions': [], 'dispatch': {}, 'knowledge': []})
    routes = []
    for group in ('independent', 'coordinated'):
        world = build_world(scenario)
        with tempfile.TemporaryDirectory(prefix='b33-d54-') as tmp:
            db = open_db(Path(tmp) / 'probe.db', world.site.plant_id)
            try:
                registry = default_registry()
                runner = TimedRunner(world, db, registry=registry, mode='evaluation', group=group)
                sources = []
                if collector is not None and group == 'coordinated':
                    def resolve(binding):
                        ticket = runner.tickets.tickets.get(binding.ticket_id)
                        return (ticket.event.get('event_id') if ticket else None,
                                {'world': runner.state.to_json(), 'energy_kwh': runner.energy,
                                 'knowledge': sorted(runner.knowledge)})
                    sources = install_cache(registry, ResultCache(Path(tmp) / 'cache.json', 'd54-v1'),
                                  {'scenario': fingerprint({'scenario': scenario.scenario_id, 'seed': scenario.seed}),
                                   'prompt_version': 'd54-v1', 'data_version': fingerprint([x.model_dump(mode='json') for x in world.inputs.data_source_refs()]),
                                   'model_version': model_version, 'resolve': resolve, 'collector': collector},
                                  list(dict.fromkeys([*EXPERTS, 'expert', 'triage', 'investigate', 'arbitration', 'follow_up'])))
                runner.run(until=scenario.instant('01:00'))
                records = []
                for event, (_, expected, required_refs, _) in zip(events, cases):
                    tid = runner.tickets.by_event.get(event.id)
                    ticket = runner.tickets.tickets.get(tid) if tid else None
                    experts = list(ticket.ticket.experts) if ticket else []
                    channel, refs = ('programmatic' if group == 'independent' and tid else 'unrouted'), []
                    if group == 'coordinated' and tid:
                        store = runner.dispatcher.router.store
                        channel = store.channel_of(tid) or 'unrouted'
                        investigation = store.investigation(tid, 1) or {}
                        refs = investigation.get('evidence_refs') or []
                    records.append({'event_id': event.id, 'expected_expert': expected, 'required_evidence': list(required_refs), 'experts': experts,
                                    'correct': expected in experts,
                                    'evidence_complete': set(required_refs).issubset(set(refs)), 'has_reference': bool(refs),
                                    'evidence_refs': refs, 'channel': channel, 'elapsed_ms': runner.event_elapsed[event.id]})
                collection = None
                if sources:
                    collection = {
                        'collected': sum(s.collected for s in sources),
                        'cache_hits': sum(s.hits for s in sources),
                        'cache_misses': sum(s.misses for s in sources),
                        'failures': sum(s.collection_failures for s in sources),
                        'errors': [e for s in sources for e in s.collection_errors],
                    }
                routes.append({'route': 'A_programmatic' if group == 'independent' else 'B_fast_slow',
                               'count': len(records), 'routing_accuracy': mean(r['correct'] for r in records),
                               'diagnosis_accuracy': None,
                               'evidence_completeness': mean(r['evidence_complete'] for r in records),
                               'evidence_presence': mean(r['has_reference'] for r in records),
                               'slow_fraction': mean(r['channel'] == 'slow' for r in records),
                               'fast_fraction': mean(r['channel'] == 'fast' for r in records),
                               'urgent_fraction': mean(r['channel'] == 'urgent' for r in records),
                               'unrouted_fraction': mean(r['channel'] == 'unrouted' for r in records),
                               'programmatic_fraction': mean(r['channel'] == 'programmatic' for r in records),
                               'mean_wall_ms': mean(r['elapsed_ms'] for r in records), 'records': records,
                               **({'collection': collection} if collection is not None else {})})
            finally:
                db.close()
    summary = {'nature': 'synthetic', 'scope': 'expert routing, not diagnosis correctness',
               'truth': 'configured expert labels and required upstream panel + transformer references (factory site topology)',
               'model': ('explicit Nexent collector; inspect call records and coverage' if collector is not None else
                         'offline deterministic; independent real Nexent experiment not yet performed'), 'routes': routes}
    if collector is not None:
        # 采集失败会被缓存层吞掉并退回离线结果；把计数写进产物，避免“看起来跑成功、实际大半回退”。
        totals = {name: sum(r['collection'][name] for r in routes if r.get('collection'))
                  for name in ('collected', 'cache_hits', 'cache_misses', 'failures')}
        summary['collection'] = {**totals, 'errors': [e for r in routes for e in (r.get('collection') or {}).get('errors', [])]}
        summary['collection_complete'] = totals['failures'] == 0 and totals['collected'] > 0
    return summary
