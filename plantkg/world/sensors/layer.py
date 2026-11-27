"""观测层（B22 / D59）：真实状态 → 传感器读数 → Quality → 残差 → 候选假设 / 虚拟传感器。

`SensorLayer` 是 B17 场景世界的 Simulator，必须排在仿真器列表最后：它读本分钟的真实状态（功率、压力、
油温、区域温度），按 `sensors.yaml` 变成带 Quality 的读数，再算三条一致性关系的残差、按故障特征矩阵
列出候选假设（只列不下结论），并给出 2 个虚拟传感器推算值。真实状态不受影响。

传感器故障由 `SensorFaultInjector` 注入，与设备故障分开；试探动作由 `TrialActionSim` 在世界上产生反应。
读数是 `simulated`；传感器布置与关系是 `configured`。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from plantkg.core.contracts import SimAction
from plantkg.world.sim.base import WorldState
from plantkg.world.site.model import SiteModel

from .bus import ObservationBus
from .config import SensorConfig, SensorDef, load_sensor_config

_VOLTAGE_V, _POWER_FACTOR = 380.0, 0.9


def _value(readings: Mapping[str, dict], sid: str) -> float | None:
    reading = readings.get(sid)
    return None if reading is None or reading.get("value") is None else float(reading["value"])


def _flag(readings: Mapping[str, dict], sid: str) -> str:
    reading = readings.get(sid)
    return reading["quality"]["flag"] if reading else "missing"


class SensorLayer:
    """B17 Simulator：每分钟最后运行，产出观测快照并写入进程内总线。"""

    name = "sensors"
    stage = "observe"  # B17 按阶段排序仿真器：观测层自动排在全部物理仿真之后

    def __init__(self, site: SiteModel, air_entity_id: str, config: SensorConfig | None = None,
                 config_path: str | None = None) -> None:
        self.site = site
        self.plant_id = site.plant_id
        self.air_entity_id = air_entity_id
        self.config = config or load_sensor_config(config_path)
        self.by_code = {e.code: e for e in site.equipment}
        self.zone_ids = {z: f"entity://{site.plant_id}/zone/{z}" for z in site.zones}

    def init(self, state: WorldState) -> None:
        state.sims.setdefault("sensors", {"latest": None, "history": {}, "trial": None, "seq": 0})

    def step(self, state: WorldState, sim_actions: Sequence[SimAction], dt: timedelta) -> WorldState:
        box = state.sims.setdefault("sensors", {"latest": None, "history": {}, "trial": None, "seq": 0})
        active = list(state.sims.get("sensor_faults", {}).get("active", []))
        raw = {sid: self._raw(state, sensor) for sid, sensor in self.config.sensors.items()}
        readings = {sid: self._reading(state, sensor, raw[sid], active) for sid, sensor in self.config.sensors.items()}
        relations = [self._relation(rel, readings, state.at) for rel in self.config.relations]
        self._mark_quality(readings, relations)
        virtuals = {v.id: self._virtual(v, readings, state.at) for v in self.config.virtuals}
        candidates = self._candidates(relations, readings)
        trial = self._trial(state, box, readings, virtuals)
        if trial and trial.get("verdict") == "sensor_fault":
            candidates = [c for c in candidates if c["category"] == "sensor"]
        elif trial and trial.get("verdict") == "equipment_fault":
            candidates = [c for c in candidates if c["category"] == "equipment"]
        snapshot = {"at": state.at.isoformat(), "seq": int(box.get("seq", 0)), "readings": readings, "relations": relations,
                    "virtuals": virtuals, "candidates": candidates, "trial": trial,
                    "alarms": [rel["id"] for rel in relations if rel["alarm"]],
                    "running": {e.code: bool(state.devices.get(e.entity_id, {}).get("running")) for e in self.by_code.values()
                                if e.type == "air_compressor"},
                    "nature": "simulated", "config_ref": self.config.evidence_ref(), "site": self.plant_id,
                    "run_id": box.get("run_id")}
        box["latest"], box["seq"] = snapshot, int(box.get("seq", 0)) + 1
        history = box.setdefault("history", {})
        for sid, reading in readings.items():
            history.setdefault(sid, []).append({"at": reading["at"], "value": reading["value"], "quality": reading["quality"]["flag"]})
            history[sid] = history[sid][-self.config.history_minutes:]
        ObservationBus.publish(self.plant_id, snapshot, history, run_id=box.get("run_id"))
        return state

    # ------------------------------------------------------------------ 真实值 → 读数
    def _raw(self, state: WorldState, sensor: SensorDef) -> float | None:
        source = sensor.source
        if source == "air_header":
            base = float(state.devices.get(self.air_entity_id, {}).get("pressure_bar", 7.0))
            return base + sum(float(e.get("bar", 0.0)) for e in state.effect("pressure_boost"))
        if source == "power_to_current":
            device = self.by_code.get(sensor.entity)
            if device is None:
                return None
            power = float(state.power_kw.get(device.code, 0.0))
            return power * 1000 / (math.sqrt(3) * _VOLTAGE_V * _POWER_FACTOR)
        if source == "device_field":
            device = self.by_code.get(sensor.entity)
            if device is not None:
                entity_id = device.entity_id
            else:  # 配电节点（如 TX1）：状态帧按 entity://{厂区}/{type}/{节点} 记，与图谱一致
                node = self.site.nodes.get(sensor.entity)
                if node is None:
                    return None
                entity_id = f"entity://{self.plant_id}/{node.type}/{node.id}"
            return state.devices.get(entity_id, {}).get(sensor.field) if sensor.field else None
        if source == "total_power":
            return sum(float(v) for v in state.power_kw.values())
        if source == "members_power":
            return sum(float(state.power_kw.get(member, 0.0)) for member in sensor.members)
        if source == "zone_temperature":
            zone = sensor.entity.split(":", 1)[-1]
            zid = self.zone_ids.get(zone)
            return state.devices.get(zid, {}).get("temperature") if zid else None
        raise ValueError(f"传感器 {sensor.id} 的 source {source} 不可用")

    def _reading(self, state: WorldState, sensor: SensorDef, raw: float | None, active: list[dict]) -> dict:
        value, flag, note = raw, "ok", None
        fault = next((f for f in active if f.get("sensor_id") == sensor.id), None)
        if fault is not None:
            kind = fault["kind"]
            params = fault.get("params") or {}
            elapsed = max(0.0, (state.at - datetime.fromisoformat(fault["since"])).total_seconds() / 60)
            if kind == "offline":
                value, flag, note = None, "missing", "传感器掉线"
            elif value is not None and kind == "stuck":
                if "frozen" not in fault:
                    fault["frozen"] = float(params.get("value", value))
                value, flag, note = float(fault["frozen"]), "ok", None
            elif value is not None and kind == "drift":
                value = value + float(params.get("rate_per_min", 0.0)) * elapsed
            elif value is not None and kind == "bias":
                value = value + float(params.get("offset", 0.0))
            elif value is not None and kind == "spike":
                period = max(1, int(params.get("period_min", 5)))
                if int(elapsed) % period == 0:
                    value, flag, note = value + float(params.get("magnitude", 1.0)), "suspect", "读数跳变"
        if value is not None:
            if sensor.value_range and not sensor.value_range[0] <= value <= sensor.value_range[1]:
                flag, note = "suspect", f"超出量程 {sensor.value_range[0]}～{sensor.value_range[1]}"
            value = round(float(value), sensor.decimals)
        return {"sensor_id": sensor.id, "entity": sensor.entity, "metric": sensor.metric, "unit": sensor.unit,
                "value": value, "quality": {"flag": flag, "note": note}, "at": state.at.isoformat(),
                "nature": "simulated", "source": f"sensor:{sensor.id}"}

    # ------------------------------------------------------------------ 残差与候选
    def _relation(self, rel, readings: Mapping[str, dict], at: datetime) -> dict:
        missing = [sid for sid in rel.sensors if _value(readings, sid) is None]
        base = {"id": rel.id, "label": rel.label, "kind": rel.kind, "nodes": list(rel.nodes), "sensors": list(rel.sensors),
                "currents": list(rel.currents), "pressure": rel.pressure, "power": rel.power, "temperature": rel.temperature,
                "unit": rel.unit, "tolerance": rel.tolerance, "at": at.isoformat()}
        if missing:
            return base | {"value": None, "expected": None, "residual": None, "alarm": True,
                           "detail": f"读数缺失：{','.join(missing)}"}
        if rel.kind == "sum_equals":
            total = _value(readings, rel.total)
            expected = sum(_value(readings, sid) or 0.0 for sid in rel.parts)
            residual = total - expected
            detail = f"总表 {total:.1f} kW，分表合计 {expected:.1f} kW，残差 {residual:+.1f} kW"
        elif rel.kind == "current_pressure":
            currents = [_value(readings, sid) or 0.0 for sid in rel.currents]
            mean = sum(currents) / max(1, len(currents))
            p0, normal, k = rel.params["p0_bar"], max(1.0, rel.params["normal_current_a"]), rel.params["k_bar"]
            expected = p0 - k * max(0.0, mean / normal - 1.0)
            measured = _value(readings, rel.pressure)
            residual = measured - expected
            detail = f"电流均值 {mean:.1f} A，压力 {measured:.2f} bar，按电流推算 {expected:.2f} bar，残差 {residual:+.2f} bar"
        elif rel.kind == "hvac_power_temperature":
            power = _value(readings, rel.power) or 0.0
            measured = _value(readings, rel.temperature)
            expected = rel.params["setpoint_c"] + rel.params["k_c_per_unit"] * max(0.0, 1.0 - power / max(1.0, rel.params["rated_kw"]))
            residual = measured - expected
            detail = f"暖通 {power:.0f} kW，车间 {measured:.2f} ℃，预期 {expected:.2f} ℃，残差 {residual:+.2f} ℃"
        else:
            raise ValueError(f"未知一致性关系 kind={rel.kind}")
        return base | {"value": round(measured if rel.kind != "sum_equals" else total, 3), "expected": round(expected, 3),
                       "residual": round(residual, 3), "alarm": abs(residual) > rel.tolerance, "detail": detail}

    @staticmethod
    def _mark_quality(readings: dict[str, dict], relations: list[dict]) -> None:
        """只给“最可能是坏点”的读数标 suspect：电流与压力不一致时，看电流彼此是否一致，
        一致则压力传感器可疑，不一致则离群的电流传感器可疑；其他关系交给候选矩阵，不急着标坏。"""
        suspect: dict[str, list[str]] = {}
        for rel in relations:
            if not rel["alarm"]:
                continue
            if rel["kind"] == "current_pressure" and rel.get("currents"):
                values = [(sid, _value(readings, sid)) for sid in rel["currents"]]
                values = [(sid, v) for sid, v in values if v is not None]
                median = sorted(v for _, v in values)[len(values) // 2] if values else 0.0
                outliers = [sid for sid, v in values if abs(v - median) > max(0.5, 0.15 * abs(median))]
                for sid in [rel["pressure"], *outliers]:
                    if sid:
                        suspect.setdefault(sid, []).append(rel["id"])
            elif rel["kind"] == "hvac_power_temperature":
                for sid in (rel["temperature"], rel["power"]):
                    if sid:
                        suspect.setdefault(sid, []).append(rel["id"])
        for sid, relation_ids in suspect.items():
            reading = readings.get(sid)
            if reading and reading["quality"]["flag"] == "ok":
                reading["quality"] = {"flag": "suspect", "note": f"残差 {','.join(relation_ids)} 报警"}

    def _candidates(self, relations: list[dict], readings: Mapping[str, dict]) -> list[dict]:
        out: dict[str, dict] = {}
        for rel in relations:
            if not rel["alarm"]:
                continue
            for template in self.config.fault_matrix.get(rel["id"], ()):
                item = out.setdefault(template.id, {"id": template.id, "label": template.label, "category": template.category,
                                                    "sensors": list(template.sensors), "evidence": []})
                item["evidence"].append(f"relation:{rel['id']}")
        for sid, reading in readings.items():
            flag = reading["quality"]["flag"]
            covered = any(sid in c["sensors"] for c in out.values())
            if flag == "missing":
                base = self.config.sensor_fault_candidates["offline"]
                out[f"{base.id}:{sid}"] = {"id": f"{base.id}:{sid}", "label": f"{base.label}（{sid}）", "category": "sensor",
                                           "sensors": [sid], "evidence": [f"quality:{sid}"]}
            elif flag == "suspect" and not covered:
                out[f"sensor_anomaly:{sid}"] = {"id": f"sensor_anomaly:{sid}", "label": f"传感器读数异常（{sid}）",
                                                 "category": "sensor", "sensors": [sid], "evidence": [f"quality:{sid}"]}
        return sorted(out.values(), key=lambda c: (c["category"] != "sensor", c["id"]))

    def _virtual(self, virtual, readings: Mapping[str, dict], at: datetime) -> dict:
        values = [_value(readings, sid) for sid in virtual.inputs]
        quality = {"flag": "estimated", "note": f"由 {','.join(virtual.inputs)} 推算"}
        if any(value is None for value in values):
            return {"virtual_id": virtual.id, "label": virtual.label, "entity": virtual.entity, "metric": virtual.metric,
                    "unit": virtual.unit, "value": None, "quality": {"flag": "missing", "note": "输入读数缺失"},
                    "inputs": list(virtual.inputs), "basis": virtual.kind, "at": at.isoformat()}
        if virtual.kind == "current_to_pressure":
            mean = sum(values) / len(values)
            normal = max(1.0, virtual.params.get("normal_current_a", 40.0))
            # 供给侧估算：启动备用机或加负荷使电流上升时，供气能力上升，估算压力应上升；
            # 泄漏/需求侧的“电流升、压力降”由残差关系 air_current_pressure 另行判断，二者方向不同。
            value = virtual.params.get("p0_bar", 7.0) + virtual.params.get("k_bar", 0.8) * max(0.0, mean / normal - 1.0)
        elif virtual.kind == "sum":
            value = sum(values)
        else:
            raise ValueError(f"未知虚拟传感器 kind={virtual.kind}")
        return {"virtual_id": virtual.id, "label": virtual.label, "entity": virtual.entity, "metric": virtual.metric,
                "unit": virtual.unit, "value": round(value, 3), "quality": quality, "inputs": list(virtual.inputs),
                "basis": virtual.kind, "at": at.isoformat()}

    # ------------------------------------------------------------------ 试探与候选收敛
    def _trial(self, state: WorldState, box: dict, readings: Mapping[str, dict], virtuals: Mapping[str, dict]) -> dict | None:
        trial_actions = []
        for item in state.sims.get("actions", []):
            action = SimAction.model_validate(item["action"])
            if action.params.get("trial") and action.command in ("start", "switch_standby"):
                trial_actions.append(action)
        current = next((a for a in trial_actions if a.interval.start <= state.at < a.interval.end), None)
        previous = box.get("trial")
        if current is None:
            if previous and not previous.get("verdict") and state.at >= datetime.fromisoformat(previous["window_end"]):
                previous["verdict"] = "unfinished"
                previous["detail"] = "观察窗口结束，读数变化不足以区分候选"
            return previous
        if previous is None or previous.get("action", {}).get("interval") != current.model_dump(mode="json")["interval"]:
            last = box.get("latest")
            before_pressure = _value(last["readings"], "AIR-HDR.P") if last else _value(readings, "AIR-HDR.P")
            before_virtual = (last.get("virtuals") or {}).get("VS-AIR-P", {}).get("value") if last else virtuals.get("VS-AIR-P", {}).get("value")
            previous = {"action": current.model_dump(mode="json"), "started_at": current.interval.start.isoformat(),
                        "window_end": current.interval.end.isoformat(),
                        "window_min": int((current.interval.end - current.interval.start).total_seconds() // 60),
                        "hypotheses": list(current.params.get("hypotheses", ())),
                        "before": {"at": last["at"] if last else state.at.isoformat(), "pressure": before_pressure,
                                   "virtual_pressure": before_virtual},
                        "after": None, "pressure_delta": None, "virtual_delta": None, "verdict": None,
                        "readings_at": []}
            box["trial"] = previous
        pressure = _value(readings, "AIR-HDR.P")
        virtual_pressure = (virtuals.get("VS-AIR-P") or {}).get("value")
        previous["after"] = {"at": state.at.isoformat(), "pressure": pressure, "virtual_pressure": virtual_pressure}
        before = previous["before"]
        if pressure is not None and before.get("pressure") is not None:
            previous["pressure_delta"] = round(pressure - float(before["pressure"]), 3)
        if virtual_pressure is not None and before.get("virtual_pressure") is not None:
            previous["virtual_delta"] = round(virtual_pressure - float(before["virtual_pressure"]), 3)
        previous["readings_at"] = (previous["readings_at"] + [
            {"at": state.at.isoformat(), "pressure": pressure, "virtual_pressure": virtual_pressure}])[-8:]
        eps_p = self.config.trial.get("pressure_eps_bar", 0.03)
        eps_v = self.config.trial.get("virtual_eps_bar", 0.03)
        if previous.get("verdict") is None and state.at > datetime.fromisoformat(previous["started_at"]):
            p_delta, v_delta = previous.get("pressure_delta"), previous.get("virtual_delta")
            if p_delta is not None and v_delta is not None:
                if abs(p_delta) < eps_p and abs(v_delta) >= eps_v:
                    previous["verdict"] = "sensor_fault"
                    previous["detail"] = f"压力读数变化 {p_delta:+.2f} bar（小于 {eps_p}），推算值变化 {v_delta:+.2f} bar"
                elif abs(p_delta) >= eps_p:
                    previous["verdict"] = "equipment_fault"
                    previous["detail"] = f"压力读数变化 {p_delta:+.2f} bar，设备侧有反应"
        return previous


def query_snapshot(snapshot: dict, history: Mapping[str, list], *, entity: str | None = None, sensor_id: str | None = None,
                   minutes: int = 5, since: str | None = None) -> dict:
    """把观测快照裁成 sensor_query 的返回：读数、Quality、残差、候选、虚拟传感器、试探与新时间戳历史。"""
    readings = snapshot.get("readings", {})
    if sensor_id:
        chosen = {sensor_id: readings[sensor_id]} if sensor_id in readings else {}
    elif entity:
        chosen = {sid: r for sid, r in readings.items() if r.get("entity") == entity or sid.startswith(entity + ".")}
    else:
        chosen = dict(readings)
    since_at = datetime.fromisoformat(since) if since else None
    oldest = datetime.fromisoformat(snapshot["at"]) - timedelta(minutes=minutes)
    use_history = bool(sensor_id or entity)
    hist = {}
    if use_history:
        for sid in chosen:
            items = [x for x in history.get(sid, [])
                     if datetime.fromisoformat(x["at"]) >= oldest and (since_at is None or datetime.fromisoformat(x["at"]) > since_at)]
            hist[sid] = items[-12:]
    ids = set(chosen)
    relations = [r for r in snapshot.get("relations", []) if not ids or ids & set(r.get("sensors", ()))]
    virtuals = [v for v in snapshot.get("virtuals", {}).values()
                if not ids or ids & set(v.get("inputs", ())) or (entity and v.get("entity") == entity)]
    candidates = [c for c in snapshot.get("candidates", []) if not ids or ids & set(c.get("sensors", ()))]
    data = {"at": snapshot.get("at"), "nature": snapshot.get("nature"), "config_ref": snapshot.get("config_ref"),
            "run_id": snapshot.get("run_id"),
            "readings": list(chosen.values()), "relations": relations, "virtuals": virtuals, "candidates": candidates,
            "trial": snapshot.get("trial"), "alarms": snapshot.get("alarms", [])}
    if use_history:
        data["history"] = hist
    else:
        data["history_note"] = "按 sensor_id 或 entity 查时返回带时间戳的读数历史"
    return data
