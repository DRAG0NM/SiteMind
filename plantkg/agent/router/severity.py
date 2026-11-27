"""D55 严重程度入口与快通道的规则匹配（B27）。

参数读 `configs/sites/factory/severity.yaml`（configured/synthetic）；金额线可由 PolicyConfig.major_loss_cny 覆盖。
生产不停机就无法判断的项，靠注入的图谱/试算回调补：`safety_hit`（B25 safety_tags）、`capacity_trip`（变压器/配电柜过载）、
`downstream_lines`（图谱下游对象）、`entity_type`（对象类型）。没接图谱时按配置的产线清单与事件类型清单估算。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from plantkg.core.contracts import DayEvent, Interval, PolicyConfig, SeverityEstimate

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "configs/sites/factory/severity.yaml"
UNAVAILABLE = "unavailable"  # 观测层根本没有读数：不是可疑，但也不能当证据
# B17 容量违规没有 entity_id，节点与负载率只在 detail 里：`<时刻> TX1 1280.0 kW > 容量 1150.0 kW`
CAPACITY_DETAIL = re.compile(r"(?P<node>\S+) (?P<flow>[\d.]+) kW > 容量 (?P<limit>[\d.]+) kW")


def load_severity_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def major_loss_line(policy: PolicyConfig | None, config: Mapping[str, Any]) -> float | None:
    """重大损失金额线：以带版本的 PolicyConfig 为准，缺省回落到本卡的 severity.yaml（configured）。"""
    if policy is not None and policy.major_loss_cny is not None:
        return float(policy.major_loss_cny)
    line = config.get("major_loss_cny")
    return float(line) if line is not None else None


class SeverityEstimator:
    """用“不采取任何动作”的未保护情形估算损失与是否停产。运行电费、候梯与舒适软代价不计入。

    `trial`/`snapshot` 是 B17 的 TrialRunner 与快照来源；接上后 estimate 会跑一次空动作的未保护试算：
    同一节点按时间戳连续超容 capacity_minutes 分钟视为跳闸，其下游 downstream_stop_types 对象视为停产。
    """

    def __init__(self, config: Mapping[str, Any] | None = None, *, major_loss_cny: float | None = None,
                 policy: PolicyConfig | None = None,
                 safety_hit: Callable[[str], bool] | None = None, capacity_trip: Callable[[str, float, int], bool] | None = None,
                 downstream_lines: Callable[[str], list[str]] | None = None,
                 entity_type: Callable[[str], str | None] | None = None,
                 readings: Callable[[str, datetime, datetime], Sequence[Mapping[str, Any]]] | None = None,
                 trial: Callable[[Any, Sequence[Any], Interval], Any] | None = None,
                 snapshot: Callable[[DayEvent], Any] | None = None) -> None:
        self.config = dict(load_severity_config() if config is None else config)
        self.policy = policy
        self.major_loss_cny = major_loss_cny if major_loss_cny is not None else major_loss_line(policy, self.config)
        self._safety_hit = safety_hit
        self._capacity_trip = capacity_trip
        self._downstream_lines = downstream_lines
        self._entity_type = entity_type
        self._readings = readings
        self._trial = trial
        self._snapshot = snapshot

    def estimate(self, event: DayEvent) -> SeverityEstimate:
        """D59：先读观测层。关键读数有残差报警或质量不是 ok 时不判停产，交慢通道先核实现场。"""
        reading = self._key_reading(event)
        suspect = self._suspect(reading)
        tripped = self._tripped(self._unprotected_trial(event)) if not suspect else []
        stop = not suspect and (bool(tripped) or self._production_stop(event))
        lines = self._stop_lines(tripped) or self._lines(event)
        minutes = int(self.config.get("stop_minutes", 30)) if stop else 0
        damage = float(self.config.get("equipment_damage_cny", {}).get(self._type_of(event) or "default",
                                                                     self.config.get("equipment_damage_cny", {}).get("default", 0)))
        loss = minutes * float(self.config.get("line_value_cny_per_min", 0)) * max(1, len(lines)) + damage
        basis = (f"未保护 {self.config.get('horizon_min', 60)} 分钟内："
                 f"{('跳闸 ' + '、'.join(tripped) + '，') if tripped else ''}"
                 f"{'停产 ' + str(minutes) + ' 分钟 × ' + str(len(lines)) + ' 个对象（' + '、'.join(lines) + '）' if stop else '不停产'}，"
                 f"设备类型 {self._type_of(event) or '未知'} 估值 {damage:.0f} 元")
        if suspect:
            loss = 0.0  # 读数不可信时不算损失，按现场核实后的结论再说
            basis += self._suspect_note(reading)
        return SeverityEstimate(loss_cny=round(loss, 2), production_stop=stop,
                                horizon_min=int(self.config.get("horizon_min", 60)), basis=basis, field_check=suspect)

    def urgent_reason(self, event: DayEvent, estimate: SeverityEstimate) -> str | None:
        """任一满足即立即派单：安全标记命中、试算损失超线、会停产。读数可疑时不派单，交慢通道先核实。"""
        if self.is_safety(event):
            return "安全标记命中（B25 safety_tags），按涉及处理" + (self._suspect_note(None) if estimate.field_check else "")
        if estimate.field_check:
            return None
        if estimate.production_stop:
            return f"未保护时会停产：{estimate.basis}"
        line = self.major_loss_cny
        if line is not None and estimate.loss_cny > float(line):
            return f"试算损失 {estimate.loss_cny:.0f} 元超过重大损失线 {float(line):.0f} 元"
        return None

    @staticmethod
    def _suspect_note(reading: Mapping[str, Any] | None) -> str:
        detail = ""
        if reading:
            quality = reading.get("quality")
            detail = f"质量 {quality}" if quality and quality != "ok" else "残差报警"
        return (f"；关键读数可疑（{detail or '残差报警'}）：请先现场核实，保护动作是否执行交 Agent，"
                f"截止内无结论默认执行")

    def _key_reading(self, event: DayEvent) -> Mapping[str, Any] | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if isinstance(payload.get("reading"), dict):
            return payload["reading"]
        if self._readings and event.entity_id:
            try:
                got = list(self._readings(event.entity_id, event.at - timedelta(minutes=10), event.at))
            except Exception:  # 观测层不可用时不冒充有读数
                got = []
            return next((r for r in got if self._suspect(r)), got[-1] if got else None)  # 同设备多个传感器，有一个可疑就算
        return None

    @staticmethod
    def _suspect(reading: Mapping[str, Any] | None) -> bool:
        if not reading:
            return False
        if reading.get("residual_alarm"):
            return True
        return str(reading.get("quality", "ok")).lower() not in ("ok", "good", "okay", UNAVAILABLE)

    def is_safety(self, event: DayEvent) -> bool:
        if event.event_type in set(self.config.get("safety_event_types", ())):
            return True
        return bool(self._safety_hit and event.entity_id and self._safety_hit(event.entity_id))

    # ---- 内部
    def _production_stop(self, event: DayEvent) -> bool:
        if event.event_type in set(self.config.get("stop_event_types", ())):
            return True
        return bool(self._capacity_trip and event.entity_id and self._capacity_trip(
            event.entity_id, float(self.config.get("capacity_ratio", 0.95)), int(self.config.get("capacity_minutes", 5))))

    def _stop_lines(self, nodes: Sequence[str]) -> list[str]:
        """跳闸节点在图谱上的下游里，会算停产的对象（默认生产线与空压站）；图谱没接时为空，由配置的产线清单兜底。"""
        stop_types = set(self.config.get("downstream_stop_types", ()))
        found: dict[str, None] = {}
        for node in nodes:
            for code in (self._downstream_lines(node) if self._downstream_lines else ()):
                if not stop_types or not self._entity_type or self._entity_type(code) in stop_types:
                    found[code] = None
        return list(found)

    def _lines(self, event: DayEvent) -> list[str]:
        if self._downstream_lines and event.entity_id:
            found = self._downstream_lines(event.entity_id)
            if found:
                return found
        return list(self.config.get("production_lines", ()))

    def _type_of(self, event: DayEvent) -> str | None:
        found = self._entity_type(event.entity_id) if self._entity_type and event.entity_id else None
        return found or (event.payload.get("entity_type") if isinstance(event.payload, dict) else None)

    def _unprotected_trial(self, event: DayEvent) -> Any | None:
        """D55/D57：跑一次“不采取任何动作”的试算（TrialRunner + 快照）；没接上时返回 None。"""
        if self._trial is None or self._snapshot is None:
            return None
        horizon_min = int(self.config.get("horizon_min", 60))
        try:
            return self._trial(self._snapshot(event), (), Interval(start=event.at, end=event.at + timedelta(minutes=horizon_min)))
        except Exception:  # 试算不可用时不冒充有结论
            return None

    def _tripped(self, trial: Any | None) -> list[str]:
        """同一节点按时间戳连续超容达到 capacity_minutes 分钟的节点；超容比例低于 capacity_ratio 的分钟不算。

        “未保护试算”要的是绝对结论：既有越界（TrialResult.baseline_violations）和相对新增的违规一起看。"""
        ratio = float(self.config.get("capacity_ratio", 0.95))
        needed = int(self.config.get("capacity_minutes", 5))
        minutes: dict[str, set[datetime]] = {}
        existing = getattr(trial, "violations", None) or ()
        baseline = getattr(trial, "baseline_violations", None) or ()
        for violation in (*existing, *baseline):
            match = CAPACITY_DETAIL.search(str(getattr(violation, "detail", ""))) if violation.kind == "capacity" else None
            if match and float(match["flow"]) >= ratio * float(match["limit"]):
                minutes.setdefault(match["node"], set()).add(violation.at)
        return [node for node, seen in sorted(minutes.items()) if self._longest_run(seen) >= needed]

    @staticmethod
    def _longest_run(seen: set[datetime]) -> int:
        best = run = 0
        previous = None
        for at in sorted(seen):
            run = run + 1 if previous is not None and at - previous == timedelta(minutes=1) else 1
            best, previous = max(best, run), at
        return best


class EventTypeRuleMatcher:
    """RuleMatcher 的 fallback：事件类型在规则覆盖白名单里才算“有已发布规则覆盖”（synthetic）。

    B30/B32 的规则与 B25 playbooks 接上后，用注册表里的 RuleMatcher 覆盖本实现。
    """

    def __init__(self, covered: Mapping[str, Any] | None = None) -> None:
        self.covered = frozenset(covered.get("rule_covered_events", ()) if covered is not None else ())

    @classmethod
    def from_config(cls, path: str | Path = DEFAULT_CONFIG) -> EventTypeRuleMatcher:
        return cls(load_severity_config(path))

    def covers(self, event: DayEvent) -> bool:
        return event.event_type in self.covered
