"""B42: bounded health checks and evidence-based ticket diagnostics."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from plantkg.core.contracts import Context, ContextChannel
from plantkg.core.versions import atomic, read

log = logging.getLogger(__name__)

# B27 登记 fn() -> 当前运行视图；B42 每次 check 重新取，作用域（plant 或 plant#run）由运行决定
WATCHDOG_TICKETS_HOOK = 'watchdog.tickets'


def now():
    return datetime.now(timezone.utc)


def http_probe(url):
    if not url:
        return "unknown", "未配置检查地址"
    try:
        with urlopen(url, timeout=3) as response:
            return ("ok", f"HTTP {response.status}") if 200 <= response.status < 300 else ("failed", f"HTTP {response.status}")
    except HTTPError as exc:
        return ('unknown' if exc.code in (401, 403, 404) else 'failed'), f'健康端点 HTTP {exc.code}'
    except (URLError, OSError, ValueError):
        return "failed", "连接失败或超时"


def queue_probe(jobs=None, writer=None, plant_id=None):
    """任务队列健康：接 JobRunner 端口时读执行器健康，否则退回只读统计（仅旧装配与单测）。"""
    if jobs is not None:
        try:
            health = jobs.health(_read_context(plant_id))
            return 'ok', (f"执行器：队列 {health.queue_depth}，工作线程 {health.alive_workers}/{health.configured_workers}"
                          + ("，已关闭" if health.closed else ""))
        except Exception:  # noqa: BLE001 — 健康检查失败如实报未知，不影响其它探针
            log.exception('watchdog job health failed')
            return 'unknown', '无法读取任务队列'
    try:
        rows = read(writer, lambda c: c.execute("SELECT status, COUNT(*) FROM jobs WHERE plant_id=? GROUP BY status", (plant_id,)).fetchall())
        return 'unknown', '队列记录：' + (', '.join(f'{s}={n}' for s, n in rows) or '无任务') + '；未接执行器健康端口'
    except Exception:
        return 'unknown', '无法读取任务队列'


def _read_context(plant_id):
    return Context(plant_id=plant_id, industry_pack='-', actor=None, channel=ContextChannel.none,
                   trace_id='watchdog', as_of=now())


# 本轮沿用上一轮结论的“无证据”原因；这些分支不覆盖已记录的受阻/恢复结论（#152）
_UNKNOWN_REASONS = frozenset({
    '事件单已受阻，原受阻原因未知',
    'Agent 运行失败；依赖故障证据不足，原因未知',
    '事件单等待中，但未找到等待依据；原因未知，需人工核查',
    '未发现明确受阻证据；无进展原因未知',
    '最近运行未显示明确故障',
})


class _Inspection:
    """巡检视图：B27 的 TicketInspectionSource 或旧装配给的 store，读事件单、运行记录与结论。"""

    def __init__(self, source, runs):
        self.source, self.runs = source, runs
        self.store = getattr(source, 'store', source)
        self.plant_id = getattr(source, 'plant_id', None) or getattr(self.store, 'plant_id', '')
        self.writer = getattr(source, 'writer', None) or getattr(self.store, 'writer', None)

    def open_tickets(self):
        return self.store.open_tickets()

    def ticket(self, ticket_id):
        return self.store.ticket(ticket_id)

    def questions(self, ticket_id):
        return self.store.questions(ticket_id)

    def probe_of(self, ticket_id):
        return self.store.probe_of(ticket_id)

    def run_of_ticket(self, ticket_id):
        return self.store.run_of_ticket(ticket_id)

    def is_running(self, ticket_id):
        return self.store.is_running(ticket_id)

    def channel_of(self, ticket_id):
        return self.store.channel_of(ticket_id)

    def records(self, ticket_id):
        """B27 的运行记录端口优先；没有记录时退回 B23 运行存储（旧装配仍只有 B23）。"""
        agent_runs = getattr(self.store, 'agent_runs', None)
        records = list(agent_runs(ticket_id)) if callable(agent_runs) else []
        return records or (list(self.runs.list(ticket_id=ticket_id)) if self.runs is not None else [])

    def conclusion(self, ticket_id):
        read_conclusion = getattr(self.source, 'conclusion', None)
        if callable(read_conclusion):
            return read_conclusion(ticket_id)
        raw = self.store.ticket_state(ticket_id, 'watchdog_conclusion') if hasattr(self.store, 'ticket_state') else None
        return json.loads(raw) if raw else None

    def save_conclusion(self, ticket_id, value):
        write_conclusion = getattr(self.source, 'save_conclusion', None)
        if callable(write_conclusion):
            write_conclusion(ticket_id, value)
            return
        self.store.set_ticket_state(ticket_id, 'watchdog_conclusion', json.dumps(value, ensure_ascii=False))


class Watchdog:
    """Uses B27 ticket views and B27/B23 run readers; never retries a business action.

    Recovery is limited to one fresh dependency probe per failure episode. No
    ticket is re-dispatched, no approval is changed, and no job is deleted.
    """
    def __init__(self, store, runs, writer, plant_id, probes, *, clock=now, sources=None, jobs=None):
        self.store, self.runs, self.writer, self.plant_id = store, runs, writer, plant_id
        self.probes, self.clock, self.jobs = probes, clock, jobs
        self._sources = sources
        self._current_view = None
        self._lock = threading.Lock()
        def initialize(c):
            c.execute("CREATE TABLE IF NOT EXISTS b42_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id TEXT NOT NULL, ticket_id TEXT, at TEXT NOT NULL, record TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS b42_attempts (plant_id TEXT NOT NULL, ticket_id TEXT NOT NULL, episode TEXT NOT NULL, result TEXT, PRIMARY KEY (plant_id, ticket_id, episode))")
        atomic(writer, initialize)

    def _save(self, scope, ticket_id, value):
        atomic(self.writer, lambda c: c.execute("INSERT INTO b42_checks(plant_id,ticket_id,at,record) VALUES (?,?,?,?)", (scope, ticket_id, value['checked_at'], json.dumps(value, ensure_ascii=False))))

    def _latest(self, ticket_id, scope=None):
        sql, params = "SELECT record FROM b42_checks WHERE ticket_id=?", [ticket_id]
        if scope is not None:
            sql += " AND plant_id=?"
            params.append(scope)
        row = read(self.writer, lambda c: c.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone())
        return json.loads(row[0]) if row else None

    def _views(self):
        sources = self._sources() if self._sources is not None else [self.store]
        return [_Inspection(source, self.runs) for source in sources]

    def _active_jobs(self, run_id):
        if not run_id:
            return []
        if self.jobs is not None:
            try:
                pending = self.jobs.snapshot(_read_context(self.plant_id), run_id=run_id, status='pending')
                running = self.jobs.snapshot(_read_context(self.plant_id), run_id=run_id, status='running')
                return [*pending.jobs, *running.jobs]
            except Exception:  # noqa: BLE001 — 读队列失败按没有活动任务处理，不中断巡检
                log.exception('watchdog job snapshot failed')
                return []
        rows = read(self.writer, lambda c: c.execute("SELECT record FROM jobs WHERE plant_id=? AND status IN ('pending','running')", (self.plant_id,)).fetchall())
        return [json.loads(row[0]) for row in rows if json.loads(row[0]).get('run_id') == run_id]

    def check(self):
        with self._lock:
            at = self.clock().isoformat()
            health = {}
            for name, probe in self.probes.items():
                try:
                    status, reason = probe()
                    if status not in ('ok', 'failed', 'unknown', 'disabled'):
                        status, reason = 'unknown', '检查结果无效'
                except Exception:
                    log.exception('watchdog probe failed: %s', name)
                    status, reason = 'unknown', '检查执行失败'
                health[name] = {'status': status, 'reason': reason, 'checked_at': at}
            self._save(self.plant_id, None, {'checked_at': at, 'health': health})
            for view in self._views():
                self._current_view = view
                try:
                    for ticket in view.open_tickets():
                        try:
                            self._inspect(ticket, health, at)
                        except Exception:
                            log.exception('watchdog ticket inspection failed: %s', ticket.ticket_id)
                except Exception:
                    log.exception('watchdog scope inspection failed: %s', view.plant_id)
                finally:
                    self._current_view = None
            return health

    def _inspect(self, ticket, health, at):
        view = self._current_view or self._views()[0]
        tid = ticket.ticket_id
        previous = self._latest(tid, view.plant_id)
        conclusion = view.conclusion(tid)
        records = view.records(tid)
        last = records[-1] if records else None
        probe = view.probe_of(tid)
        questions = view.questions(tid)
        run_id = view.run_of_ticket(tid)
        active_jobs = self._active_jobs(run_id)
        pending_human = any(q['status'] == 'pending' for q in questions) or view.channel_of(tid) == 'urgent'
        waiting = bool(active_jobs) or view.is_running(tid) or pending_human or bool(probe and probe.get('state') in ('pending', 'proposed', 'executing'))
        observation = [{'job_id': job['job_id'], 'status': job['status'], 'progress': job.get('progress')} for job in active_jobs]
        reason, evidence, recovery, status, episode = '', [], '未尝试', 'observing', None
        if ticket.status == 'blocked' and previous and previous.get('episode') and last and previous['episode'].startswith(last.agent_run_id + ':'):
            dep = previous['episode'].rsplit(':', 1)[1]
            reason, evidence, recovery, status = previous['reason'], previous['evidence'], previous['recovery'], 'blocked'
            episode = previous['episode']
            if health.get(dep, {}).get('status') == 'ok':
                if '后续巡检' not in recovery:
                    recovery += f'；后续巡检 {dep} 已连通，业务仍需按原流程处理'
        elif ticket.status == 'blocked':
            # B28/B47 先置受阻：已有结论优先沿用，重复巡检不把首轮证据降级成泛化原因（#151/#152）
            prior = conclusion if (conclusion and conclusion.get('reason') and conclusion.get('reason') not in _UNKNOWN_REASONS
                                   and conclusion.get('status') in ('blocked', 'observing')) else None
            if prior is None and previous and previous.get('status') == 'blocked' and previous.get('reason') not in _UNKNOWN_REASONS:
                prior = previous
            failed = [name for name in ('nexent', 'model') if health.get(name, {}).get('status') == 'failed']
            if prior is not None:
                reason, evidence = prior['reason'], list(prior.get('evidence') or [])
                recovery, status, episode = prior.get('recovery') or recovery, 'blocked', prior.get('episode')
                evidence = [*evidence, *[health[name]['reason'] for name in failed if health[name]['reason'] not in evidence]]
            elif last is not None and (last.status in ('failed', 'timeout', 'blocked') or failed):
                reason = f'事件单已受阻，关联 Agent 运行 {last.status}'
                if failed:
                    reason += f'；{failed[0]} 检查失败'
                evidence = [last.agent_run_id, *[health[name]['reason'] for name in failed]]
                status = 'blocked'
            else:
                reason, recovery, status = '事件单已受阻，原受阻原因未知', '未知', 'blocked'
        elif waiting:
            reason = '等待人工确认、试探观察或后台执行，继续观察'
            if active_jobs and previous and previous.get('job_observation') == observation:
                for job in active_jobs:
                    started = job.get('started_at') or job.get('submitted_at')
                    if started and (self.clock() - datetime.fromisoformat(started.replace('Z', '+00:00'))).total_seconds() > job.get('budget', {}).get('time_limit_s', 300):
                        reason = f"任务 {job['job_id']} 超过预算且两次检查进度未变；仍在执行或排队，需人工核查"
                        evidence = [job['job_id']]
                        break
        elif ticket.status == 'waiting':
            reason = '事件单等待中，但未找到等待依据；原因未知，需人工核查'
        elif last and any(c.business_status in ('forbidden', 'PLANTKG_E_FORBIDDEN', 'cross_plant', 'PLANTKG_E_CROSS_PLANT') or any(code in (c.summary or '') for code in ('PLANTKG_E_FORBIDDEN', 'PLANTKG_E_CROSS_PLANT')) for c in last.tool_calls):
            reason = '运行记录显示权限边界拒绝'; evidence = [last.agent_run_id]
            if last.status in ('failed', 'timeout', 'blocked'):
                status = 'blocked'
        elif last and last.status in ('failed', 'timeout', 'blocked'):
            failed = [name for name in ('nexent', 'model') if health.get(name, {}).get('status') == 'failed']
            if failed:
                dep = failed[0]
                reason = f'{dep} 检查失败，关联 Agent 运行 {last.status}'
                evidence = [last.agent_run_id, health[dep]['reason']]
                episode = f'{last.agent_run_id}:{dep}'
                claimed = atomic(self.writer, lambda c: c.execute("INSERT OR IGNORE INTO b42_attempts(plant_id,ticket_id,episode) VALUES (?,?,?)", (view.plant_id, tid, episode)).rowcount == 1)
                if claimed:
                    try:
                        state, detail = self.probes[dep]()
                        recovery = f'重新检查 {dep}：{state}（{detail}）'
                    except Exception:
                        state, recovery = 'unknown', f'重新检查 {dep} 失败'
                    atomic(self.writer, lambda c: c.execute("UPDATE b42_attempts SET result=? WHERE plant_id=? AND ticket_id=? AND episode=?", (recovery, view.plant_id, tid, episode)))
                    if state == 'ok':
                        reason = f'{dep} 重新检查已恢复；业务运行仍需人工或原有流程处理'
                        status = 'observing'
                    else:
                        status = 'blocked'
                else:
                    row = read(self.writer, lambda c: c.execute("SELECT result FROM b42_attempts WHERE plant_id=? AND ticket_id=? AND episode=?", (view.plant_id, tid, episode)).fetchone())
                    recovery = (row[0] if row and row[0] else '先前重新检查结果未知，不重复执行') + '；当前依赖检查失败'
                    status = 'blocked'
            else:
                reason = 'Agent 运行失败；依赖故障证据不足，原因未知'
                evidence = [last.agent_run_id]
        elif len(records) >= 3 and all(str(r.kind) in ('investigate', 'AgentResultKind.investigate') and r.final_answer and r.final_answer.strip() == records[-1].final_answer.strip() for r in records[-3:]) and (records[-1].timing.requested_at - records[-3].timing.requested_at).total_seconds() >= 300:
            reason = '最近三次调查结论相同，需结合证据检查是否有实质进展'
            evidence = [r.agent_run_id for r in records[-3:]]
        elif last and last.timing.returned_at and (self.clock() - last.timing.returned_at).total_seconds() > 1800:
            reason = '最近运行结束后长时间未见新运行；实质进展原因未知，需人工核查'
            evidence = [last.agent_run_id]
        else:
            reason = '未发现明确受阻证据；无进展原因未知' if not records else '最近运行未显示明确故障'
        if status == 'observing' and conclusion and reason in _UNKNOWN_REASONS and conclusion.get('status') in ('blocked', 'observing'):
            # 恢复结论保留到事件单关闭或新的失败 episode：巡检没发现新证据时不覆盖（#152）
            reason = conclusion.get('reason') or reason
            evidence = list(conclusion.get('evidence') or [])
            recovery = conclusion.get('recovery') or recovery
            episode = conclusion.get('episode') or episode
        if status == 'blocked' and ticket.status == 'open':
            view.store.block_ticket_if_idle(tid, ticket.round)
        current = view.ticket(tid)
        if current:
            status = 'blocked' if current.status == 'blocked' else ('observing' if status == 'blocked' else status)
        value = {'ticket_id': tid, 'status': status, 'ticket_status': current.status if current else 'unknown', 'reason': reason,
                 'checked_at': at, 'recovery': recovery, 'evidence': evidence, 'job_observation': observation}
        if episode:
            value['episode'] = episode
        self._save(view.plant_id, tid, value)
        conclusion_value = {key: value[key] for key in ('ticket_id', 'status', 'ticket_status', 'reason', 'evidence', 'recovery', 'checked_at')}
        if episode:
            conclusion_value['episode'] = episode
        view.save_conclusion(tid, conclusion_value)

    def health(self):
        row = read(self.writer, lambda c: c.execute("SELECT record FROM b42_checks WHERE plant_id=? AND ticket_id IS NULL ORDER BY id DESC LIMIT 1", (self.plant_id,)).fetchone())
        return json.loads(row[0]) if row else {'checked_at': '', 'health': {}}

    def blocked(self):
        result = []
        for view in self._views():
            for ticket in view.open_tickets():
                if ticket.status == 'blocked':
                    result.append(view.conclusion(ticket.ticket_id) or self._latest(ticket.ticket_id, view.plant_id)
                                  or {'ticket_id': ticket.ticket_id, 'status': 'blocked', 'ticket_status': 'blocked', 'reason': '原因未知：尚无巡检记录', 'checked_at': '', 'recovery': '未知', 'evidence': []})
        return result

    def explain(self, question):
        match = re.search(r'TK-\d+', question, re.I)
        if not match:
            return {'answer': '请提供事件单编号，例如 TK-0003。'}
        short = match.group().upper()
        ticket = item = None
        for view in self._views():
            found = [t for t in view.open_tickets() if t.ticket_id.rsplit('/', 1)[-1].upper() == short]
            if found:
                ticket = found[0]
                item = view.conclusion(ticket.ticket_id) or self._latest(ticket.ticket_id, view.plant_id)
                break
        if ticket is None:
            return {'answer': f'{short} 不在当前打开的事件单中，原因未知。'}
        if not item:
            return {'answer': f'{short} 当前状态 {ticket.status}；尚无巡检记录，原因未知。'}
        evidence = '、'.join(item['evidence']) or '无明确故障证据'
        return {'answer': f"{short} 当前状态 {ticket.status}；原因：{item['reason']}；依据：{evidence}；恢复：{item['recovery']}；最近检查：{item['checked_at']}。建议维护者核对运行记录，按既有审批流程处理。"}


def install(registry, settings, writer, runs, jobs=None):
    if writer is None:
        return None
    routers = registry.hooks_for('router.service')
    if not registry.hooks_for(WATCHDOG_TICKETS_HOOK) and not routers:
        return None
    probes = {'nexent': (lambda: http_probe(settings.nexent.northbound_url.rstrip('/') + '/nb/v1/health')) if settings.nexent.enabled else (lambda: ('disabled', '未启用')),
              'model': lambda: http_probe(os.environ.get('PLANTKG_MODEL_HEALTH_URL')),
              'jobs': lambda: queue_probe(jobs, writer, settings.plant_id)}
    def sources():
        # 每次巡检重新取视图：协同组运行结束后视图自然消失，不把上一轮的作用域当成当前运行
        found = [source for hook in registry.hooks_for(WATCHDOG_TICKETS_HOOK) for source in hook()]
        return found or [router.store for router in registry.hooks_for('router.service')]
    watchdog = Watchdog(routers[-1].store if routers else None, runs, writer, settings.plant_id, probes, sources=sources, jobs=jobs)
    registry.register_rest('GET', '/watchdog/health', lambda context, path, query: watchdog.health())
    registry.register_rest('GET', '/watchdog/blocked', lambda context, path, query: {'items': watchdog.blocked()})
    registry.register_rest('GET', '/watchdog/explain', lambda context, path, query: watchdog.explain(query.get('question', '')))
    registry.register_hook('watchdog.check', watchdog.check)  # 同步巡检入口：装配自检与测试不等 60 秒后台轮询
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                watchdog.check()
            except Exception:
                log.exception('watchdog check failed')
            stop.wait(60)
    thread = threading.Thread(target=loop, name='plantkg-watchdog', daemon=True)
    thread.start()
    def close():
        stop.set()
        thread.join(timeout=10)
    return close
