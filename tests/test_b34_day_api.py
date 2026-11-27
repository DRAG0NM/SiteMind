from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.world.scenario import ScenarioRunner, day_api
from plantkg.world.scenario.__main__ import build_world, catalog_metrics_provider, open_db
from plantkg.world.scenario.export import export_day
from plantkg.world.scenario.runner import default_registry
from plantkg.world.scenario.script import Scenario


def test_exported_day_is_available_by_run_id(tmp_path, monkeypatch):
    sample = Path(__file__).resolve().parents[1] / "contracts/examples/day.sample.json"
    run_id = "run-scn-20260701-f1"
    (tmp_path / f"{run_id}.json").write_bytes(sample.read_bytes())
    monkeypatch.setattr(day_api, "DAY_DIR", tmp_path)
    services = build_services(load_settings())
    with TestClient(create_app(services)) as client:
        response = client.get(f"/api/v1/demo/day/{run_id}")
        assert response.status_code == 200
        assert response.json()["events"][0]["summary"] == "早班到岗，生产楼电梯群候梯 42 人"
        assert client.get("/api/v1/demo/day/missing").status_code == 404
        # An export requested from a parent can identify the chain's final run.
        (tmp_path / "parent-run.json").write_bytes(sample.read_bytes())
        mismatch = client.get("/api/v1/demo/day/parent-run")
        assert mismatch.status_code == 400
        assert run_id in mismatch.json()["error"]["message"]


def test_day_api_returns_config_metrics_from_real_export(tmp_path, monkeypatch):
    """#238：真实导出的 config 组限值经 GET /api/v1/demo/day/{run_id} 原样返回。"""
    sc = Scenario.model_validate({"scenario_id": "api-config", "title": "限值导出", "date": "2026-07-01", "seed": 1,
                                  "initial": {"soc": 0.5}})
    world = build_world(sc)
    db = open_db(tmp_path / "api.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db).run(until=datetime(2026, 7, 1, 0, 5, tzinfo=timezone.utc))
        record = export_day(db, trace.run_id, "2026-07-01", registry=default_registry(), site=world.site,
                            asset_metrics_provider=catalog_metrics_provider(db))
    finally:
        db.close()
    (tmp_path / f"{trace.run_id}.json").write_text(record.model_dump_json(indent=1), encoding="utf-8")
    monkeypatch.setattr(day_api, "DAY_DIR", tmp_path)
    with TestClient(create_app(build_services(load_settings()))) as client:
        response = client.get(f"/api/v1/demo/day/{trace.run_id}")
    assert response.status_code == 200
    configured = [m for m in response.json()["metrics"] if m["group"] == "config"]
    assert {m["metric"] for m in configured} == {"transformer_limit_pct", "contract_capacity_kw", "battery_soc_floor_pct",
                                                 "hvac_supply_setpoint_c", "compressed_air_floor_mpa",
                                                 "compressed_air_leak_threshold"}
    assert all(m["nature"] == "configured" for m in configured)

    knowledge = {m["metric"]: m for m in response.json()["metrics"] if m["group"] == "knowledge"}
    assert knowledge["asset_total"]["value"] == 0
    assert knowledge["asset_state_counts"]["counts"] == dict(dormant=0, identified=0, in_graph=0, referenced=0)
    assert knowledge["asset_activation_rate"]["value"] is None
