from starlette.testclient import TestClient

from plantkg.agent.watchdog.demo import demo_app, exercise


def test_synthetic_watchdog_recovery_and_explanation(tmp_path):
    writer, watchdog, answer = exercise(tmp_path)
    try:
        item = watchdog.blocked()[0]
        assert item['ticket_id'] == 'ticket://TK-0001'
        assert item['ticket_status'] == 'blocked'
        assert item['reason'] and item['checked_at'] and item['recovery']
        assert '原因' in answer and '依据' in answer and '恢复' in answer
        assert '未知' in watchdog.explain('为什么 TK-9999 受阻')['answer']
        with TestClient(demo_app(watchdog)) as client:
            response = client.get('/api/v1/watchdog/blocked')
            assert response.status_code == 200
            assert response.json()['items'][0]['ticket_id'] == item['ticket_id']
            assert 'synthetic-failed-0001' in client.get('/api/v1/watchdog/explain', params={'question': '为什么 TK-0001 受阻'}).json()['answer']
            assert client.get('/api/v1/watchdog/health').json()['health']['model']['status'] == 'ok'
    finally:
        writer.close()


def test_registered_read_only_routes(tmp_path):
    from types import SimpleNamespace
    from plantkg.api import create_app
    from plantkg.agent.watchdog import install
    from plantkg.core.registry import Registry

    writer, watchdog, _ = exercise(tmp_path)
    registry = Registry()
    registry.register_hook('router.service', SimpleNamespace(store=watchdog.store))
    settings = SimpleNamespace(plant_id='synthetic', nexent=SimpleNamespace(enabled=False, base_url=''))
    close = install(registry, settings, writer, watchdog.runs)
    services = SimpleNamespace(registry=registry, settings=settings, guard=SimpleNamespace(anonymous_read_context=lambda: None), close=close)
    try:
        with TestClient(create_app(services)) as client:
            assert client.get('/api/v1/watchdog/blocked').json()['items'][0]['ticket_status'] == 'blocked'
            assert 'TK-0001' in client.get('/api/v1/watchdog/explain', params={'question': '为什么 TK-0001 受阻'}).json()['answer']
    finally:
        writer.close()


def test_recheck_recovers_without_blocking_or_duplicate_recovery(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from plantkg.agent.watchdog import Watchdog
    from plantkg.core.contracts import Ticket

    writer, first, _ = exercise(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        first.store.save_ticket(Ticket(ticket_id='ticket://TK-0004', status='open', opened_at=at, round=1), run_id='run-4')
        record = SimpleNamespace(agent_run_id='ar-4', status='failed', tool_calls=[], final_answer=None,
                                 timing=SimpleNamespace(requested_at=at, returned_at=at))
        class Runs:
            def list(self, *, ticket_id):
                return [record] if ticket_id.endswith('0004') else []
        responses = iter([('failed', '断开'), ('ok', '已恢复'), ('ok', '已恢复'), ('failed', '再次断开')])
        calls = []
        def probe():
            calls.append(1)
            return next(responses)
        watchdog = Watchdog(first.store, Runs(), writer, 'synthetic', {'model': probe})
        watchdog.check()
        assert first.store.ticket('ticket://TK-0004').status == 'open'
        assert '已恢复' in watchdog.explain('为什么 TK-0004 受阻')['answer']
        watchdog.check()
        watchdog = Watchdog(first.store, Runs(), writer, 'synthetic', {'model': probe})
        watchdog.check()
        assert len(calls) == 4  # 同一运行再次失败不重复执行恢复
        assert first.store.ticket('ticket://TK-0004').status == 'blocked'
    finally:
        writer.close()


def test_explicit_permission_rejection_is_reported_without_escalating_access(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from plantkg.agent.watchdog import Watchdog
    from plantkg.core.contracts import Ticket

    writer, first, _ = exercise(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        first.store.save_ticket(Ticket(ticket_id='ticket://TK-0005', status='open', opened_at=at, round=1), run_id='run-5')
        call = SimpleNamespace(business_status='failed', summary='PLANTKG_E_FORBIDDEN: 拒绝')
        record = SimpleNamespace(agent_run_id='ar-5', status='failed', tool_calls=[call], final_answer=None,
                                 timing=SimpleNamespace(requested_at=at, returned_at=at))
        class Runs:
            def list(self, *, ticket_id):
                return [record] if ticket_id.endswith('0005') else []
        watchdog = Watchdog(first.store, Runs(), writer, 'synthetic', {})
        watchdog.check()
        assert first.store.ticket('ticket://TK-0005').status == 'blocked'
        assert '权限边界拒绝' in watchdog.explain('为什么 TK-0005 受阻')['answer']
    finally:
        writer.close()


def test_active_job_over_budget_needs_two_observations_without_blocking(tmp_path):
    from datetime import datetime, timedelta, timezone
    from plantkg.agent.watchdog import Watchdog
    from plantkg.core.contracts import Job, Ticket
    from plantkg.core.versions import atomic

    writer, first, _ = exercise(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        first.store.save_ticket(Ticket(ticket_id='ticket://TK-0006', status='open', opened_at=at, round=1), run_id='run-6')
        job = Job(job_id='job-6', kind='run_day', idempotency_key='key-6', status='running',
                  submitted_at=at-timedelta(hours=1), started_at=at-timedelta(hours=1), budget={'time_limit_s': 60},
                  progress=0.2, run_id='run-6')
        atomic(writer, lambda c: c.execute('INSERT INTO jobs(plant_id,job_id,idempotency_key,request_hash,status,record) VALUES (?,?,?,?,?,?)',
                                           ('synthetic', job.job_id, job.idempotency_key, 'hash', 'running', job.model_dump_json())))
        watchdog = Watchdog(first.store, first.runs, writer, 'synthetic', {})
        watchdog.check()
        assert '超过预算' not in watchdog.explain('为什么 TK-0006 等待')['answer']
        watchdog.check()
        assert '超过预算且两次检查进度未变' in watchdog.explain('为什么 TK-0006 等待')['answer']
        assert first.store.ticket('ticket://TK-0006').status == 'open'
    finally:
        writer.close()


def test_finished_run_with_permission_rejection_is_only_observed(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from plantkg.agent.watchdog import Watchdog
    from plantkg.core.contracts import Ticket

    writer, first, _ = exercise(tmp_path)
    try:
        at = datetime.now(timezone.utc)
        tid = 'ticket://TK-0007'
        first.store.save_ticket(Ticket(ticket_id=tid, status='open', opened_at=at, round=1), run_id='run-7')
        call = SimpleNamespace(business_status='failed', summary='PLANTKG_E_FORBIDDEN: 拒绝')
        record = SimpleNamespace(agent_run_id='ar-7', status='finished', tool_calls=[call], final_answer=None,
                                 timing=SimpleNamespace(requested_at=at, returned_at=at))
        class Runs:
            def list(self, *, ticket_id):
                return [record] if ticket_id == tid else []
        watchdog = Watchdog(first.store, Runs(), writer, 'synthetic', {})
        watchdog.check()
        assert first.store.ticket(tid).status == 'open'
        assert '权限边界拒绝' in watchdog.explain('为什么 TK-0007 受阻')['answer']
    finally:
        writer.close()


def test_install_probes_northbound_health(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import plantkg.agent.watchdog as module
    from plantkg.core.registry import Registry

    writer, watchdog, _ = exercise(tmp_path)
    registry = Registry()
    registry.register_hook('router.service', SimpleNamespace(store=watchdog.store))
    urls = []
    monkeypatch.setattr(module, 'http_probe', lambda url: (urls.append(url) or ('ok', 'HTTP 200')))
    settings = SimpleNamespace(plant_id='synthetic', nexent=SimpleNamespace(enabled=True, northbound_url='http://nexent:5013/'))
    close = module.install(registry, settings, writer, watchdog.runs)
    try:
        close()
        assert 'http://nexent:5013/nb/v1/health' in urls
    finally:
        writer.close()


def test_one_ticket_inspection_failure_does_not_stop_others(tmp_path, monkeypatch, caplog):
    from datetime import datetime, timezone
    from plantkg.core.contracts import Ticket

    writer, watchdog, _ = exercise(tmp_path)
    try:
        tid = 'ticket://TK-0008'
        watchdog.store.save_ticket(Ticket(ticket_id=tid, status='open', opened_at=datetime.now(timezone.utc), round=1), run_id='run-8')
        original = watchdog._inspect
        def inspect(ticket, health, at):
            if ticket.ticket_id == 'ticket://TK-0001':
                raise ValueError('bad ticket')
            return original(ticket, health, at)
        monkeypatch.setattr(watchdog, '_inspect', inspect)
        watchdog.check()
        assert 'watchdog ticket inspection failed: ticket://TK-0001' in caplog.text
        assert watchdog._latest(tid)['ticket_id'] == tid
    finally:
        writer.close()
