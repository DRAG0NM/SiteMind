"""#243：evaluation 模式下主 Agent 的来源记录与 live 回退同一口径。"""
import pytest

from plantkg.agent.router import install
from plantkg.agent.router.severity import SeverityEstimator
from plantkg.core.registry import Registry
from plantkg.evaluation.cache import ResultCache, install_cache
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from tests.agent.router.helpers import AIR, PLANT, SEVERITY, T0, make_event


class FakeLive:
    """离线假 live 来源：只返回结果，不联网。"""
    cache_source = 'nexent'

    def get(self, kind, binding, payload):
        return {'status': 'finished', 'final_answer': '已汇报', 'latency_ms': 37}


@pytest.fixture
def make_router(tmp_path):
    writers = []

    def build(collector):
        writer = SQLiteWriter(tmp_path / f'r{len(writers)}.db')
        writers.append(writer)
        MigrationRunner(writer).apply()
        registry = Registry()
        registry.live_fallback = True
        router = install(registry, writer, PLANT, run_id='run-1', knowledge_version=3, mode='evaluation',
                         state_version=7, severity=SeverityEstimator(SEVERITY))
        install_cache(registry, ResultCache(tmp_path / 'cache.json', 'v1'),
                      {'scenario': 's', 'prompt_version': 'p', 'data_version': 'd', 'collector': collector,
                       'resolve': lambda binding: ('EV-1', {'world': {}})}, ['triage'])
        return router
    yield build
    for writer in writers:
        writer.close()


def _flush(router, ticket_id):
    router.store.add_pending('pend-1', at=T0, kind='report', ticket_id=ticket_id, record={'summary': '压力异常', 'round': 1})
    return router.flush()


def test_triage_collection_is_nexent_with_latency_and_replay_is_cache(make_router):
    router = make_router(FakeLive())
    ticket_id = router.handle_event(make_event('EV-1', 0, 'compressor_pressure_drop', AIR), flush=False).ticket_id
    router.store.take_pending([i['item_id'] for i in router.store.pending()])
    first = _flush(router, ticket_id)
    record = router.runs[-1]
    assert (record.source, record.run_mode, record.coverage_gap) == ('nexent', 'evaluation', False)
    assert record.timing.latency_ms == 37 and first.source == 'nexent'
    _flush(router, ticket_id)  # 同一内容再来一次：命中缓存，不再等待
    record = router.runs[-1]
    assert (record.source, record.run_mode, record.timing.latency_ms) == ('cache', 'evaluation', 0)


def test_cache_miss_falls_back_to_deterministic_run_mode(make_router):
    router = make_router(None)
    ticket_id = router.handle_event(make_event('EV-1', 0, 'compressor_pressure_drop', AIR), flush=False).ticket_id
    router.store.take_pending([i['item_id'] for i in router.store.pending()])
    _flush(router, ticket_id)
    record = router.runs[-1]
    assert (record.source, record.run_mode, record.coverage_gap) == ('deterministic', 'deterministic', True)
