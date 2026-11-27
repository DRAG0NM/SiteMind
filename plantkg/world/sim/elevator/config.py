"""电梯群控的物理参数与实体对照（B20）。

参数按常理设定（configured），不是设备选型：层高与速度折成逐层运行 2.5 s，开关门 8 s，每人上车 1.5 s，
轿厢待机 0.6 kW。楼层、轿厢与区域来自 B18 装配；货梯在 B18 里只有 1 个生产层，所以物料路径的楼层
（原料库 1 层、三条产线 2～4 层）标 synthetic，等 B18/B19 给出真实楼层后替换。
"""
from __future__ import annotations

from dataclasses import dataclass

from plantkg.world.site.model import SiteModel

PASSENGER_CAPACITY = 8
FREIGHT_CAPACITY = 4  # 峰电时段合并批次时一车最多带的托盘数
PER_FLOOR_S = 2.5
DOOR_S = 8.0
BOARD_S = 1.5
BATCH_S = 10.0
STANDBY_KW = 0.6
TRIP_KWH = 0.09  # 人梯跑一趟 12 层的耗电（configured）
FREIGHT_TRIP_KWH = 0.6  # 货梯带一托盘跑一趟的耗电（configured）
FREIGHT_FLOORS = (1, 2, 3, 4)  # synthetic：原料库 1 层，LINE-1/2/3 映射到 2/3/4 层


@dataclass(frozen=True)
class CarSpec:
    code: str
    entity_id: str
    zone: str  # low / high / freight
    capacity: int
    rated_kw: float


@dataclass(frozen=True)
class ElevatorConfig:
    plant_id: str
    floors: int
    low_floors: tuple[int, ...]
    high_floors: tuple[int, ...]
    passenger_cars: tuple[CarSpec, ...]
    freight_cars: tuple[CarSpec, ...]
    per_floor_s: float = PER_FLOOR_S
    door_s: float = DOOR_S
    board_s: float = BOARD_S
    batch_s: float = BATCH_S
    standby_kw: float = STANDBY_KW
    trip_kwh: float = TRIP_KWH
    freight_trip_kwh: float = FREIGHT_TRIP_KWH
    freight_capacity: int = FREIGHT_CAPACITY

    @classmethod
    def from_site(cls, site: SiteModel) -> ElevatorConfig:
        floor_count = int(site.zones["admin"]["floors"])
        half = floor_count // 2
        lifts = sorted((e for e in site.equipment if e.type == "passenger_elevator"), key=lambda e: e.code)
        passenger = []
        for index, e in enumerate(lifts):
            if "低区" in e.label:
                zone = "low"
            elif "高区" in e.label:
                zone = "high"
            else:
                zone = "low" if index < len(lifts) / 2 else "high"
            passenger.append(CarSpec(e.code, e.entity_id, zone, PASSENGER_CAPACITY, e.rated_kw))
        freight = tuple(CarSpec(e.code, e.entity_id, "freight", FREIGHT_CAPACITY, e.rated_kw)
                        for e in sorted((e for e in site.equipment if e.type == "freight_elevator"), key=lambda e: e.code))
        return cls(plant_id=site.plant_id, floors=floor_count, low_floors=tuple(range(1, half + 1)),
                   high_floors=tuple(range(half + 1, floor_count + 1)), passenger_cars=tuple(passenger), freight_cars=freight)

    # ---- 实体编号
    def group_id(self, name: str) -> str:
        """admin_low / admin_high / freight / low / high → 电梯群实体 ID。"""
        short = {"low": "admin_low", "high": "admin_high"}.get(name, name)
        return f"entity://{self.plant_id}/elevator_group/{short}"

    def car(self, code: str) -> CarSpec:
        for spec in (*self.passenger_cars, *self.freight_cars):
            if spec.code == code:
                return spec
        raise ValueError(f"电梯 {code} 不在 B18 装配中")

    def eid(self, name: str | None) -> str | None:
        """设备编号、elevator_group:x、zone:x 或完整实体 ID → 实体 ID。"""
        if name is None or name.startswith("entity://"):
            return name
        if name in {c.code for c in (*self.passenger_cars, *self.freight_cars)}:
            return self.car(name).entity_id
        kind, _, code = name.partition(":")
        if kind in ("elevator_group", "group"):
            return self.group_id(code)
        if kind == "zone":
            return f"entity://{self.plant_id}/zone/{code}"
        raise ValueError(f"剧本里的实体 {name} 不在电梯装配中")

    def cars(self, zone: str | None = None) -> tuple[CarSpec, ...]:
        if zone == "freight":
            return self.freight_cars
        if zone in ("low", "high"):
            return tuple(c for c in self.passenger_cars if c.zone == zone)
        return (*self.passenger_cars, *self.freight_cars)

    def zone_floors(self, zone: str) -> tuple[int, ...]:
        if zone == "low":
            return self.low_floors
        if zone == "high":
            return self.high_floors
        return FREIGHT_FLOORS
