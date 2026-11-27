"""剧本 YAML（B17）：装配引用 B18，事件与值班人员操作按时刻注入，极端场景参数另列。

时刻写法：`"08:05"` 指首日厂区当地时间，`"+1 16:00"` 指第二天；也可写带时区的 ISO 时间。
设备写编号（如 `AC-2`）或完整 `entity://`；区域与电梯群写 `zone:admin`、`elevator_group:admin_low`。
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from plantkg.core.contracts import PolicyConfig, ResponseBudget

ROOT = Path(__file__).resolve().parents[3]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptEvent(_Strict):
    at: str
    type: str
    entity_id: str | None = None
    severity: int = Field(default=2, ge=0, le=5)  # 0 = 噪声，程序准入时丢弃
    source: Literal["script", "random", "sim_limit", "operator_message"] = "script"
    id: str | None = None
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class OperatorAction(_Strict):
    at: str
    kind: Literal["instruction", "upload", "confirm", "correct"]
    payload: dict[str, Any] = Field(default_factory=dict)


class Burst(_Strict):
    """随机告警洪峰：在 [at, at+minutes) 内按种子生成 count 条告警，noise_ratio 的部分是噪声（severity 0）。"""

    at: str
    minutes: int = Field(default=1, ge=1)
    count: int = Field(ge=1)
    alarms: list[dict[str, str]] = Field(min_length=1)  # [{type, entity}]：每条告警从中按种子抽一项
    noise_ratio: float = Field(default=0.9, ge=0, le=1)


class Window(_Strict):
    start: str
    end: str
    factor: float | None = None
    delta_c: float | None = None


class Params(_Strict):
    pv_scale: list[Window] = Field(default_factory=list)  # 云层：光伏乘系数
    outdoor_offset_c: list[Window] = Field(default_factory=list)  # 热浪：室外温度加偏移
    capacity_kw: dict[str, float] = Field(default_factory=dict)  # 配电节点容量覆盖（kW），未写的取 B18 变压器容量
    state_version_tolerance: int = Field(default=20, ge=0)
    capacity_warn_ratio: float = Field(default=0.92, gt=0)  # 超过即产生 sim_limit 事件（每槽一次）


class Initial(_Strict):
    soc: float = Field(default=0.5, ge=0, le=1)
    energy_end_target_kwh: float | None = Field(default=None, ge=0)


class InitialAction(_Strict):
    """开局动作（如极端剧本让备用机先停机，试探才真的有由停转开的变化）。"""

    entity: str
    command: str
    params: dict[str, Any] = Field(default_factory=dict)
    minutes: int = Field(default=1, ge=1)  # 生效区间长度，缺省一分钟（启停类立刻生效）


class SensorFaultPlan(_Strict):
    """观测层传感器故障计划（B22）：到点注入，读取时按 kind 改读数。"""

    sensor_id: str
    kind: Literal["stuck", "drift", "offline", "spike", "bias"]
    at: str
    until: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class Sources(_Strict):
    site: str = "configs/sites/factory/site.yaml"
    storage: str = "configs/sites/factory/storage.yaml"
    pv: str = "configs/sites/factory/pv.yaml"
    d05: str = "fixtures/sites/factory/d05_week.parquet"
    pvgis: str = "fixtures/timeseries/pvgis_2017_excerpt.json"
    tariff_rules: str = "fixtures/packs/tariff/jiangsu_2025_426.yaml"
    tariff_config: str = "fixtures/packs/tariff/tariff.yaml"


class Scenario(_Strict):
    scenario_id: str
    title: str
    nature: Literal["synthetic", "configured", "simulated"] = "synthetic"
    description: str = ""
    sources: Sources = Field(default_factory=Sources)
    date: str  # 首日厂区当地日期
    days: int = Field(default=1, ge=1, le=7)
    seed: int = 0
    initial: Initial = Field(default_factory=Initial)
    knowledge: list[str] = Field(default_factory=list)  # 起始知识标签（知识 v1 的内容）
    policy: dict[str, Any] = Field(default_factory=dict)
    params: Params = Field(default_factory=Params)
    dispatch: dict[str, list[str]] = Field(default_factory=dict)  # 覆盖默认的事件类型 → 专家
    events: list[ScriptEvent] = Field(default_factory=list)
    bursts: list[Burst] = Field(default_factory=list)
    operator_actions: list[OperatorAction] = Field(default_factory=list)
    initial_actions: list[InitialAction] = Field(default_factory=list)
    sensor_faults: list[SensorFaultPlan] = Field(default_factory=list)
    path: str | None = None
    sha256: str = ""

    # ---- 时间
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.site_tz)

    site_tz: str = "Asia/Shanghai"

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.date).replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC"))

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.days)

    def instant(self, text: str) -> datetime:
        text = text.strip()
        if "T" in text:
            value = datetime.fromisoformat(text)
            if value.tzinfo is None:
                raise ValueError(f"ISO 时刻 {text} 必须带时区")
            return value.astimezone(ZoneInfo("UTC"))
        day = 0
        if text.startswith("+"):
            day_text, text = text[1:].split(maxsplit=1)
            day = int(day_text)
        h, m = text.split(":")
        local = datetime.fromisoformat(self.date).replace(hour=int(h), minute=int(m), tzinfo=self.tz) + timedelta(days=day)
        return local.astimezone(ZoneInfo("UTC"))

    def policy_config(self) -> PolicyConfig:
        base = {"policy_version": f"scenario-policy:{self.scenario_id}", "arbitration_weights": {"cny": 1.0, "wait_min": 0.5, "comfort_min": 0.8},
                "priority_profiles": {"default": ("safety", "production", "comfort", "cost")},
                "response_budget": ResponseBudget(layer12_min=1, layer3_min=3)}
        return PolicyConfig.model_validate(base | self.policy)

    def expanded_events(self) -> list[ScriptEvent]:
        """剧本事件 + 按种子展开的洪峰告警，按时刻排序（同一时刻保持剧本顺序）。"""
        rng = random.Random(f"{self.seed}:bursts")
        out = list(self.events)
        for b in self.bursts:
            start = self.instant(b.at)
            for k in range(b.count):
                at = start + timedelta(seconds=rng.randrange(b.minutes * 60))
                noise = rng.random() < b.noise_ratio
                alarm = rng.choice(b.alarms)
                out.append(ScriptEvent(at=at.isoformat(), type=alarm["type"], entity_id=alarm["entity"],
                                       severity=0 if noise else 3, source="random", summary="噪声告警" if noise else "随机告警",
                                       payload={"burst": b.at, "index": k}))
        return sorted(out, key=lambda e: self.instant(e.at))

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else ROOT / path


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    scenario = Scenario.model_validate(data | {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    for e in scenario.expanded_events():
        scenario.instant(e.at)
    for a in scenario.operator_actions:
        scenario.instant(a.at)
    for fault in scenario.sensor_faults:
        scenario.instant(fault.at)
        if fault.until:
            scenario.instant(fault.until)
    return scenario
