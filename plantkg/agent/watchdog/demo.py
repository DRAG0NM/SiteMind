"""Repeatable synthetic B42 demonstration: python -m plantkg.agent.watchdog.demo"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from plantkg.agent.router.store import RouterStore
from plantkg.agent.watchdog import Watchdog
from plantkg.core.contracts import Ticket
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter


def exercise(directory):
    writer = SQLiteWriter(Path(directory) / 'watchdog.db')
    MigrationRunner(writer).apply()
    store = RouterStore(writer, 'synthetic')
    at = datetime.now(timezone.utc)
    for number, status in ((1, 'open'), (2, 'waiting'), (3, 'open')):
        store.save_ticket(Ticket(ticket_id=f'ticket://TK-{number:04}', status=status, opened_at=at, round=1), run_id=f'run-{number}')
    def record(name):
        return SimpleNamespace(agent_run_id=name, status='failed', tool_calls=[], final_answer=None,
                               timing=SimpleNamespace(requested_at=at, returned_at=at))
    class Runs:
        def list(self, *, ticket_id):
            return [record('synthetic-failed-' + ticket_id[-4:])] if ticket_id.endswith(('0001', '0002')) else []
    class Model:
        state = 'failed'
        calls = 0
        def __call__(self):
            self.calls += 1
            return self.state, 'synthetic 模型连接' if self.state == 'ok' else 'synthetic 模型断开'
    model = Model()
    watchdog = Watchdog(store, Runs(), writer, 'synthetic', {'nexent': lambda: ('ok', 'synthetic'),
                        'model': model, 'jobs': lambda: ('ok', 'synthetic 空队列')})
    watchdog.check()
    assert store.ticket('ticket://TK-0001').status == 'blocked'
    assert store.ticket('ticket://TK-0002').status == 'waiting'
    assert store.ticket('ticket://TK-0003').status == 'open'
    first = watchdog.blocked()[0]
    assert 'failed' in first['recovery']
    model.state = 'ok'
    watchdog.check()
    assert model.calls == 3  # 一次异常只进行一次额外重新检查
    answer = watchdog.explain('为什么 TK-0001 一直没有处理完成？')['answer']
    assert '后续巡检 model 已连通' in answer
    assert 'synthetic-failed-0001' in answer
    return writer, watchdog, answer


def demo_app(watchdog):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    async def blocked(request):
        return JSONResponse({'items': watchdog.blocked()})
    async def health(request):
        return JSONResponse(watchdog.health())
    async def explain(request):
        return JSONResponse(watchdog.explain(request.query_params.get('question', '')))
    dist = Path(__file__).resolve().parents[3] / 'web' / 'dist'
    return Starlette(routes=[Route('/api/v1/watchdog/blocked', blocked), Route('/api/v1/watchdog/health', health),
                             Route('/api/v1/watchdog/explain', explain), Mount('/', StaticFiles(directory=dist, html=True, check_dir=False))])


def main():
    import sys
    with TemporaryDirectory() as directory:
        writer, watchdog, answer = exercise(directory)
        try:
            print('[synthetic] 受阻事件单：', watchdog.blocked())
            print('[synthetic] 维护者查询：', answer)
            print('[synthetic] 合理等待与正常事件单未误标受阻')
            if '--serve' in sys.argv:
                import uvicorn
                print('[synthetic] 工作台：http://127.0.0.1:8700 （先构建 web/dist）')
                uvicorn.run(demo_app(watchdog), host='127.0.0.1', port=8700)
        finally:
            writer.close()


if __name__ == '__main__':
    main()
