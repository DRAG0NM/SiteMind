from pathlib import Path

import pytest

from plantkg.evaluation.orchestration import run_probe
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.runner import ScenarioRunner
from plantkg.world.scenario.script import load_scenario


def test_b17_sources_are_recorded_and_inherited(tmp_path):
    scenario = load_scenario('configs/scenarios/factory_demo_day.yaml')
    world = build_world(scenario)
    db = open_db(tmp_path / 'source.db', world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        root = runner.start()
        assert root.data_sources[0].source == world.inputs.loads.base_kind
        assert root.data_sources[0].dataset_version == world.inputs.loads.base_digest
        runner.run_until(scenario.instant('07:31'))
        assert len(runner.trace.runs) > 1
        assert all(db.runs.get_run(r.run_id).data_sources == root.data_sources for r in runner.trace.runs)
    finally:
        db.close()


def test_d54_runs_both_routes_offline():
    report = run_probe(Path('configs/scenarios/factory_demo_day.yaml'))
    assert [r['count'] for r in report['routes']] == [10, 10]
    assert report['routes'][0]['route'] == 'A_programmatic'
    assert report['routes'][1]['route'] == 'B_fast_slow'
    assert all(0 <= r['routing_accuracy'] <= 1 and 0 <= r['evidence_completeness'] <= 1 for r in report['routes'])
    assert report['routes'][0]['routing_accuracy'] > 0  # Known types are routable by the programmatic baseline.
    for route in report['routes']:
        assert sum(route[k] for k in ('fast_fraction', 'slow_fraction', 'urgent_fraction', 'unrouted_fraction', 'programmatic_fraction')) == pytest.approx(1)
    route_a, route_b = report['routes']
    assert (route_a['programmatic_fraction'], route_a['fast_fraction'], route_a['slow_fraction'],
            route_a['urgent_fraction'], route_a['unrouted_fraction']) == (1, 0, 0, 0, 0)
    assert route_b['fast_fraction'] == pytest.approx(0.7)
    assert route_b['urgent_fraction'] == pytest.approx(0.3)
    assert (route_b['slow_fraction'], route_b['unrouted_fraction'], route_b['programmatic_fraction']) == (0, 0, 0)
    assert 'not yet performed' in report['model']


def test_d54_collection_failures_are_visible():
    # 超限/失败会被缓存层吞掉并退回离线结果；产物必须留下计数，否则会“看起来跑成功、实际全是回退”
    class Failing:
        def get(self, kind, binding, payload):
            raise RuntimeError('budget limit reached')

    report = run_probe(Path('configs/scenarios/factory_demo_day.yaml'), collector=Failing(), model_version='qwen4-max')
    assert report['collection_complete'] is False
    assert report['collection']['collected'] == 0 and report['collection']['failures'] > 0
    assert report['collection']['errors'][0]['message'] == 'budget limit reached'
    assert 'collection' in report['routes'][1] and 'collection' not in report['routes'][0]
