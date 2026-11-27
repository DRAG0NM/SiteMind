"""Run B33 offline scenario comparison."""
import argparse
import json
import os
import sys
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plantkg.evaluation.scenario import evaluate
from plantkg.evaluation.report import write_report
from plantkg.evaluation.orchestration import run_probe
from plantkg.evaluation.ledger import resolve_ledger_default

REPO = Path(__file__).resolve().parents[1]
# 与其他三条入口同一本账、同一锚点；ISSUE244_LEDGER 优先。旧默认 runtime/b33-model-budget.json 跟随 CWD，会静默另起一本账
DEFAULT_LEDGER = resolve_ledger_default(REPO / 'runtime' / 'issue244-batch-budget.json')


def verify_nexent_assets(args):
    """核对当前 JWT 所属租户里的 Agent、模型与发布提示词，纯只读、不发任何模型请求。

    返回证据摘要（租户、账号、每个 Agent 的版本/模型/提示词字节数），供隔离租户验收与
    `--preflight-only` 使用；真实推理前先把这层确认干净，避免请求发出后才发现对错了租户。
    """
    import httpx
    import yaml

    jwt = os.environ.get('NEXENT_EVAL_ADMIN_JWT' if args.d54_only else 'NEXENT_ADMIN_JWT')
    if not jwt:
        raise ValueError('an admin JWT is required to verify the actual Nexent model and endpoint')
    with httpx.Client(timeout=15, trust_env=False, headers={'Authorization': 'Bearer ' + jwt}) as client:
        who = client.get('http://127.0.0.1:5010/user/current_user_info')
        who.raise_for_status(); account = who.json()
        account = account.get('data') or account
        account = account.get('user') or account
        listed = client.get('http://127.0.0.1:5010/agent/list'); listed.raise_for_status()
        agents = listed.json(); agents = agents if isinstance(agents, list) else agents.get('data', [])
        models = client.get('http://127.0.0.1:5010/model/list'); models.raise_for_status()
        model_table = {m.get('model_id'): m for m in models.json().get('data', [])}
        assets = [yaml.safe_load(p.read_text(encoding='utf-8')) for p in Path(args.agents_dir).glob('*.yaml')]
        asset_table = {a.get('name'): a for a in assets if isinstance(a, dict) and a.get('role') in ('main', 'expert')}
        names = list(asset_table)
        if args.main_agent not in names:
            raise ValueError('main agent missing from assets')
        rows = []
        for name in names:
            agent = next((a for a in agents if a.get('name') == name), None)
            if agent is None:
                raise ValueError(f'Nexent agent is unavailable: {name}')
            version = agent.get('current_version_no')
            if not version:
                raise ValueError(f'Nexent agent is not published: {name}')
            info = client.get(f"http://127.0.0.1:5010/agent/{agent['agent_id']}/versions/{version}/detail")
            info.raise_for_status(); data = info.json(); data = data.get('data', data)
            model = model_table.get(data.get('business_logic_model_id')) or {}
            if model.get('model_name') != args.model_version or str(model.get('base_url', '')).rstrip('/') != args.model_base_url.rstrip('/'):
                raise ValueError(f'Nexent model or endpoint mismatch for {name}')
            if data.get('prompt') != asset_table[name].get('prompt'):
                raise ValueError(f'Nexent published prompt differs from local assets for {name}')
            rows.append({'name': name, 'agent_id': agent.get('agent_id'), 'version_no': version,
                         'model_id': data.get('business_logic_model_id'),
                         'model_name': model.get('model_name'),
                         'prompt_bytes': len(data.get('prompt') or '')})
    return {'tenant_id': account.get('tenant_id'), 'user_id': account.get('user_id'),
            'user_email': account.get('user_email'),
            'role': account.get('user_role') or account.get('role'),
            'group_ids': account.get('group_ids'),
            'model_version': args.model_version,
            'model_base_url': args.model_base_url, 'agents_dir': str(args.agents_dir),
            'agents': rows, 'ok': True}


@contextmanager
def nexent_collector(args, out):
    import uvicorn
    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.core.ports import AgentResultSource, ProposalSink
    from plantkg.evaluation.collection import BudgetedCollector
    from plantkg.mcp_server import create_mcp_server
    from scripts.nexent_live_scenario import start_server

    verify_nexent_assets(args)

    overrides = {'server.host': '0.0.0.0', 'server.port': str(args.collect_port),
                 'nexent.enabled': 'true', 'nexent.run_retries': '0',
                 'nexent.run_deadline_s': str(args.timeout), 'nexent.agents_dir': args.agents_dir,
                 'nexent.main_agent': args.main_agent}
    services = build_services(load_settings(profile='dev', overrides=overrides))
    server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), host='0.0.0.0',
                                           port=args.collect_port, log_level='warning'))
    collector = None
    try:
        start_server(server)
        live = services.registry.resolve_mode(AgentResultSource, 'live', key='arbitration')
        collector = BudgetedCollector(live, model=args.model_version, budget_cny=args.budget_cny,
                                      per_call_cny=args.per_call_cny, max_calls=args.max_calls,
                                      log_path=Path(args.budget_ledger))
        collector.proposal_lookup = services.registry.resolve(ProposalSink).get
        yield collector
    finally:
        server.should_exit = True
        if collector is not None:
            collector.close()
        services.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario')
    parser.add_argument('--out', default='runtime/scenario-evaluation')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--cache-version', default='v1')
    parser.add_argument('--b32-results', help='optional B32 JSON artifact')
    parser.add_argument('--orchestration', action='store_true', help='run D54 synthetic offline routing probe')
    parser.add_argument('--d54-only', action='store_true', help='run only the D54 two-route experiment')
    parser.add_argument('--isolated-memory-disabled', action='store_true', help='confirm the dedicated evaluation account has memory disabled')
    parser.add_argument('--collect-offline', action='store_true', help='collect deterministic results without network calls')
    parser.add_argument('--collect-nexent', action='store_true', help='explicitly enable budgeted B23 Nexent collection')
    parser.add_argument('--refresh', action='store_true', help='refresh cached results (requires an explicit collector)')
    parser.add_argument('--budget-cny', type=float)
    parser.add_argument('--per-call-cny', type=float, help='conservative ceiling including all server-side steps')
    parser.add_argument('--max-calls', type=int)
    parser.add_argument('--budget-ledger', default=DEFAULT_LEDGER, help='shared persistent reservation and call log')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--collect-port', type=int, default=8710)
    parser.add_argument('--agents-dir', default='nexent_assets/agents')
    parser.add_argument('--main-agent', default='plantkg_main')
    parser.add_argument('--model-version', default='', help='model identity for cache isolation')
    parser.add_argument('--model-base-url', default='https://dashscope.aliyuncs.com/compatible-mode/v1')
    parser.add_argument('--preflight-only', action='store_true',
                        help='verify the tenant, agents and published prompts only; no model call is made')
    args = parser.parse_args()
    out = Path(args.out)
    if args.preflight_only:
        summary = verify_nexent_assets(args)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'preflight.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.collect_offline and args.collect_nexent:
        parser.error('choose one collection mode')
    if args.refresh and not (args.collect_offline or args.collect_nexent):
        parser.error('--refresh requires an explicit collection mode')
    if args.collect_nexent and (not args.budget_cny or not args.per_call_cny or not args.max_calls or not args.model_version):
        parser.error('Nexent collection requires model version, budget, per-call ceiling and call limit')
    out = Path(args.out)
    if args.d54_only:
        out.mkdir(parents=True, exist_ok=True)
        if args.collect_nexent:
            eval_key = os.environ.get('NEXENT_EVAL_API_KEY')
            eval_jwt = os.environ.get('NEXENT_EVAL_ADMIN_JWT')
            original = os.environ.get('NEXENT_API_KEY')
            if not eval_key or eval_key == original or not eval_jwt or not args.isolated_memory_disabled:
                parser.error('D54 Nexent requires a separate evaluation key, its admin JWT, and memory disabled')
            import httpx
            with httpx.Client(timeout=15, trust_env=False) as client:
                check = client.get('http://127.0.0.1:5010/memory/config/load',
                                   headers={'Authorization': 'Bearer ' + eval_jwt})
                if check.status_code != 200 or str(check.json().get('MEMORY_SWITCH', '')).upper() not in ('N', 'FALSE', '0'):
                    parser.error('evaluation account memory must be disabled and verifiable')
            os.environ['NEXENT_API_KEY'] = eval_key
            try:
                with nexent_collector(args, out) as collector:
                    probe = run_probe(args.scenario, collector=collector, model_version=args.model_version)
            finally:
                if original is None:
                    os.environ.pop('NEXENT_API_KEY', None)
                else:
                    os.environ['NEXENT_API_KEY'] = original
        else:
            probe = run_probe(args.scenario)
        (out / 'orchestration.json').write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(probe, ensure_ascii=False, indent=2))
        return
    probe = run_probe(args.scenario) if args.orchestration else None
    if args.collect_nexent:
        with nexent_collector(args, out) as collector:
            result = evaluate(args.scenario, out, args.seed, args.cache_version, args.b32_results,
                              collector=collector, refresh=args.refresh, model_version=args.model_version,
                              agents_dir=args.agents_dir, probe=probe)
    else:
        result = evaluate(args.scenario, out, args.seed, args.cache_version, args.b32_results,
                          collector='deterministic' if args.collect_offline else None, refresh=args.refresh,
                          model_version=args.model_version, agents_dir=args.agents_dir, probe=probe)
    write_report(result, out)
    if args.orchestration:
        (out / 'orchestration.json').write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding='utf-8')
    groups = ('baseline', 'independent', 'coordinated')
    print(f"{'metric':35} {'unit':10} {'baseline':>15} {'independent':>15} {'coordinated':>15}")
    for name in dict.fromkeys(m['metric'] for m in result['metrics']):
        row = [next((m for m in result['metrics'] if m['metric'] == name and m['group'] == group), None) for group in groups]
        shown = next((m for m in row if m), None)
        if shown is None:
            continue  # knowledge 组的展示指标不在三组表里
        print(f"{name:35} {str(shown['unit']):10} " + ' '.join(f"{str(m['value']) if m else '-':>15}" for m in row))
    for group in result['groups']:
        print(group['group'], 'comparable' if group['comparable'] else 'incomparable', group['incomparable_reasons'],
              'cache hits/misses/collected:', group['cache_hits'], '/', group['cache_misses'], '/', group['cache_collected'],
              'mode:', group['cache']['mode'], 'coverage gap:', group['coverage_gap'])
    print('cache misses = all misses, including those collected afterwards; coverage gap cache_misses = actual deterministic fallbacks')


if __name__ == '__main__':
    main()
