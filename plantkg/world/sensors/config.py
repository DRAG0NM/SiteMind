"""观测层配置（B22 / D59）：传感器布置、一致性关系、虚拟传感器与故障特征矩阵。

配置在 `configs/sites/factory/sensors.yaml`。节点编号引用全局图谱与 B18 装配；本模块只读配置，
不 import 图谱或别的平面，避免世界层反向依赖知识层。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "configs" / "sites" / "factory" / "sensors.yaml"
SOURCES = ("air_header", "power_to_current", "device_field", "total_power", "members_power", "zone_temperature")


@dataclass(frozen=True)
class SensorDef:
    id: str
    entity: str
    metric: str
    unit: str
    source: str
    members: tuple[str, ...] = ()
    field: str | None = None
    value_range: tuple[float, float] | None = None
    decimals: int = 2


@dataclass(frozen=True)
class RelationDef:
    id: str
    label: str
    kind: str
    nodes: tuple[str, ...]
    tolerance: float
    unit: str
    total: str | None = None
    parts: tuple[str, ...] = ()
    currents: tuple[str, ...] = ()
    pressure: str | None = None
    power: str | None = None
    temperature: str | None = None
    params: dict[str, float] = field(default_factory=dict)

    @property
    def sensors(self) -> tuple[str, ...]:
        out = [x for x in (self.total, self.pressure, self.power, self.temperature) if x]
        return tuple(out) + self.parts + self.currents


@dataclass(frozen=True)
class VirtualDef:
    id: str
    label: str
    kind: str
    entity: str
    metric: str
    unit: str
    inputs: tuple[str, ...]
    params: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FaultCandidate:
    id: str
    category: str  # sensor / equipment
    label: str
    sensors: tuple[str, ...] = ()
    nature: str = "measured"  # 人工确认案例覆盖条目标 synthetic，基准配置仍保留原性质


@dataclass(frozen=True)
class SensorConfig:
    sensors: dict[str, SensorDef]
    relations: tuple[RelationDef, ...]
    virtuals: tuple[VirtualDef, ...]
    fault_matrix: dict[str, tuple[FaultCandidate, ...]]
    sensor_fault_candidates: dict[str, FaultCandidate]
    trial: dict[str, float]
    history_minutes: int
    path: Path
    sha256: str
    nature: str

    def __post_init__(self) -> None:
        ids = list(self.sensors)
        if len(ids) != len(set(ids)):
            raise ValueError("传感器 ID 重复")
        unknown = {sid for r in self.relations for sid in r.sensors if sid not in self.sensors}
        unknown |= {sid for v in self.virtuals for sid in v.inputs if sid not in self.sensors}
        if unknown:
            raise ValueError(f"配置引用了不存在的传感器：{sorted(unknown)}")

    def evidence_ref(self) -> str:
        return f"sensors:{self.sha256[:16]}"


def _range(raw: Any) -> tuple[float, float] | None:
    return (float(raw[0]), float(raw[1])) if raw else None


def apply_sensor_overrides(config: SensorConfig, overrides: Iterable[Mapping[str, Any]]) -> SensorConfig:
    """D59：把已发布的人工确认案例追加到故障特征矩阵，不改既有条目；没有可用案例时原样返回。"""
    matrix = {relation_id: list(items) for relation_id, items in config.fault_matrix.items()}
    ids = {item.id for items in matrix.values() for item in items}
    valid = {relation.id for relation in config.relations}
    added = []
    for case in overrides:
        relation_id = str((case or {}).get("relation_id") or "")
        judgement = str((case or {}).get("judgement") or "")
        if relation_id not in valid or judgement not in ("sensor", "equipment"):
            continue
        case_id = "case-" + hashlib.sha256(
            f"{relation_id}|{judgement}|{case.get('label')}|{case.get('sensors')}".encode("utf-8")).hexdigest()[:12]
        if case_id in ids:
            continue
        ids.add(case_id)
        added.append(case_id)
        matrix.setdefault(relation_id, []).append(FaultCandidate(
            id=case_id, category=judgement, label=str(case.get("label") or f"人工确认的{judgement}故障"),
            sensors=tuple(str(item) for item in (case.get("sensors") or ())), nature="synthetic"))
    if not added:
        return config
    sha = hashlib.sha256((config.sha256 + "|" + "|".join(sorted(added))).encode("utf-8")).hexdigest()
    return replace(config, fault_matrix={key: tuple(items) for key, items in matrix.items()}, sha256=sha)


def load_sensor_config(path: str | Path | None = None) -> SensorConfig:
    folder = Path(path) if path else DEFAULT_CONFIG
    raw_bytes = folder.read_bytes()
    data = yaml.safe_load(raw_bytes)
    sensors = {}
    for item in data["sensors"]:
        source = item["source"]
        if source not in SOURCES:
            raise ValueError(f"传感器 {item['id']} 的 source {source} 不可用")
        sensors[item["id"]] = SensorDef(
            id=item["id"], entity=item["entity"], metric=item["metric"], unit=item["unit"], source=source,
            members=tuple(item.get("members", ())), field=item.get("field"),
            value_range=_range(item.get("range")), decimals=int(item.get("decimals", 2)))
    relations = []
    for item in data["relations"]:
        params = {k: float(v) for k, v in (item.get("params") or {}).items()}
        relations.append(RelationDef(
            id=item["id"], label=item["label"], kind=item["kind"], nodes=tuple(item["nodes"]),
            tolerance=float(item["tolerance"]), unit=item["unit"], total=item.get("total"),
            parts=tuple(item.get("parts", ())), currents=tuple(item.get("currents", ())),
            pressure=item.get("pressure"), power=item.get("power"), temperature=item.get("temperature"), params=params))
    virtuals = [VirtualDef(id=item["id"], label=item["label"], kind=item["kind"], entity=item["entity"], metric=item["metric"],
                           unit=item["unit"], inputs=tuple(item["inputs"]),
                           params={k: float(v) for k, v in (item.get("params") or {}).items()})
                for item in data["virtual_sensors"]]
    config_nature = str(data.get("nature", "configured"))
    matrix = {rid: tuple(FaultCandidate(c["id"], c["category"], c["label"], tuple(c.get("sensors", ())),
                                        nature=config_nature) for c in items)
              for rid, items in data["fault_matrix"].items()}
    fault_candidates = {kind: FaultCandidate(**{**item, "nature": config_nature})
                        for kind, item in data["sensor_fault_candidates"].items()}
    return SensorConfig(sensors=sensors, relations=tuple(relations), virtuals=tuple(virtuals), fault_matrix=matrix,
                        sensor_fault_candidates=fault_candidates, trial={k: float(v) for k, v in data.get("trial", {}).items()},
                        history_minutes=int(data.get("history_minutes", 90)), path=folder,
                        sha256=hashlib.sha256(raw_bytes).hexdigest(), nature=config_nature)
