"""电梯群控仿真（B20）。

- `sim.py`：SimPy 离散事件模型，人流曲线 / 物料任务 → 派梯 → 候梯与能耗；
- `scenario.py`：B17 场景世界的分钟步进适配器，把电梯功率写进 `WorldState.power_kw` 供账本汇总。
"""

from .config import CarSpec, ElevatorConfig
from .scenario import ScenarioElevatorSim
from .sim import (
    FreightResult,
    FreightStrategy,
    FreightTask,
    Passenger,
    PassengerDemand,
    PassengerResult,
    PassengerStrategy,
    build_demand,
    run_freight_sim,
    run_passenger_sim,
)

__all__ = [
    "CarSpec", "ElevatorConfig", "FreightResult", "FreightStrategy", "FreightTask", "Passenger", "PassengerDemand",
    "PassengerResult", "PassengerStrategy", "ScenarioElevatorSim", "build_demand", "run_freight_sim", "run_passenger_sim",
]
