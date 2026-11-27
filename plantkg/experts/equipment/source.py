"""设备监测专家的场景适配（AgentResultSource，deterministic）。

场景装配经 SCENARIO_HOOK 登记本结果源；它优先从观测层（ObservationBus / 场景 state.sims["sensors"]）取
告警前 15 分钟传感器读数与告警前 10 分钟截止的 60 分钟同期基线（带 Quality、残差、候选与试探状态），
观测层不可用时退回设备遥测。研判前先把读数可信度交给专家核对（D59）。
Nexent 侧的 propose 工具走 `install()` 登记的钩子，两者共用同一个专家对象。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from plantkg.core.contracts import AgentResultKind, ErrorCode, PlantkgError, RunBinding
from plantkg.core.ports import RunStore
from plantkg.knowledge.rules.normalise import entity_keys
from plantkg.knowledge.rules.runtime import matches_combo_advice, matches_combo_rule
from plantkg.world.sensors import ObservationBus
from plantkg.world.sim.faults import EquipmentTelemetry

from .expert import EquipmentExpert

SENSOR_CASE_KIND = "sensor_case"  # #236：B22 读数可疑案例，export_day 按 event_id 合并进对应事件的 payload
SENSOR_SUSPECT_EVENTS = frozenset({"compressor_pressure_drop", "compressor_leak", "air_leak"})  # 设备专家用观测层核对的告警


class EquipmentResultSource:
    """AgentResultSource：payload["event"] → EquipmentExpert.propose → {"proposal": ...}。

    有场景运行绑定时直接用 runner 的观测量；没有绑定时（SCENARIO_HOOK 注册的场景注册表）读
    ObservationBus 最近快照。两种路径共用同一套读数窗口与同期基线逻辑。
    """

    source_kind = "deterministic"

    def __init__(self, expert: EquipmentExpert | None = None, *, plant_id: str = "demo-electronics",
                 site: Any = None, reading_minutes: int = 15, baseline_minutes: int = 60, baseline_gap_minutes: int = 10,
                 registry: Any = None) -> None:
        self.expert = expert or EquipmentExpert()
        self.plant_id = plant_id
        self.site = site
        self.registry = registry
        self.reading_minutes = reading_minutes
        self.baseline_minutes = baseline_minutes
        self.baseline_gap_minutes = baseline_gap_minutes
        self.runner: Any = None  # 可选：直接构造/测试时绑 runner；场景钩子路径不需要
        self.sensor_cases: dict[str, dict] = {}  # event_id → 已写出的 sensor_case（进程内；只增不按分钟刷）

    def bind(self, runner: Any) -> "EquipmentResultSource":
        self.runner = runner
        return self

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if AgentResultKind(kind) != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"设备专家只处理 expert 结果，不处理 {kind}")
        event = dict(payload["event"])
        state = self.state_for(binding, event, payload)
        source_registry = self.registry
        runs = self.registry.ports.get(RunStore) if self.registry is not None else None
        if self.runner is not None and self.runner.registry.hooks_for('knowledge.b30.rules'):
            source_registry, runs = self.runner.registry, self.runner.db.runs
        if source_registry is not None and runs is not None:
            try:
                run = runs.get_run(binding.run_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            else:
                as_of = max(run.as_of, run.forked_at) if run.forked_at else run.as_of
                if self.runner is not None and self.runner.mode == 'trial':
                    as_of = binding.trigger_at  # still bounded by each rule's available_at/effective_from
                apply_published_rule(source_registry, run.version_manifest_id, binding.trigger_at, as_of, state, event.get("entity_id"), event)
        proposal = self.expert.propose(event, state)
        self.record_sensor_case(event, binding, state)
        return {"proposal": proposal.model_dump(mode="json")}

    # ---- #236：读数可疑时落一条 sensor_case，试探验证结束再覆盖同一记录
    def record_sensor_case(self, event: Mapping[str, Any], binding: RunBinding, state: Mapping[str, Any]) -> None:
        """专家研判时调用：观测层有报警关系且读数被判可疑，写一条 b17_records(kind="sensor_case")。
        数值全部取 ObservationBus 快照的真实计算结果；同一触发事件只写一条，不按分钟刷。"""
        runner, event_id = self.runner, event.get("event_id")
        observation = dict(state.get("observation") or {})
        if runner is None or not event_id or not observation or event_id in self.sensor_cases:
            return
        if str(event_id).startswith("preview://"):  # 预演事件只是后台对照，不是真实事件，不落导出记录
            return
        if str(event.get("event_type") or "") not in SENSOR_SUSPECT_EVENTS:
            return
        case = self._suspect_case(observation)
        if case is None:
            return
        record = case | {"event_id": str(event_id), "ticket_id": binding.ticket_id,
                         "nature": observation.get("nature") or "simulated"}
        self.sensor_cases[str(event_id)] = record
        runner.db.store.write_records(runner.run_info.run_id, [(SENSOR_CASE_KIND, str(event_id), binding.trigger_at, record)])

    def record_trial_conclusion(self, runner: Any, at: datetime) -> None:
        """每分钟钩子：试探有结论（sensor_fault / equipment_fault）时覆盖同一条 sensor_case 的结论字段。"""
        trial = dict((runner.state.sims.get("sensors") or {}).get("trial") or {})
        verdict = trial.get("verdict")
        if verdict not in ("sensor_fault", "equipment_fault"):
            return
        pending = [case for case in self.sensor_cases.values() if "conclusion" not in case]
        ticket_id = self._trial_ticket(runner, trial)
        matched = [case for case in pending if ticket_id and case.get("ticket_id") == ticket_id] or \
                  ([pending[0]] if ticket_id is None and len(pending) == 1 else [])
        if len(matched) != 1:
            return
        case = dict(matched[0])
        action = dict(trial.get("action") or {})
        params = dict(action.get("params") or {})
        standby = params.get("to") or action.get("entity_id") or "—"
        verified = (f"试探：启动 {str(standby).rsplit('/', 1)[-1]} {int(trial.get('window_min') or 0)} 分钟"
                    f"（压力读数变化 {float(trial.get('pressure_delta') or 0.0):+.2f} bar、"
                    f"推算值变化 {float(trial.get('virtual_delta') or 0.0):+.2f} bar）")
        category = "sensor" if verdict == "sensor_fault" else "equipment"
        record = case | {
            "conclusion": verdict,
            "verified_by": verified,
            "excluded": [c["id"] for c in case.get("candidates") or () if c.get("category") != category],
            "resolution": "改用虚拟传感器推算值继续运行，开计量检修工单，不停机" if category == "sensor"
                          else "设备侧确认有反应，按泄漏处置（切换备用机、隔离泄漏段）",
            "verified_at": at.isoformat(),  # #238：导出按当日结束判断结论是否当天可见
        }
        self.sensor_cases[case["event_id"]] = record
        runner.db.store.write_records(runner.run_info.run_id, [(SENSOR_CASE_KIND, case["event_id"], at, record)])

    @staticmethod
    def _suspect_case(observation: Mapping[str, Any]) -> dict | None:
        readings = dict(observation.get("readings") or {})
        for relation in observation.get("relations") or ():
            if not relation.get("alarm"):
                continue
            suspects = [sid for sid in relation.get("sensors") or ()
                        if ((readings.get(sid) or {}).get("quality") or {}).get("flag") in ("suspect", "missing")]
            if not suspects:
                continue
            reading = dict(readings.get(suspects[0]) or {})
            return {"sensor": suspects[0], "reading": reading.get("value"), "baseline": relation.get("expected"),
                    "residual_c": relation.get("residual"), "residual_limit_c": relation.get("tolerance"),
                    "quality": (reading.get("quality") or {}).get("flag"),
                    "candidates": [dict(c) for c in observation.get("candidates") or ()]}
        return None

    @staticmethod
    def _trial_ticket(runner: Any, trial: Mapping[str, Any]) -> str | None:
        interval = (trial.get("action") or {}).get("interval")
        for item in runner.state.sims.get("actions") or ():
            action = dict(item.get("action") or {})
            if (action.get("params") or {}).get("trial") and action.get("interval") == interval:
                return (item.get("source") or {}).get("ticket_id")
        return None

    def state_for(self, binding: RunBinding, event: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> dict:
        payload = dict(payload or {})
        ids = dict(payload.get("ids") or {})
        state: dict[str, Any] = {"ticket_id": binding.ticket_id, "trigger_at": binding.trigger_at, "ids": ids,
                                 "knowledge": list(payload.get("knowledge") or ()), "entity_id": event.get("entity_id")}
        entity_id = event.get("entity_id")
        code = self._code(entity_id, ids)
        if not code:
            return state
        end = binding.trigger_at
        base_end = end - timedelta(minutes=self.baseline_gap_minutes)
        observed = self._from_sensors(code, end, base_end)
        if observed is not None:
            state |= observed
        else:
            telemetry = self._telemetry()
            if telemetry is not None and self.runner is not None:
                readings = telemetry.window(self.runner.state, code, end, self.reading_minutes)
                baseline_samples = telemetry.window(self.runner.state, code, base_end, self.baseline_minutes)
                state |= {"readings": telemetry.reading_state(readings, window=self._window(readings)),
                          "baseline": telemetry.summarize(
                              baseline_samples,
                              window=f"{self._clock(base_end - timedelta(minutes=self.baseline_minutes))}～{self._clock(base_end)} 同期基线")}
        if "readings" in state:
            standby = self._standby(ids, code)
            state |= {"entity_code": code, "standby": standby}
            running = ((state.get("observation") or {}).get("running") or {}).get(standby)
            if running is not None:
                state["standby_running"] = running
            rated = self._rated_kw(entity_id)
            if rated:
                state["rated_kw"] = rated
            ref = (state.get("observation") or {}).get("config_ref")
            if ref:
                state["evidence_refs"] = tuple(state.get("evidence_refs", ())) + (ref,)
        injection = self._injection(code)
        if injection:
            state["injection"] = injection
            state["evidence_refs"] = tuple(state.get("evidence_refs", ())) + (f"injection:{injection['injection_id']}",) + (
                ("knowledge:compressor_leak_rule",) if "compressor_leak_rule" in state["knowledge"] else ())
        return state

    # ---- 观测层优先
    def _from_sensors(self, code: str, end: datetime, base_end: datetime) -> dict | None:
        box = self._observation()
        if not box:
            return None
        history = box.get("history") or {}
        snapshot = box.get("snapshot")
        readings = self._samples(code, end, self.reading_minutes, history)
        baseline_samples = self._samples(code, base_end, self.baseline_minutes, history)
        if len(readings) < 2 or len(baseline_samples) < 2:
            return None
        quality = {sid: history[sid][-1]["quality"] for sid in (f"{code}.I", f"{code}.OIL", "AIR-HDR.P") if history.get(sid)}
        return {"readings": {"samples": readings, "window": self._window(readings), "sampling_s": 60, "nature": "simulated",
                             "quality": quality},
                "baseline": self._summarize(baseline_samples, f"{self._clock(base_end - timedelta(minutes=self.baseline_minutes))}～{self._clock(base_end)} 同期基线"),
                "observation": snapshot}

    def _observation(self) -> dict | None:
        if self.runner is not None:
            box = self.runner.state.sims.get("sensors") or {}
            if box.get("latest"):
                return {"snapshot": box["latest"], "history": box.get("history") or {}}
        return ObservationBus.latest(self.plant_id)

    @staticmethod
    def _samples(code: str, end: datetime, minutes: int, history: Mapping[str, list]) -> list[dict]:
        start = end - timedelta(minutes=minutes)
        pressure = {item["at"]: item["value"] for item in history.get("AIR-HDR.P", [])}
        oil = {item["at"]: item["value"] for item in history.get(f"{code}.OIL", [])}
        out = []
        for item in history.get(f"{code}.I", []):
            at = item["at"]
            if not (start <= datetime.fromisoformat(at) < end):
                continue
            current, pressure_value = item["value"], pressure.get(at)
            if current is None or pressure_value is None:
                continue
            out.append({"at": at, "current_a": float(current), "oil_c": float(oil.get(at) or 0.0),
                        "pressure_bar": float(pressure_value), "on": float(current) > 2.0})
        return out

    @staticmethod
    def _summarize(samples: list[dict], window: str) -> dict:
        from plantkg.world.sim.faults.metropt3 import Sample, summarize

        stats = summarize([Sample(at=datetime.fromisoformat(s["at"]), current_a=s["current_a"], oil_c=s["oil_c"],
                                  pressure_bar=s["pressure_bar"], on=s["on"]) for s in samples], window=window, sampling_s=60.0)
        return stats.to_dict() | {"nature": "simulated"}

    def _telemetry(self) -> EquipmentTelemetry | None:
        if self.runner is None:
            return None
        return next((s for s in self.runner.world.simulators if isinstance(s, EquipmentTelemetry)), None)

    def _code(self, entity_id: str | None, ids: Mapping[str, str]) -> str | None:
        if not entity_id:
            return None
        equipment = self._equipment(entity_id)
        if equipment is not None:
            return equipment.code
        return str(entity_id).rsplit("/", 1)[-1]

    def _equipment(self, entity_id: str):
        if self.runner is not None:
            return self.runner.world.index.by_eid.get(entity_id)
        if self.site is not None:
            return next((e for e in self.site.equipment if e.entity_id == entity_id), None)
        return None

    def _rated_kw(self, entity_id: str | None) -> float | None:
        equipment = self._equipment(entity_id) if entity_id else None
        return equipment.rated_kw if equipment is not None else None

    def _injection(self, code: str) -> dict | None:
        if self.runner is None:
            return None
        found = [i for i in self.runner.state.sims.get("injections", []) if i.get("entity") == code]
        return found[-1] if found else None

    def _standby(self, ids: Mapping[str, str], code: str) -> str | None:
        return next((key for key in sorted(ids) if key.startswith("AC-") and key != code), None)

    def _clock(self, at) -> str:
        if self.runner is not None:
            tz = ZoneInfo(self.runner.world.site.plant_tz)
        elif self.site is not None:
            tz = ZoneInfo(self.site.plant_tz)
        else:
            tz = ZoneInfo("Asia/Shanghai")
        return at.astimezone(tz).strftime("%H:%M")

    def _window(self, samples: list[dict]) -> str:
        if not samples:
            return ""
        return f"{self._clock(datetime.fromisoformat(samples[0]['at']))}～{self._clock(datetime.fromisoformat(samples[-1]['at']))}"


def apply_published_rule(registry, manifest_id, at, as_of, state, event_entity=None, event=None):
    if not manifest_id:
        return
    providers = registry.hooks_for("knowledge.b30.rules")
    if not providers:
        return
    # 规则主体与运行时实体都归一到图谱编号后比较（#143：AIR/AIR-HDR 指同一对象）
    subjects = {key for entity in (*((state.get("ids") or {}).values()), state.get("entity_id"), event_entity)
                for key in entity_keys(entity)}
    advisories = []
    executable_combo_found = False
    for rule in providers[-1](manifest_id, at, as_of):
        rule_subjects = {key for subject in rule.subject_ids for key in entity_keys(subject)}
        if (rule.kind == "compressor_pressure_drop" and rule.unit == "bar/h" and rule.value is not None
                and subjects & rule_subjects):
            state["published_leak_rule"] = rule.model_dump(mode="json")
        if rule.kind == "maintenance_advisory" and event_entity and set(entity_keys(event_entity)) & rule_subjects:
            advisories.append(rule.model_dump(mode="json"))
        if event and matches_combo_rule(rule, event.get('event_type'), event_entity, event.get('summary')):
            state['published_combo_rule'] = rule.model_dump(mode='json')
            executable_combo_found = True
        elif (event and not executable_combo_found
              and matches_combo_advice(rule, event.get('event_type'), event_entity, event.get('summary'))):
            state['published_combo_rule'] = rule.model_dump(mode='json')
    if advisories:
        state["published_advisories"] = advisories
