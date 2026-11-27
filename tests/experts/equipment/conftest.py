from __future__ import annotations

from pathlib import Path

import pytest

from plantkg.experts.equipment.expert import EquipmentExpert
from plantkg.world.sim.faults import load_calibration

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def calibration():
    return load_calibration()


@pytest.fixture
def expert() -> EquipmentExpert:
    return EquipmentExpert()
