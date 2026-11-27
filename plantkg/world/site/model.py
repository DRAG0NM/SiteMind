"""工厂装配清单：区域、楼层、设备与配电拓扑（B18）。

`SiteModel.load(site_yaml)` 读取 configs/sites/factory/site.yaml，展开逐层设备，校验拓扑连通，
输出设备清单、配电路径和 B01 `Entity` 记录。清单是多个专家的共同输入，字段增改只在本模块做，下游只读。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from plantkg.core.contracts import Entity


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # grid / transformer / bus / panel
    label: str
    parent: str | None = None
    zone: str | None = None
    capacity_kva: float | None = None


@dataclass(frozen=True)
class Equipment:
    entity_id: str
    code: str
    type: str
    label: str
    zone: str
    panel: str
    rated_kw: float
    load: str  # d05:<分项> / synthetic:<模型> / none
    floor: int | None = None
    shiftable: bool = False
    ontology_class: str | None = None
    flags: tuple[str, ...] = ()


@dataclass
class SiteModel:
    plant_id: str
    site_name: str
    plant_tz: str
    zones: dict
    nodes: dict[str, Node]
    equipment: tuple[Equipment, ...]
    power_factor: float
    config: dict = field(repr=False)
    config_sha256: str = ""
    config_path: Path | None = None

    @classmethod
    def load(cls, site_yaml: str | Path) -> SiteModel:
        path = Path(site_yaml)
        raw = path.read_bytes()
        data = yaml.safe_load(raw)
        plant_id = data["plant_id"]
        zones = data["zones"]
        nodes = {}
        for n in data["distribution"]["nodes"]:
            if n["id"] in nodes:
                raise ValueError(f"配电节点重复：{n['id']}")
            nodes[n["id"]] = Node(id=n["id"], type=n["type"], label=n["label"], parent=n.get("parent"),
                                  zone=n.get("zone"), capacity_kva=n.get("capacity_kva"))
        equipment = []
        for item in data["equipment"]:
            floors = range(1, zones[item["zone"]]["floors"] + 1) if item.get("per_floor") else [item.get("floor")]
            for floor in floors:
                code = f"{item['code']}{floor:02d}" if item.get("per_floor") else item["code"]
                label = f"{item['label']}（{floor} 层）" if item.get("per_floor") else item["label"]
                equipment.append(Equipment(
                    entity_id=f"entity://{plant_id}/{item['type']}/{code}", code=code, type=item["type"],
                    label=label, zone=item["zone"], panel=item["panel"], rated_kw=float(item["rated_kw"]),
                    load=item["load"], floor=floor, shiftable=bool(item.get("shiftable", False)),
                    ontology_class=item.get("ontology_class"), flags=tuple(zones[item["zone"]].get("flags", ()))))
        site = cls(plant_id=plant_id, site_name=data["site_name"], plant_tz=data["plant_tz"], zones=zones,
                   nodes=nodes, equipment=tuple(equipment), power_factor=float(data["distribution"]["power_factor"]),
                   config=data, config_sha256=hashlib.sha256(raw).hexdigest(), config_path=path)
        site.validate()
        return site

    def validate(self) -> None:
        codes = [e.code for e in self.equipment]
        if len(codes) != len(set(codes)):
            raise ValueError("设备编号重复")
        if set(codes) & set(self.nodes):
            raise ValueError(f"设备编号与配电节点重名：{sorted(set(codes) & set(self.nodes))}")
        categories = self.config["production_base"]["categories"]
        for e in self.equipment:
            if e.zone not in self.zones:
                raise ValueError(f"{e.code} 的区域 {e.zone} 未定义")
            if self.nodes.get(e.panel, Node("", "", "")).type != "panel":
                raise ValueError(f"{e.code} 接入的 {e.panel} 不是配电柜")
            if self.nodes[e.panel].zone != e.zone:
                raise ValueError(f"{e.code} 属于 {e.zone}，却接在 {self.nodes[e.panel].zone} 的配电柜 {e.panel}")
            if not e.rated_kw > 0:
                raise ValueError(f"{e.code} 的额定功率必须为正数")
            if e.load.startswith("d05:") and e.load[4:] not in categories:
                raise ValueError(f"{e.code} 的负荷分项 {e.load} 不在 production_base.categories 中")
            if not any(self.nodes[n].type == "transformer" for n in self.path(e.code)[1:]):
                raise ValueError(f"{e.code} 的配电路径上没有变压器")
            if e.floor is not None and not 1 <= e.floor <= self.zones[e.zone]["floors"]:
                raise ValueError(f"{e.code} 的楼层 {e.floor} 超出 {e.zone}")
            Entity(entity_id=e.entity_id, entity_type=e.type, label=e.label, plant_id=self.plant_id)
        for node in self.nodes.values():
            self.path(node.id)  # 每个节点都能回到电网，且没有环
            if node.type == "transformer" and not (node.capacity_kva or 0) > 0:
                raise ValueError(f"变压器 {node.id} 缺容量上限")

    def path(self, node_id: str) -> list[str]:
        """从节点或设备编号到电网的配电路径，如 ['AC-1', 'P-AIR', 'P-PROD', 'LV1', 'TX1', 'GRID']。"""
        by_code = {e.code: e for e in self.equipment}
        out = [node_id] if node_id in by_code else []
        current = by_code[node_id].panel if node_id in by_code else node_id
        while current is not None:
            if current not in self.nodes:
                raise ValueError(f"配电路径断开：{node_id} → {current}")
            if current in out:
                raise ValueError(f"配电路径成环：{node_id}")
            out.append(current)
            current = self.nodes[current].parent
        if out[-1] != "GRID" and self.nodes[out[-1]].type != "grid":
            raise ValueError(f"{node_id} 没有接到电网")
        return out

    def transformer_of(self, code: str) -> Node:
        return next(self.nodes[n] for n in self.path(code) if n in self.nodes and self.nodes[n].type == "transformer")

    def transformer_limit_kw(self, transformer_id: str) -> float:
        return self.nodes[transformer_id].capacity_kva * self.power_factor

    def entities(self) -> list[Entity]:
        return [Entity(entity_id=e.entity_id, entity_type=e.type, label=e.label, plant_id=self.plant_id,
                       ontology_class=e.ontology_class) for e in self.equipment]

    def table(self) -> list[dict]:
        """一张表：设备、所属区域、楼层、额定功率、负荷来源与配电路径。"""
        return [{"entity_id": e.entity_id, "code": e.code, "label": e.label, "zone": self.zones[e.zone]["label"],
                 "floor": e.floor, "rated_kw": e.rated_kw, "load": e.load, "flags": list(e.flags),
                 "path": " → ".join(reversed(self.path(e.code)))} for e in self.equipment]
