"""B19 能源专家冒烟测试的公共夹具：场景、本地时刻与演示装配（每个用例一份 SQLite）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from plantkg.experts.energy.assembly import build_demo_assembly
from plantkg.experts.energy.day import open_demo_inputs, run_day
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]
TZ = ZoneInfo("Asia/Shanghai")


def local(hhmm: str, day: int = 1) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 7, day, h, m, tzinfo=TZ).astimezone(timezone.utc)


@pytest.fixture
def scene() -> object:
    return load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")


@pytest.fixture
def make_assembly(tmp_path):
    opened = []

    def build(name: str = "energy.db", plant_id: str = "demo-electronics"):
        assembly = build_demo_assembly(tmp_path / name, plant_id)
        opened.append(assembly)
        return assembly

    yield build
    for assembly in opened:
        assembly.close()


@pytest.fixture
def day(scene, make_assembly):
    """演示日（2026-07-01）的日前计划 + 门控 + 计划账本。"""
    assembly = make_assembly()
    return run_day(scenario=scene, assembly=assembly, date="2026-07-01")


@pytest.fixture
def inputs(scene):
    return open_demo_inputs(scene, profile="dev")
