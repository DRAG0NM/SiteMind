"""B19 能源专家：日前计划、实时削峰与照明回路。

- `run_day(site, date)`：预测 → 共享规划（B09）→ 门控（B15）→ 场景执行（B17）→ 账本（B08）；
- `EnergyExpert.propose(event, state)`：事件级确定性提案（储能放电削峰、照明回路切换）；
- `RealtimeController.step(minute_state, slot_target)`：分钟层跟踪 15 分钟层目标线；
- `install(registry)`：把 `expert_propose:energy` 钩子挂到注册表，供 `plantkg_energy_propose` 工具调用。

看效果：`python -m plantkg.experts.energy.demo configs/scenarios/factory_demo_day.yaml`。
"""

from plantkg.experts.energy.day import DayRun, Replan, open_demo_inputs, run_day
from plantkg.experts.energy.proposal import EnergyExpert, EnergyExpertSource, energy_state_hook, install, register_agent_source
from plantkg.experts.energy.realtime import RealtimeController, SlotTarget

__all__ = ["DayRun", "EnergyExpert", "EnergyExpertSource", "RealtimeController", "Replan", "SlotTarget", "energy_state_hook",
           "install", "open_demo_inputs", "register_agent_source", "run_day"]
