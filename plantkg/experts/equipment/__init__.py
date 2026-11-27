"""设备监测专家（B22）。领域包 install：MCP propose 钩子、sensor_query、场景专家与仿真器。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from plantkg.core.contracts import Context, DayMetric, ErrorCode, ExpertProposal, PlantkgError, RunBinding
from plantkg.core.ports import AgentResultSource, RunStore
from plantkg.core.registry import Registry
from plantkg.world.scenario.runner import SCENARIO_HOOK, SCENARIO_SENSOR_TICK
from plantkg.world.scenario.sims import SIM_HOOK
from plantkg.world.sensors import SensorFault, SensorFaultInjector, SensorLayer, TrialActionSim
from plantkg.world.sensors import install as install_sensors
from plantkg.world.sim.faults import EquipmentTelemetry, FaultInjector
from plantkg.world.site.model import SiteModel

from .expert import EquipmentExpert
from .source import EquipmentResultSource, apply_published_rule

READINGS_HOOK = "equipment.readings"  # 实时遥测提供者：fn(binding) -> state dict（B26/B46 接入时登记）
CONFIG_METRICS_HOOK = "export.config_metrics"  # 场景注册表钩子：fn(site, policy) -> list[DayMetric]
DEFAULT_SITE = Path(__file__).resolve().parents[3] / "configs" / "sites" / "factory" / "site.yaml"
DEFAULT_EQUIPMENT_CONFIG = DEFAULT_SITE.parent / "equipment.yaml"
_EXPERT = EquipmentExpert()


def _handler(registry: Registry):
    def propose(context: Context, binding: RunBinding, focus: str | None = None, **_params: Any) -> ExpertProposal:
        """`equipment_propose` 的业务函数（B23 统一包装后落库）。有遥测提供者时用它，否则按事件单给保守结论。"""
        state: dict[str, Any] = {}
        providers = registry.hooks_for(READINGS_HOOK)
        if providers:
            state = dict(providers[-1](binding) or {})
        event = state.pop("event", None) or {"event_type": "equipment_query", "entity_id": state.get("entity_id"),
                                             "summary": focus or "", "payload": {"focus": focus or ""}}
        state |= {"ticket_id": binding.ticket_id, "trigger_at": binding.trigger_at}
        as_of = getattr(context, "as_of", binding.trigger_at)
        if context and context.run_id and RunStore in registry.ports:
            try:
                run = registry.resolve(RunStore).get_run(context.run_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            else:
                if run.version_manifest_id != context.version_manifest_id:
                    raise PlantkgError(ErrorCode.PRECONDITION, "设备运行与知识版本不一致")
                if run.forked_at:
                    as_of = max(as_of, run.forked_at)
        apply_published_rule(registry, getattr(context, "version_manifest_id", None), binding.trigger_at, as_of, state,
                             event.get("entity_id"), event)
        return _EXPERT.propose(event, state)

    propose.__b22_equipment__ = True  # type: ignore[attr-defined]
    return propose


# ---- SIM_HOOK：设备故障、空压遥测、试探反应、传感器故障与观测层，按场景装配注册
def _faults(index: Any) -> FaultInjector:
    return FaultInjector(index.site, ())


def _telemetry(index: Any) -> EquipmentTelemetry:
    return EquipmentTelemetry(index.site, index.air_eid)


def _trial(index: Any) -> TrialActionSim:
    return TrialActionSim()


def _sensor_faults(index: Any) -> SensorFaultInjector:
    return SensorFaultInjector(())


def _sensor_layer_factory(scenario_registry: Registry):
    """观测层装配：把活动清单里已发布的传感器案例追加到故障特征矩阵（D59）。"""
    from plantkg.world.sensors import apply_sensor_overrides, load_sensor_config

    def factory(index: Any) -> SensorLayer:
        overrides = []
        for hook in scenario_registry.hooks_for("sensors.config_overrides"):
            try:
                overrides.extend(hook() or ())
            except Exception:  # noqa: BLE001 — 案例只是参考知识，读不到时按原配置运行
                continue
        if overrides:
            return SensorLayer(index.site, index.air_eid, config=apply_sensor_overrides(load_sensor_config(), overrides))
        return SensorLayer(index.site, index.air_eid)

    factory.__b22_sensors__ = True  # type: ignore[attr-defined]
    return factory


def _config_metrics(site: Any, policy: Any) -> list[DayMetric]:
    """空压限值进 day.json 的 config 组（#238 方案 1）：数值从 site 同目录的 equipment.yaml 读，读不到的项不输出。"""
    config_path = Path(site.config_path).parent / "equipment.yaml" if getattr(site, "config_path", None) else DEFAULT_EQUIPMENT_CONFIG
    if not config_path.is_file():
        return []
    air = (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("compressed_air") or {}
    limits = (("compressed_air_floor_mpa", "floor_mpa", "MPa"), ("compressed_air_leak_threshold", "leak_threshold_mpa_per_min", "MPa/min"))
    return [DayMetric(metric=name, group="config", value=float(air[key]), unit=unit, nature="configured")
            for name, key, unit in limits
            if isinstance(air.get(key), (int, float)) and not isinstance(air.get(key), bool)]


def install(registry: Registry, settings: Any = None) -> EquipmentExpert:
    """登记 MCP 工具钩子与场景装配钩子。

    - `expert_propose:equipment`：B23 包装成 `plantkg_equipment_propose`；
    - `sensor_query` 正式实现：观测读数、Quality、残差/候选、虚拟传感器；
    - `SCENARIO_HOOK`：把真实 `EquipmentResultSource` 登记进场景注册表，并登记 SIM_HOOK 仿真器；
    - `SIM_HOOK` 仿真器：故障注入、空压遥测、试探反应、传感器故障、观测层（stage=observe）。
    幂等：同一注册表重复调用只登记一次。
    """
    hooks = registry.hooks_for("expert_propose:equipment")
    if not any(getattr(h, "__b22_equipment__", False) for h in hooks):
        registry.register_hook("expert_propose:equipment", _handler(registry))
    install_sensors(registry, settings)
    scenario_hooks = registry.hooks_for(SCENARIO_HOOK)
    if not any(getattr(h, "__b22_scenario__", False) for h in scenario_hooks):
        plant_id = getattr(settings, "plant_id", "demo-electronics")
        site = SiteModel.load(DEFAULT_SITE) if DEFAULT_SITE.is_file() else None

        def scenario_plugin(scenario_registry: Registry) -> None:
            source = EquipmentResultSource(_EXPERT, plant_id=plant_id, site=site, registry=registry)
            scenario_registry.register_mode(AgentResultSource, "deterministic", source, key="equipment")
            for factory in (_faults, _telemetry, _trial, _sensor_faults, _sensor_layer_factory(scenario_registry)):
                scenario_registry.register_hook(SIM_HOOK, factory)

            def on_started(runner: Any) -> None:
                """运行开始时绑定世界（#236 写 sensor_case 用），并按剧本替换传感器故障计划。"""
                source.bind(runner)
                plans = getattr(runner.world.scenario, "sensor_faults", ())
                if plans:
                    faults = [SensorFault(p.sensor_id, p.kind, runner.world.scenario.instant(p.at),
                                          runner.world.scenario.instant(p.until) if p.until else None, dict(p.params))
                              for p in plans]
                    runner.world.simulators = [SensorFaultInjector(faults) if getattr(s, "name", "") == "sensor_faults" else s
                                               for s in runner.world.simulators]

            scenario_registry.register_hook("scenario.started", on_started)
            # 观测层结论回写用独立钩子：不能占用 scenario.minute_layer，B47 靠它判断“储能由分钟层接管”
            scenario_registry.register_hook(SCENARIO_SENSOR_TICK,
                                            lambda runner, at: source.record_trial_conclusion(runner, at))
            scenario_registry.register_hook(CONFIG_METRICS_HOOK, _config_metrics)

        scenario_plugin.__b22_scenario__ = True  # type: ignore[attr-defined]
        registry.register_hook(SCENARIO_HOOK, scenario_plugin)
    return _EXPERT


__all__ = ["READINGS_HOOK", "EquipmentExpert", "EquipmentResultSource", "install"]
