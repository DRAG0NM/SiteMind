from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario

UTC = timezone.utc


def local(hhmm: str, day: int = 1) -> datetime:
    """2026-07-0{day} 厂区当地钟面 → UTC。"""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 7, day, h, m, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


def scenario(**kw) -> Scenario:
    base = {"scenario_id": "test", "title": "测试", "date": "2026-07-01", "seed": 7, "initial": {"soc": 0.5}}
    return Scenario.model_validate(base | kw)


@pytest.fixture
def make(tmp_path):
    opened = []

    def build(sc: Scenario | None = None, *, mode: str = "evaluation", simulators=None, registry=None, wall=None, name: str = "s.db"):
        sc = sc or scenario()
        world = build_world(sc, simulators=simulators)
        db = open_db(tmp_path / name, world.site.plant_id)
        opened.append(db)
        kw = {"wall": wall} if wall else {}
        runner = ScenarioRunner(world, db, registry=registry, mode=mode, **kw)
        runner.start()
        return runner

    yield build
    for db in opened:
        db.close()
