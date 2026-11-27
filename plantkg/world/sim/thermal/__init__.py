"""区域温度模型（B21）：一阶 RC、舒适区间与 ThermalSim（替换 B17 的 HvacSim）。"""

from .model import COOLING_TYPES, DEFAULT_CONFIG, HVAC_TYPES, ZoneSpec, control, load_zones
from .simulator import ThermalSim

__all__ = ["COOLING_TYPES", "DEFAULT_CONFIG", "HVAC_TYPES", "ThermalSim", "ZoneSpec", "control", "load_zones"]
