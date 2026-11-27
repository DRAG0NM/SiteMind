"""#238 方案 1：设备包的空压限值经场景注册表钩子 export.config_metrics 进 day.json 的 config 组。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario


def test_equipment_config_metrics_come_from_config(tmp_path):
    sc = Scenario.model_validate({"scenario_id": "config-metrics", "title": "限值导出", "date": "2026-07-01",
                                  "seed": 7, "initial": {"soc": 0.5}})
    world = build_world(sc)
    db = open_db(tmp_path / "config_metrics.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        metrics = runner.registry.hooks_for("export.config_metrics")[-1](world.site, runner.policy)
        config = yaml.safe_load((Path(world.site.config_path).parent / "equipment.yaml").read_text(encoding="utf-8"))["compressed_air"]
        values = {m.metric: m for m in metrics}
        assert set(values) == {"compressed_air_floor_mpa", "compressed_air_leak_threshold"}
        assert values["compressed_air_floor_mpa"].value == config["floor_mpa"]
        assert values["compressed_air_leak_threshold"].value == config["leak_threshold_mpa_per_min"]
        assert all(m.group == "config" and m.nature == "configured" for m in metrics)
        assert runner.registry.hooks_for("export.config_metrics")[-1](SimpleNamespace(config_path=tmp_path / "site.yaml"), None) == []
    finally:
        db.close()
