"""设备监测专家（B22）：空压泄漏、电机过载与计量异常的确定性研判。

`EquipmentExpert.propose(event, state) -> ExpertProposal` 是业务入口：输入事件单（DayEvent JSON）与
本领域的读数/基线，输出带动作、代价、置信度与依据的提案草稿（proposal_id 为空，由 B23 或 B17 补）。
研判用阈值 + 残差：

- 泄漏：压力斜率、运转占比差、电流均值比，都要与**同时段**基线比（白天不能拿夜间基线判）；
  知识里有 `compressor_leak_rule` 时把依据挂到提案上，阈值默认值待 B30 抽出规则后替换。
- 计量异常（D05 的 24 条）：判为读数与设备运行状态矛盾、设备本体正常，只开计量检修工单，不切换设备。
- 组合异常（`unknown_combo`）：无 `combo_rule` 时低置信诊断转人工；有纠错规则时按知识给设备复位动作。
  电压暂降（`power_quality_dip`）只给设备侧诊断、不下动作，供第 3 层呈现与能源专家的分歧。
- 噪声/未复现的告警：不给动作，低置信度转人工。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from plantkg.core.contracts import ExpertProposal, Interval, ProposedAction, ProposalCost
from plantkg.world.sim.faults.metropt3 import Sample, plant_rated_current_a, summarize

DEFAULT_LEAK_THRESHOLDS = {
    "pressure_drop_bar_per_h": -0.3,  # 压力斜率低于此值判为下降（MetroPT-3 泄漏约 −0.6 bar/h，正常约 0）
    "running_ratio_delta": 0.2,  # 运转占比比同时段基线高 20 个百分点
    "current_ratio": 1.15,  # 电流均值比同时段基线高 15%
    "oil_residual_c": 3.0,  # 油温高于基线 3 ℃ 作为辅助证据
}
METERING_TYPES = {"metering_anomaly", "metering_fault"}
METERING_LABELS = {"reversed_ct_polarity": "CT 极性反接", "phase_reference_misalignment": "相位基准错位",
                   "phase_reference_misalignment_multi_phase": "多相相位基准错位",
                   "phase_reference_misalignment_all_phases": "全相位相位基准错位",
                   "ct_ratio_misconfiguration": "互感器变比配错", "tag_misconfiguration": "标签配错"}


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def _act(entity_id: str, command: str, start: datetime, minutes: int, readiness: str = "ready", depends_on: tuple[str, ...] = (),
         **params: Any) -> ProposedAction:
    return ProposedAction(entity_id=entity_id, command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)), readiness=readiness,
                          depends_on=tuple(depends_on))


def _window_stats(samples: list[Mapping[str, Any]]) -> dict:
    if len(samples) < 2:
        return {"samples": len(samples), "running_ratio": 0.0, "current_mean_a": 0.0, "oil_mean_c": 0.0, "oil_start_c": 0.0,
                "oil_end_c": 0.0, "pressure_start_bar": 0.0, "pressure_end_bar": 0.0, "pressure_drop_bar_per_h": 0.0}
    return summarize([Sample(at=_instant(s["at"]), current_a=float(s["current_a"]), oil_c=float(s["oil_c"]),
                             pressure_bar=float(s["pressure_bar"]), on=bool(s["on"])) for s in samples]).to_dict()


def _published_pressure(rule: Mapping[str, Any], samples: list[Mapping[str, Any]]) -> tuple[bool, float] | None:
    """按原文指定窗口判压降；窗口或供气状态缺失时不猜测。"""
    if not samples:
        return None
    condition = rule.get("condition") or {}
    minutes = float(condition.get("window_minutes") or 0)
    if minutes <= 0 or condition.get("comparison") != "strictly_below":
        return None
    ordered = sorted(samples, key=lambda s: _instant(s["at"]))
    end = _instant(ordered[-1]["at"])
    start = end - timedelta(minutes=minutes)
    window = [sample for sample in ordered if start <= _instant(sample["at"]) <= end]
    if len(window) < 2 or _instant(window[0]["at"]) != start:
        return None
    if any((_instant(right["at"]) - _instant(left["at"])).total_seconds() > 90
           for left, right in zip(window, window[1:])):
        return None
    if condition.get("requires_continuous_supply") and not all(s.get("on") is True for s in window):
        return None
    rate = (float(window[-1]["pressure_bar"]) - float(window[0]["pressure_bar"])) * 60 / minutes
    return rate < float(rule["value"]), rate


def _standby(ids: Mapping[str, str], code: str, preferred: str | None = None) -> tuple[str | None, str | None]:
    """备用空压机：优先用调用方指定，否则取编号最小的另一台。返回（编号，实体 ID）。"""
    if preferred:
        return preferred, ids.get(preferred) or (preferred if preferred.startswith("entity://") else None)
    for key in sorted(ids):
        if key.startswith("AC-") and key != code:
            return key, ids[key]
    return None, None


class EquipmentExpert:
    """设备监测专家。无状态；读数与基线都由调用方放进 `state`。"""

    name = "equipment"

    def propose(self, event: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> ExpertProposal:
        state = dict(state or {})
        event = dict(event or {})
        payload = dict(event.get("payload") or {})
        event_type = str(event.get("event_type") or "")
        entity_id = event.get("entity_id") or state.get("entity_id")
        code = str(state.get("entity_code") or (str(entity_id).rsplit("/", 1)[-1] if entity_id else "") or "")
        trigger = _instant(state.get("trigger_at") or event.get("at") or "1970-01-01T00:00:00+00:00")
        ticket = state.get("ticket_id") or event.get("ticket_id")
        if not ticket:
            raise ValueError("设备监测提案需要 ticket_id（事件单会诊的绑定）")
        readings = dict(state.get("readings") or {})
        samples = list(readings.get("samples") or [])
        stats = _window_stats(samples)
        evidence = tuple(state.get("evidence_refs") or ())
        knowledge = set(state.get("knowledge") or ())
        if payload.get("kind") == "metering" or event_type in METERING_TYPES:
            return self._metering(payload, state, trigger, ticket, evidence)
        if event_type in ("compressor_pressure_drop", "compressor_leak", "air_leak"):
            proposal = self._leak(code, entity_id, trigger, ticket, stats, state, knowledge, evidence,
                                  severity=int(event.get("severity") or 0))
            summary = str(event.get("summary") or payload.get("observation") or "")
            matched = [rule for rule in state.get("published_advisories", ())
                       if rule.get("condition", {}).get("event_type") == event_type
                       and rule.get("condition", {}).get("observation")
                       and rule["condition"]["observation"] in summary]
            if matched:
                advice = "；".join(rule["condition"]["recommendation"] for rule in matched)
                refs = tuple(f"rule:{rule['rule_id']}" for rule in matched)
                return proposal.model_copy(update={"diagnosis": (proposal.diagnosis or "") + "；历史工单复核建议：" + advice,
                                                   "evidence_refs": proposal.evidence_refs + refs})
            return proposal
        if event_type in ("motor_overload", "equipment_overload") or payload.get("fault") == "motor_overload":
            return self._overload(code, entity_id, trigger, ticket, stats, state, evidence)
        if event_type == "lighting_fault":
            return self._no_action(ticket, 0.55, "照明回路失电不属设备监测，由能源专家切换回路", evidence)
        if event_type == "equipment_fault":
            if not entity_id:
                return self._no_action(ticket, 0.2, "故障告警缺设备对象，无法研判", evidence)
            return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.75,
                                  actions=(_act(str(entity_id), "repair", trigger, 60, "needs_human"),),
                                  cost=ProposalCost(cny=0.0), diagnosis="设备故障，开维修工单由值班人员确认",
                                  evidence_refs=evidence)
        if event_type == "unknown_combo":
            return self._unknown_combo(str(event.get("summary") or payload.get("observation") or ""), entity_id, trigger, ticket,
                                       dict(state.get("ids") or {}), knowledge, evidence, state.get('published_combo_rule'))
        if event_type == "power_quality_dip":
            device = code or (str(entity_id).rsplit("/", 1)[-1] if entity_id else "空压机")
            return self._no_action(ticket, 0.6, f"{device} 判为设备侧电机绕组故障，欠压跳机只是表象；"
                                                f"与能源专家的电网侧电压暂降判断不同，建议停机检修核实，交裁决",
                                   evidence)
        return self._no_action(ticket, 0.2, "证据不足或事件不属设备监测领域，不处置", evidence)

    # ---- 空压泄漏
    def _leak(self, code: str, entity_id: str | None, trigger: datetime, ticket: Any, stats: dict, state: Mapping[str, Any],
              knowledge: set[str], evidence: tuple[str, ...], severity: int = 0) -> ExpertProposal:
        observation = dict(state.get("observation") or {})
        observation_verdict = self._from_observation(code, entity_id, trigger, ticket, observation, state, evidence, knowledge)
        if observation_verdict is not None:
            return observation_verdict
        thresholds = DEFAULT_LEAK_THRESHOLDS | dict(state.get("leak_thresholds") or {})
        baseline = dict(state.get("baseline") or {})
        base_ratio = float(baseline.get("running_ratio", 0.0))
        base_current = max(1e-6, float(baseline.get("current_mean_a", 1.0)))
        base_oil = float(baseline.get("oil_mean_c", 65.0))
        ratio_delta = stats["running_ratio"] - base_ratio
        current_ratio = stats["current_mean_a"] / base_current
        slope = stats["pressure_drop_bar_per_h"]
        published = state.get("published_leak_rule")
        measured = _published_pressure(published, list((state.get("readings") or {}).get("samples") or [])) if published else None
        if published:
            pressure_leak = measured[0] if measured is not None else None
            if measured is not None:
                slope = measured[1]
        else:
            pressure_leak = slope <= float(thresholds["pressure_drop_bar_per_h"]) and stats["pressure_end_bar"] < stats["pressure_start_bar"] - 0.1
        shape_leak = ratio_delta >= float(thresholds["running_ratio_delta"]) and current_ratio >= float(thresholds["current_ratio"])
        oil_residual = stats["oil_end_c"] - base_oil
        if not (pressure_leak or shape_leak):
            rule_note = ""
            if published and pressure_leak is None:
                rule_note = "手册要求的完整压降窗口或持续供气证据不足，暂不能按该规则判定泄漏；"
            elif published:
                rule_note = "手册规定窗口内压降未超过已发布阈值；"
            if severity >= 2:
                air = (state.get("ids") or {}).get("air_system")
                if "compressor_leak_rule" in knowledge:
                    actions: list[ProposedAction] = []
                    if air:
                        actions.append(_act(air, "isolate_leak", trigger, 1))
                    if entity_id:
                        actions.append(_act(str(entity_id), "repair", trigger + timedelta(hours=2), 60, "needs_human"))
                    return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.65, actions=tuple(actions),
                                          cost=ProposalCost(cny=-1.0), evidence_refs=evidence + ("knowledge:compressor_leak_rule",),
                                          diagnosis=rule_note + "压力告警已到，但读数窗口未见明显趋势；按《空压机运行维护手册》先隔离泄漏段，维修由值班人员确认")
                if air:
                    return ExpertProposal(
                        expert="equipment", ticket_id=str(ticket), confidence=0.45,
                        actions=(_act(air, "isolate_leak", trigger, 1, "needs_diagnosis", depends_on=("leak_check",)),),
                        cost=ProposalCost(cny=0.0), evidence_refs=evidence,
                        diagnosis=rule_note + "压力异常；现场缺少泄漏判别规则，待诊断：看压力趋势与电流是否同时异常")
            diagnosis = (rule_note + f"告警未复现：运行占比 {stats['running_ratio']:.0%}（同期基线 {base_ratio:.0%}），"
                         f"电流均值 {stats['current_mean_a']:.1f} A（基线 {base_current:.1f} A），压力 {slope:+.2f} bar/h；继续观察，不处置")
            return self._no_action(ticket, 0.2, diagnosis, evidence)
        if pressure_leak:
            confidence = (0.55 + 0.2 * min(1.0, abs(slope) / 2.0) + 0.15 * min(1.0, max(0.0, current_ratio - 1.0) / 0.5)
                          + 0.1 * min(1.0, max(0.0, ratio_delta) / 0.4))
        else:
            confidence = 0.5 + 0.15 * min(1.0, max(0.0, current_ratio - 1.0) / 0.5) + 0.1 * min(1.0, max(0.0, ratio_delta) / 0.4)
        if oil_residual >= float(thresholds["oil_residual_c"]):
            confidence += 0.03
        refs = evidence
        if published and pressure_leak:
            refs += (f"rule:{published['rule_id']}",)
        if "compressor_leak_rule" in knowledge:  # 知识规则给出阈值；B30 抽出后替换默认值
            confidence += 0.03
            refs += ("knowledge:compressor_leak_rule",)
        confidence = round(min(0.97, confidence), 2)
        standby_code, standby_entity = _standby(state.get("ids") or {}, code, state.get("standby"))
        actions: list[ProposedAction] = []
        if entity_id and standby_entity:
            actions.append(_act(str(entity_id), "switch_standby", trigger, 1, to=standby_entity))
        air = (state.get("ids") or {}).get("air_system")
        if air:
            actions.append(_act(air, "isolate_leak", trigger, 1))
        if entity_id:
            actions.append(_act(str(entity_id), "repair", trigger + timedelta(hours=2), 60, "needs_human"))
        diagnosis = (f"空压机泄漏：同期运行占比 {stats['running_ratio']:.0%}（基线 {base_ratio:.0%}），"
                     f"电流均值 {stats['current_mean_a']:.1f} A（基线 {base_current:.1f} A），"
                     f"压力 {slope:+.2f} bar/h、油温 {stats['oil_end_c']:.0f} ℃（基线 {base_oil:.0f} ℃）；"
                     f"切换到备用机 {standby_code or '—'}、隔离泄漏段并开维修工单")
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=confidence, actions=tuple(actions),
                              cost=ProposalCost(cny=-3.0), diagnosis=diagnosis, evidence_refs=refs,
                              safety_tags=())

    # ---- 观测层：读数可疑时先试探，再按试探结果下结论（D59 / D61）
    def _from_observation(self, code: str, entity_id: str | None, trigger: datetime, ticket: Any, observation: Mapping[str, Any],
                          state: Mapping[str, Any], evidence: tuple[str, ...], knowledge: set[str]) -> ExpertProposal | None:
        if not observation:
            return None
        trial = dict(observation.get("trial") or {})
        alarms = set(observation.get("alarms") or ())
        candidates = list(observation.get("candidates") or ())
        if trial.get("verdict") == "sensor_fault":
            return self._sensor_conclusion(code, entity_id, trigger, ticket, observation, trial, evidence)
        if trial and trial.get("verdict") in (None, "unfinished"):
            return self._no_action(ticket, 0.4, "试探未完成：观察窗口还没结束或读数不足，改用其他证据或问值班人员", evidence)
        if "air_current_pressure" in alarms and trial.get("verdict") is None \
                and {"sensor", "equipment"} <= {c["category"] for c in candidates}:
            return self._trial_proposal(code, trigger, ticket, observation, candidates, state, evidence)
        return None

    def _trial_proposal(self, code: str, trigger: datetime, ticket: Any, observation: Mapping[str, Any], candidates: list[dict],
                        state: Mapping[str, Any], evidence: tuple[str, ...]) -> ExpertProposal:
        standby_code, standby_entity = _standby(state.get("ids") or {}, code, state.get("standby"))
        hypothesis_labels = [c["label"] for c in candidates]
        window = 3
        if not standby_entity or state.get("standby_running"):
            reason = "没有可用的备用空压机做试探" if not standby_entity else f"备用机 {standby_code} 已在运行，启动不会带来可区分的变化"
            return self._no_action(ticket, 0.4, f"压力读数与电流不一致，候选：{'、'.join(hypothesis_labels)}；{reason}，转值班人员核对压力表", evidence)
        action = _act(standby_entity, "start", trigger, window, trial=True, kw=30.0, pressure_boost_bar=0.2,
                      observation_minutes=window, hypotheses=hypothesis_labels,
                      expected={"sensor_fault": "压力读数不动，虚拟传感器推算值变化", "equipment_fault": "压力读数跟着变"})
        diagnosis = (f"压力读数与电流/产线不一致，候选：{'、'.join(hypothesis_labels)}；"
                     f"先短时启动备用机 {standby_code or code} {window} 分钟试探，按试探后的新读数判断")
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.6, actions=(action,), cost=ProposalCost(cny=0.0),
                              diagnosis=diagnosis, evidence_refs=evidence + tuple(f"candidate:{c['id']}" for c in candidates[:4]))

    def _sensor_conclusion(self, code: str, entity_id: str | None, trigger: datetime, ticket: Any, observation: Mapping[str, Any],
                           trial: Mapping[str, Any], evidence: tuple[str, ...]) -> ExpertProposal:
        before = trial.get("before") or {}
        after = trial.get("after") or {}
        readings = observation.get("readings") or {}
        pressure = (readings.get("AIR-HDR.P") or {}).get("value")
        virtual = ((observation.get("virtuals") or {}).get("VS-AIR-P") or {}).get("value")
        panel = self._panel(entity_id)
        action = _act(panel, "log_maintenance", trigger, 30, "needs_human", kind="metering", sensor="AIR-HDR.P",
                      issue="stuck", virtual_sensor="VS-AIR-P", virtual_value=virtual, nature="derived")
        diagnosis = (f"试探后压力读数 {pressure if pressure is not None else '—'} bar 不变"
                     f"（{before.get('pressure', '—')} → {after.get('pressure', '—')}，新时间戳 {after.get('at', '—')}），"
                     f"虚拟传感器推算 {virtual if virtual is not None else '—'} bar；判压力传感器卡死，改用推算值继续运行，"
                     f"开计量检修工单，不停机")
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.9, actions=(action,), cost=ProposalCost(cny=0.0),
                              diagnosis=diagnosis, evidence_refs=evidence + (f"trial:{trial.get('started_at', '')}", "virtual:VS-AIR-P"))

    @staticmethod
    def _panel(entity_id: str | None) -> str:
        plant = str(entity_id).split("/")[2] if entity_id and str(entity_id).startswith("entity://") else "demo-electronics"
        return f"entity://{plant}/panel/P-AIR"

    # ---- 电机过载
    def _overload(self, code: str, entity_id: str | None, trigger: datetime, ticket: Any, stats: dict, state: Mapping[str, Any],
                  evidence: tuple[str, ...]) -> ExpertProposal:
        rated = float(state.get("rated_kw") or 0.0)
        rated_current = plant_rated_current_a(rated) if rated else float(state.get("rated_current_a") or 0.0)
        current = stats["current_mean_a"]
        if not entity_id or (rated_current and current <= 1.1 * rated_current):
            return self._no_action(ticket, 0.25, f"电流均值 {current:.1f} A 未超过额定，过载告警未复现", evidence)
        standby_code, standby_entity = _standby(state.get("ids") or {}, code, state.get("standby"))
        actions: list[ProposedAction] = []
        if standby_entity:
            actions.append(_act(str(entity_id), "switch_standby", trigger, 1, to=standby_entity))
        actions.append(_act(str(entity_id), "repair", trigger + timedelta(hours=1), 60, "needs_human"))
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.82, actions=tuple(actions),
                              cost=ProposalCost(cny=-1.0), evidence_refs=evidence,
                              diagnosis=f"空压机电机过载：电流均值 {current:.1f} A 超过额定 {rated_current:.1f} A，"
                                        f"切到备用机 {standby_code or '—'} 并开维修工单")

    # ---- 组合异常：无规则只给低置信诊断转人工，有 combo_rule 时按纠错知识复位
    def _unknown_combo(self, summary: str, entity_id: str | None, trigger: datetime, ticket: Any, ids: Mapping[str, str],
                       knowledge: set[str], evidence: tuple[str, ...], published: Mapping[str, Any] | None = None) -> ExpertProposal:
        observed = f"（{summary}）" if summary else ""
        if published:
            if 'actions' not in published['condition']:
                note = published['condition'].get('note', '')
                return self._no_action(ticket, 0.4, f'已发布纠错知识：{note}；缺少明确动作，转人工核实',
                                       evidence + (f"rule:{published['rule_id']}",))
            actions = published['condition']['actions']
            valid_targets = set(ids.values()) | ({str(entity_id)} if entity_id else set())
            if not all(isinstance(a, Mapping) and set(a) <= {'entity_id', 'command'}
                       and a.get('entity_id') in valid_targets and a.get('command') == 'repair' for a in actions):
                return self._no_action(ticket, 0.25, '已发布规则的动作或目标不可执行，转人工核实', evidence)
            return ExpertProposal(expert='equipment', ticket_id=str(ticket), confidence=0.85,
                                  actions=tuple(_act(a['entity_id'], a['command'], trigger, 1) for a in actions),
                                  cost=ProposalCost(cny=0.0), evidence_refs=evidence + (f"rule:{published['rule_id']}",),
                                  diagnosis=f"组合异常{observed}命中已发布纠错规则 {published['rule_id']}")
        if "combo_rule" not in knowledge:
            return self._no_action(ticket, 0.25, f"未见过的组合异常{observed}：知识里没有对应规则，不自动处置；"
                                                 f"转值班人员核实根因与影响范围", evidence)
        targets = [str(entity_id)] if entity_id else []
        compressor = next((ids[key] for key in sorted(ids) if key.startswith("AC-") and ids[key] not in targets), None)
        if compressor:
            targets.append(compressor)
        if not targets:
            return self._no_action(ticket, 0.3, f"组合异常{observed}有纠错规则，但运行简报里没有可复位的设备对象", evidence)
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.85,
                              actions=tuple(_act(target, "repair", trigger, 1) for target in targets),
                              cost=ProposalCost(cny=0.0), evidence_refs=evidence + ("knowledge:combo_rule",),
                              diagnosis=f"照明与空压同时异常{observed}：同一生产总配电柜接触不良，按纠错知识复位并记检修记录")

    # ---- 计量异常（D05 的 24 条）
    def _metering(self, payload: Mapping[str, Any], state: Mapping[str, Any], trigger: datetime, ticket: Any,
                  evidence: tuple[str, ...]) -> ExpertProposal:
        label = payload.get("issue_label") or METERING_LABELS.get(str(payload.get("issue_type")), "计量回路异常")
        meter = payload.get("meter") or payload.get("asset_id") or "未知表计"
        panel = payload.get("panel") or state.get("panel") or (state.get("ids") or {}).get("P-PROD") or "entity://plant/panel/P-PROD"
        refs = evidence + ("d05:meter_data_quality_log",)
        action = _act(str(panel), "log_maintenance", trigger, 30, "needs_human", kind="metering", meter=str(meter),
                      issue_type=str(payload.get("issue_type") or ""), issue_label=label, nature=str(payload.get("nature") or "derived"))
        diagnosis = (f"数据异常、设备本身正常：表计 {meter} {label}；读数与设备运行状态及同级表计矛盾，"
                     f"开计量检修工单，不切换设备")
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=0.9, actions=(action,), cost=ProposalCost(cny=0.0),
                              diagnosis=diagnosis, evidence_refs=refs)

    # ---- 无动作
    @staticmethod
    def _no_action(ticket: Any, confidence: float, diagnosis: str, evidence: tuple[str, ...]) -> ExpertProposal:
        return ExpertProposal(expert="equipment", ticket_id=str(ticket), confidence=round(confidence, 2), actions=(),
                              cost=ProposalCost(cny=0.0), diagnosis=diagnosis, evidence_refs=evidence)
