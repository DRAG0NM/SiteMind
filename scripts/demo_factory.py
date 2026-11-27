"""Run the factory scenario and export its actual offline replay data."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__' and os.environ.get('PYTHONHASHSEED') != '0':
    # Set iteration affects same-minute triage ordering; set it before imports.
    raise SystemExit(subprocess.call([sys.executable, *sys.argv], env={**os.environ, 'PYTHONHASHSEED': '0'}))
sys.path.insert(0, str(ROOT))

from plantkg.contracts.validate import validate, validate_chain_file
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, DayMetric, PendingAction, QaEntry, Role
from plantkg.core.ports import PendingActionStore, RuleMatcher, VersionStore
from plantkg.core.versions import read
from plantkg.evolution.service import EvolutionService, evolution_config
from plantkg.evolution.store import EvolutionStore
from plantkg.evaluation.cache import ResultCache, fingerprint, install_cache
from plantkg.evaluation.scenario import cache_state, evaluate
from plantkg.evaluation.report import write_report
from plantkg.experts.energy.minute import install_minute_layer
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.store import CandidateStore
from plantkg.world.scenario import ScenarioRunner, load_scenario
from plantkg.world.scenario.__main__ import build_world, catalog_metrics_provider, day_exports, trace_json
from plantkg.world.scenario.export import chain, day_window, export_day
from plantkg.world.scenario.runner import ScenarioDB, default_registry
from plantkg.world.scenario.experts import EXPERTS

SCENARIO = ROOT / 'configs/scenarios/factory_demo_day.yaml'


def is_display_metric(name):
    """D54 编排对照与 Harness 展示指标：不属于 B33 两日对照，不套“三组可比”的说明导入回放日。"""
    return name.startswith('orchestration_') or name == 'harness_pass_rate'


def install_demo_review(runtime, services, scenario):
    """Offline synthetic reviewer; B32 creates, backtests and publishes real artifacts."""
    original = runtime.hooks_for('scenario.document_upload')[-1]
    registry = services.registry
    evidence = {'scenario_nature': 'synthetic', 'reviewer_nature': 'synthetic',
                'business_logic': 'B32/B47', 'mode': 'offline', 'onboarding': []}

    def upload_and_review(runner, at, payload):
        note_start = len(runner.trace.notes)
        original(runner, at, payload)
        reviewer = Actor(actor_id='b36-synthetic-reviewer', roles=(Role.reviewer,))
        actions, versions = registry.resolve(PendingActionStore), registry.resolve(VersionStore)
        if payload.get('document'):
            candidates = CandidateStore(versions.writer, versions.plant_id)
            subjects = {'elevator_congestion_rule': runner.ids['admin_low'],
                        'zone_temperature_limit': runner.ids['zone:cleanroom'],
                        'compressor_pressure_drop': runner.ids['air_system']}
            pending_notes = list(runner.trace.notes[note_start:])
            for note in pending_notes:
                for candidate_id in note.get('candidate_ids', []):
                    candidate = candidates.get(candidate_id)
                    kind = candidate.draft['kind']
                    if kind not in subjects:
                        evidence['onboarding'].append({'candidate_id': candidate_id, 'kind': kind,
                            'status': 'pending', 'reason': '原文未完整抽取，保留待复核，不伪造规则'})
                        continue
                    context = runner._context(runner.run_info.version_manifest_id).model_copy(
                        update={'actor': reviewer, 'as_of': at})
                    review = registry.tools['propose_review_decision'].handler(context, review_id=candidate_id,
                        decision='accept', rationale='synthetic 离线复核原文及适用设备',
                        revision={'subject_id': subjects[kind], 'effective_from': candidate.draft['available_at']})
                    if not review.ok:
                        raise RuntimeError(review.to_agent_text())
                    action = actions.get(review.data['action_id'])
                    actions.confirm(action.action_id, reviewer, 'synthetic 资料复核确认', action.revision,
                                    'b36-review-' + candidate_id)
                    review_result = actions.result(action.action_id)
                    if not review_result or not review_result.ok:
                        raise RuntimeError('资料复核确认未成功')
                    publish = registry.tools['propose_publish'].handler(context, candidate_id=candidate_id,
                                                                       rationale='synthetic 资料规则发布')
                    if not publish.ok:
                        raise RuntimeError(publish.to_agent_text())
                    action = actions.get(publish.data['action_id'])
                    actions.confirm(action.action_id, reviewer, 'synthetic 资料发布确认', action.revision,
                                    'b36-onboarding-' + candidate_id)
                    publish_result = actions.result(action.action_id)
                    if not publish_result or not publish_result.ok or not publish_result.version_manifest_id:
                        raise RuntimeError('资料发布确认未成功')
                    for step in runtime.hooks_for('scenario.knowledge_step'):
                        step(runner, at)
                    if runner.run_info.version_manifest_id != publish_result.version_manifest_id:
                        raise RuntimeError('已发布资料未接续到场景运行')
                    # B30's knowledge-step fork is also a current B32 publication target.
                    for register in runtime.hooks_for('evolution.register_scenario_runner'):
                        register(runner)
                    evidence['onboarding'].append({'candidate_id': candidate_id, 'kind': kind, 'status': 'published',
                        'at': at.isoformat(), 'manifest_id': runner.run_info.version_manifest_id,
                        'knowledge_version': runner.knowledge_version})
            return
        if payload.get('event') != 'act4-combo':
            return
        ticket = next((t for t in runner.trace.tickets.values() if 'act4-combo' in t.get('event_ids', [])), None)
        if ticket is None:
            raise RuntimeError('第四幕组合异常 act4-combo 没有生成事件单')
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={'actor': reviewer, 'run_id': runner.run_info.run_id, 'as_of': at})
        evidence.update(parent_run_id=runner.run_info.run_id,
                        parent_manifest_id=runner.run_info.version_manifest_id)
        correction = {'note': '照明与空压异常由生产总配电柜接触不良引起',
                      'event_type': 'unknown_combo', 'entity_id': runner.ids['LT-P'],
                      'autonomy_category': 'unknown_combo|lighting', 'autonomy_level': 'L2',
                      'condition': {'event_type': 'unknown_combo', 'observation_contains': '组合异常',
                                    'actions': [{'entity_id': runner.ids['LT-P'], 'command': 'repair'}]}}
        result = registry.hooks_for('evolution.record_correction')[-1](context, ticket['ticket_id'], correction)
        if not result.ok:
            raise RuntimeError(result.to_agent_text())
        evolution = EvolutionService(EvolutionStore(versions.writer, versions.plant_id), versions, actions,
                                     RuleArtifacts(versions.writer, versions.plant_id), registry, context,
                                     config=evolution_config(services.settings))
        report = evolution.backtest(result.data['candidate_id'], [{
            'scenario_id': 'factory_demo_day', 'seed': scenario.seed, 'path': str(SCENARIO)}])
        if report.verdict != 'pass':
            raise RuntimeError(f'B32 backtest did not pass: {report.failures}')
        proposal = registry.tools['propose_publish'].handler(
            context, candidate_id=result.data['candidate_id'], rationale='synthetic 离线复核')
        if not proposal.ok:
            raise RuntimeError(proposal.to_agent_text())
        action = actions.get(proposal.data['action_id'])
        actions.confirm(action.action_id, reviewer, 'synthetic 离线人工确认', action.revision, 'b36-demo-publish')
        result = actions.result(action.action_id)
        if not result or not result.ok or runner.run_info.version_manifest_id != result.version_manifest_id:
            raise RuntimeError('B32 发布确认后未接续到场景运行')
        candidate = evolution.store.candidate(result.data['candidate_id'])
        evidence.update(request=correction, candidate=candidate.model_dump(mode='json'),
                        backtest=report.model_dump(mode='json'),
                        confirmation=actions.get(action.action_id).model_dump(mode='json'),
                        rules=evolution.artifacts.load(candidate.changes['rules'])['rules'],
                        child_run_id=runner.run_info.run_id,
                        manifest_id=runner.run_info.version_manifest_id,
                        knowledge_version=runner.knowledge_version,
                        fork_at=at.isoformat())

    runtime.register_hook('scenario.document_upload', upload_and_review)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['offline'], default='offline')
    parser.add_argument('--seed', type=int, default=20260701)
    parser.add_argument('--out', type=Path, default=ROOT / 'runtime/b36-demo', help='new output directory')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    scenario = load_scenario(SCENARIO).model_copy(update={'seed': args.seed})
    world = build_world(scenario)
    # Collect through B33's official interface in an isolated output directory.
    report = evaluate(SCENARIO, args.out / 'evaluation', args.seed, collector='deterministic', refresh=True)
    write_report(report, args.out / 'evaluation')
    db_path = args.out / 'scenario.db'
    settings = load_settings(profile='dev', overrides={'nexent.enabled': False,
        'storage.enabled': True, 'storage.sqlite': str(db_path),
        'storage.graph': str(args.out / 'graph'), 'storage.series': str(args.out / 'series')})
    services = build_services(settings)
    db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
    try:
        registry = default_registry(services.registry)
        runner = ScenarioRunner(world, db, registry=registry, mode='evaluation', group='coordinated')
        correction_evidence = install_demo_review(registry, services, scenario)
        def cache_context(binding):
            ticket = runner.tickets.tickets.get(binding.ticket_id)
            event = ticket.event if ticket is not None else {}
            return event.get('event_id'), {'world': cache_state(runner.state), 'energy_kwh': runner.energy,
                                           'knowledge': sorted(runner.knowledge)}
        sources = install_cache(registry, ResultCache(args.out / 'evaluation/agent-cache.json', 'v1'), {
            'scenario': fingerprint({'sha256': scenario.sha256, 'seed': scenario.seed, 'group': 'coordinated'}),
            'prompt_version': report['prompt_version'],
            'data_version': fingerprint([x.model_dump(mode='json') for x in world.inputs.data_source_refs()]),
            'resolve': cache_context,
            'resolve_proposals': lambda proposal_id: runner.dispatcher.router.sink.get(proposal_id),
        }, list(dict.fromkeys([*EXPERTS, 'expert', 'triage', 'investigate', 'arbitration', 'follow_up'])))
        runner.start()
        install_minute_layer(runner)
        trace = runner.run_until()
        (args.out / 'trace.json').write_text(json.dumps(trace_json(trace, scenario), ensure_ascii=False, indent=2), encoding='utf-8')

        # Answers are explicitly simulated and refer to the actual event, never to a fake model call.
        runs = chain(db, trace.run_id)
        real_published = False
        real_equipment_proposals = set()
        evolution = EvolutionStore(db.writer, db.plant_id)
        run_manifests = {r.version_manifest_id for r in runs}
        original_ticket = next((n.get('ticket_id') for n in trace.notes
                                if n.get('event_id') == 'act4-combo' and n['kind'] == 'admission'), None)
        next_ticket = next((n.get('ticket_id') for n in trace.notes
                            if n.get('event_id') == 'day2-combo' and n['kind'] == 'admission'), None)
        for published in evolution.timeline():
            if published.get('event') != 'published' or published.get('manifest_id') != runs[-1].version_manifest_id:
                continue
            candidate = evolution.candidate(published['candidate_id'])
            report_record = evolution.report(published['report_id'])
            row = read(db.writer, lambda con: con.execute(
                'SELECT record FROM pending_actions WHERE plant_id=? AND action_id=?',
                (db.plant_id, candidate.publish_action_id)).fetchone())
            action = PendingAction.model_validate_json(row[0]) if row else None
            proposals = db.store.records(runs[-1].run_id, 'proposal')
            matching_proposals = {p['proposal_id'] for p in proposals
                                  if p.get('ticket_id') == next_ticket and p.get('expert') == 'equipment'
                                  and p.get('binding', {}).get('run_id') == runs[-1].run_id
                                  and p.get('binding', {}).get('knowledge_version') ==
                                  db.versions.knowledge_version(published['manifest_id'])}
            if (original_ticket and next_ticket and candidate.trigger.split('；', 1)[0] == f'人工纠错：{original_ticket}'
                    and candidate.status == 'published' and candidate.base_manifest_id in run_manifests
                    and candidate.backtest_report_id == report_record.report_id
                    and report_record.base_manifest_id == candidate.base_manifest_id
                    and report_record.candidate_id == candidate.candidate_id and report_record.verdict == 'pass'
                    and action is not None and action.status == 'confirmed'
                    and action.target_ref == candidate.candidate_id
                    and matching_proposals):
                real_published = True
                real_equipment_proposals = matching_proposals
                break
        again = next((e for e in trace.events if e.event_id == 'day2-combo'), None)
        if again is None:
            raise RuntimeError('次日组合异常 day2-combo 没有出现在运行事件里')
        matcher = registry.resolve(RuleMatcher)
        proposal = next((p for p in trace.proposals if p.ticket_id == next_ticket and p.expert == 'equipment'), None)
        if proposal is None or not proposal.actions:
            raise RuntimeError('次日设备专家没有根据已发布规则提出动作')
        checks = {
            'new_version_matches': matcher.covers_for_run(again, correction_evidence['child_run_id']),
            'old_version_matches': matcher.covers_for_run(again, correction_evidence['parent_run_id']),
            'other_device_matches': matcher.covers_for_run(
                again.model_copy(update={'entity_id': runner.ids['AC-2']}), correction_evidence['child_run_id']),
            'other_condition_matches': matcher.covers_for_run(
                again.model_copy(update={'summary': '条件不匹配'}), correction_evidence['child_run_id']),
            'proposal_authorized': matcher.authorizes_for_run(again, correction_evidence['child_run_id'], proposal.actions),
            'extra_action_authorized': matcher.authorizes_for_run(again, correction_evidence['child_run_id'],
                (*proposal.actions, proposal.actions[0].model_copy(update={'entity_id': runner.ids['AC-2']}))),
        }
        if checks != {'new_version_matches': True, 'old_version_matches': False, 'other_device_matches': False,
                      'other_condition_matches': False, 'proposal_authorized': True, 'extra_action_authorized': False}:
            raise RuntimeError(f'B32 rule scope regression: {checks}')
        correction_evidence['checks'] = checks
        (args.out / 'correction.json').write_text(json.dumps(correction_evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        event_owners = {}
        for run in runs:
            for recorded in db.store.records(run.run_id, 'event'):
                event_owners.setdefault(recorded['event_id'], run.run_id)
        for event in trace.events:
            if event.event_type not in {'document_upload', 'elevator_congestion', 'load_spike', 'compressor_pressure_drop',
                                        'lighting_fault', 'operator_instruction', 'elevator_trapped', 'operator_confirm',
                                        'unknown_combo', 'operator_correct'}:
                continue
            owner = event_owners.get(event.event_id)
            if owner is None:
                raise RuntimeError(f'事件 {event.event_id} 没有落库，无法关联运行')
            tickets = [t for t in trace.tickets.values() if event.event_id in t.get('event_ids', [])]
            ticket_ids = ', '.join(t['ticket_id'] for t in tickets) or '没有关联事件单'
            answers = [
                (event.at, '发生了什么？', event.summary),
                (event.at, '为什么这样处置？', f'请查看事件单 {ticket_ids} 中实际记录的专家提案和裁决。'),
                (event.at, '依据哪些记录？', f'事件 {event.event_id}；事件单 {ticket_ids}；运行 {owner}。'),
            ]
            if event.event_id == 'day2-combo':
                actual = next((n for n in trace.notes if n['kind'] == 'b47_execution'
                               and n.get('ticket_id') == next_ticket
                               and n.get('proposal_id') in real_equipment_proposals), None)
                if actual:
                    execution_proposal = next((p for p in trace.proposals if p.proposal_id == actual['proposal_id']), None)
                    if execution_proposal is None:
                        raise RuntimeError(f"B47 执行记录引用的提案 {actual['proposal_id']} 不在运行提案里")
                    visible_times = [event.at]
                    if execution_proposal.timing and execution_proposal.timing.effective_at:
                        visible_times.append(execution_proposal.timing.effective_at)
                    visible_times.extend(r.timing.effective_at for r in trace.agent_runs
                                         if actual['proposal_id'] in r.proposal_ids and r.timing.effective_at)
                    delivered = [a for a in trace.actions if a.get('executed')
                                 and a.get('source', {}).get('proposal_id') == actual['proposal_id']]
                    if actual['executed'] and not delivered:
                        raise RuntimeError('B47 执行记录缺少实际动作')
                    visible_times.extend(datetime.fromisoformat(a['action']['interval']['start'].replace('Z', '+00:00'))
                                         for a in delivered)
                    answers.append((max(visible_times), 'B47 如何判断和执行？',
                        f"运行记录：等级 {actual['level']}，置信度 {actual['confidence']}；"
                        f"执行 {actual['executed']} 项，排队 {actual['queued']} 项；提案 {actual['proposal_id']}。"))
            for i, (answer_at, question, answer) in enumerate(answers, 1):
                entry = QaEntry(qa_id=f'qa:{event.event_id}:{i}', at=answer_at, question=question,
                                answer=f'【模拟预设回答】{answer}')
                db.store.write_records(owner, [('qa', entry.qa_id, entry.at, entry.model_dump(mode='json'))])

        cache_stats = {'version': 'v1', 'source': 'B36 演示重放缓存',
                       'hits': sum(s.hits for s in sources), 'misses': sum(s.misses for s in sources),
                       'b33_groups': {g['group']: g['cache'] for g in report['groups']},  # B33 采集运行自己的缓存统计，口径同报告的 cache 字段
                       'details': [s.miss_differences for s in sources if s.miss_differences]}
        (args.out / 'demo-cache-stats.json').write_text(json.dumps(cache_stats, ensure_ascii=False, indent=2), encoding='utf-8')
        # These are B33's separate two-day runs, not the replay's own ledger.
        # Show deltas only when B33 declares all three runs comparable.
        groups = {g['group']: g for g in report['groups']}
        replay_sources = [x.model_dump(mode='json') for x in runner.run_info.data_sources]
        eligible = set(groups) == {'baseline', 'independent', 'coordinated'} and all(
            g['comparable'] and g['data_sources'] == replay_sources for g in groups.values())
        eligible = (eligible and report['scenario_sha256'] == scenario.sha256 and report['seed'] == scenario.seed
                    and report['days'] == scenario.days)
        metrics = tuple(DayMetric(metric=m['metric'], group=m['group'], value=m['value'] if eligible else None,
                                  unit=m['unit'], nature='simulated', variant=m.get('variant'),
                                  note=f"B33 独立两日对照；运行 {groups[m['group']]['run_id']}；" +
                                       ('三组可比；评测末端电量恢复，非回放运行' if eligible else '不可比，数值隐藏') +
                                       '；B33 对照不含 B36 的纠错复核流程' +
                                       f"；{m.get('note') or ''}")
                        for m in report['metrics'] if m['group'] in groups and not is_display_metric(m['metric']))  # 展示页指标不是回放日自己的对照，不导入
        for offset in range(scenario.days):
            date = (scenario.start.astimezone(ZoneInfo(scenario.site_tz)).date() + timedelta(days=offset)).isoformat()
            as_of = day_window(runs, date)[2]
            evidence, assets = day_exports(db, runs, as_of)
            day = export_day(db, trace.run_id, date, metrics=metrics, evidence=evidence, assets=assets,
                             registry=default_registry(), site=world.site, asset_metrics_provider=catalog_metrics_provider(db))
            day = day.model_copy(update={'cache_version': 'v1'})
            path = args.out / ('day.json' if offset == 0 else f'day-{date}.json')
            path.write_text(day.model_dump_json(indent=2), encoding='utf-8')
            problems = validate(path)
            if problems:
                raise RuntimeError(f'{path}: {problems}')
    finally:
        services.close()
    problems = validate_chain_file(db_path, trace.run_id)
    if problems:
        raise RuntimeError(f'run chain: {problems}')
    print(f'run_id={trace.run_id}\noutput={args.out.resolve()}\nday={(args.out / "day.json").resolve()}')
    print(f'events={len(trace.events)} minutes={len(trace.ledger)} balanced={trace.ledger_summary()["minutes_balanced"]}')
    admission = next((n for n in trace.notes if n.get('event_id') == 'day2-combo' and n['kind'] == 'admission'), None)
    executions = [n for n in trace.notes if admission and n.get('ticket_id') == admission.get('ticket_id')
                  and n['kind'] == 'b47_execution']
    if not (real_published and admission and admission.get('channel') == 'fast' and
            any(n['level'] == 'L2' and n['executed'] > 0 and n.get('proposal_id') in real_equipment_proposals
                for n in executions)):
        print('INCOMPLETE: real B32 publication and B47 automatic execution are not both evidenced.', file=sys.stderr)
        raise SystemExit(1)
    print('Real B32 publication and B47 automatic execution evidenced; reviewer and scenario are synthetic.')


if __name__ == '__main__':
    main()
