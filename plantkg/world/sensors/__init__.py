"""观测层（B22 / D59）：传感器读数、残差与候选、虚拟传感器、5 种传感器故障注入。"""
from .bus import ObservationBus
from .config import DEFAULT_CONFIG, SensorConfig, apply_sensor_overrides, load_sensor_config
from .faults import SensorFault, SensorFaultInjector, TrialActionSim
from .layer import SensorLayer, query_snapshot
from .query import install

__all__ = ["DEFAULT_CONFIG", "ObservationBus", "SensorConfig", "SensorFault", "SensorFaultInjector", "SensorLayer",
           "TrialActionSim", "apply_sensor_overrides", "install", "load_sensor_config", "query_snapshot"]
