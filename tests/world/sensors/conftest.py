from __future__ import annotations

import pytest

from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario
from plantkg.world.sensors import SensorFaultInjector


def sensor_scenario(**kw) -> Scenario:
    base = {"scenario_id": "sensor-test", "title": "观测层测试", "nature": "synthetic", "date": "2026-07-01", "days": 1,
            "seed": 7, "initial": {"soc": 0.5}, "events": []}
    return Scenario.model_validate(base | kw)


@pytest.fixture
def make(tmp_path):
    opened = []

    def build(sc: Scenario | None = None, *, faults=(), name: str = "sensors.db"):
        sc = sc or sensor_scenario()
        world = build_world(sc)
        if faults:  # 观测层与传感器故障都由设备包的 SIM_HOOK 提供，只替换故障计划
            world.simulators = [SensorFaultInjector(faults) if getattr(s, "name", "") == "sensor_faults" else s
                                for s in world.simulators]
        db = open_db(tmp_path / name, world.site.plant_id)
        opened.append(db)
        runner = ScenarioRunner(world, db, mode="evaluation")
        runner.start()
        return runner, sc

    yield build
    for db in opened:
        db.close()
