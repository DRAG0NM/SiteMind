"""SimPy 电梯群控仿真（B20）：乘客按人流曲线到达，按策略派梯，记录候梯与能耗。

两个模型共用一个离散事件框架：
- 人梯：早高峰上行（首层集中、分区服务）、午间双向、晚高峰下行、低谷少开梯；分区运行把低区/高区
  分开派梯，减少停靠与绕行；
- 货梯：按生产任务优先级与物料紧急度排队，人梯高峰时推迟非紧急任务，峰电时段合并批次。

结果是 simulated：到达曲线来自 B18 人流/物料任务，物理参数 configured，随机数固定种子，同一输入可复现。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import mean

import simpy

from .config import CarSpec, ElevatorConfig


# ---------------------------------------------------------------- 人梯


@dataclass(frozen=True)
class Passenger:
    passenger_id: int
    arrive_at: float  # 相对仿真起点的秒
    origin: int
    destination: int
    direction: str  # up / down


@dataclass(frozen=True)
class PassengerDemand:
    arrivals: tuple[Passenger, ...]
    horizon_s: float
    label: str = "morning_peak"


@dataclass(frozen=True)
class PassengerStrategy:
    name: str  # fixed / zone
    mode: str  # peak_up / lunch / peak_down / off_peak
    active_cars: int | None = None
    return_floor: int | None = None


@dataclass(frozen=True)
class PassengerResult:
    strategy: str
    mode: str
    arrivals: int
    served: int
    avg_wait_s: float  # 到 horizon 仍在排队的乘客按“已等到 horizon”计入
    max_wait_s: float
    p95_wait_s: float
    congestion_min: float
    avg_queue: float
    energy_kwh: float  # = trip_kwh + standby_kwh
    trips: int
    unserved: int = 0  # horizon 结束时仍在排队、未上梯的人数
    trip_kwh: float = 0.0  # 行程能耗
    standby_kwh: float = 0.0  # 待机能耗（按启用梯台数全时长摊入）


@dataclass
class _Car:
    spec: CarSpec
    floor: int = 1
    busy: bool = False
    active: bool = True
    zone: str | None = None  # 分区策略动态分配；固定策略为 None（任意层都服务）
    trips: int = 0
    energy_kwh: float = 0.0


def build_demand(config: ElevatorConfig, mode: str, *, start_s: float = 0.0, minutes: int = 45, staff: int = 600,
                 surge: tuple[int, int, float] | None = None, seed: int = 7) -> PassengerDemand:
    """按厂区作息造到达曲线（synthetic）：peak_up 首层上行，peak_down 下行，lunch 双向，off_peak 低强度。"""
    rng = random.Random(seed)
    per_min = staff / max(minutes, 1)
    if mode == "off_peak":
        per_min *= 0.2
    carry, arrivals, pid = 0.0, [], 0
    for minute in range(minutes):
        factor = 1.0
        if surge is not None and surge[0] <= minute < surge[1]:
            factor = surge[2]
        carry += per_min * factor
        count = int(carry)
        carry -= count
        for _ in range(count):
            at = start_s + minute * 60 + rng.random() * 60
            if mode == "peak_up":
                origin, destination = 1, rng.choice(tuple(range(2, config.floors + 1)))
            elif mode == "peak_down":
                origin, destination = rng.choice(tuple(range(2, config.floors + 1))), 1
            elif mode == "lunch":
                origin = 1 if rng.random() < 0.5 else rng.choice(tuple(range(2, config.floors + 1)))
                destination = 1 if origin != 1 and rng.random() < 0.5 else rng.choice(
                    tuple(f for f in range(1, config.floors + 1) if f != origin))
            else:
                origin, destination = rng.sample(range(1, config.floors + 1), 2)
            arrivals.append(Passenger(pid, at, origin, destination, "up" if destination > origin else "down"))
            pid += 1
    return PassengerDemand(tuple(sorted(arrivals, key=lambda p: p.arrive_at)), start_s + minutes * 60, mode)


def run_passenger_sim(config: ElevatorConfig, demand: PassengerDemand, strategy: PassengerStrategy) -> PassengerResult:
    """跑一段人流曲线。分区策略：低区梯只服务低区楼层、高区梯只服务高区；固定策略：任意梯服务任意层。"""
    env = simpy.Environment()
    cars = [_Car(spec=spec) for spec in config.passenger_cars]
    if strategy.name == "zone":
        _assign_zones(cars, config)
    if strategy.active_cars is not None:
        for index, car in enumerate(cars):
            car.active = index < strategy.active_cars
    waiting: list[Passenger] = []
    waits: dict[int, float] = {}
    queue_samples: list[int] = []
    return_floor = strategy.return_floor

    def arrive(passenger: Passenger):
        yield env.timeout(max(0.0, passenger.arrive_at - env.now))
        waiting.append(passenger)

    for passenger in demand.arrivals:
        env.process(arrive(passenger))

    def monitor():
        while True:
            yield env.timeout(60.0)
            queue_samples.append(len(waiting))

    def serve(car: _Car, batch: list[Passenger]):
        origin = batch[0].origin
        travel = abs(car.floor - origin) * config.per_floor_s
        if travel:
            yield env.timeout(travel)
        car.floor = origin
        yield env.timeout(config.door_s + config.board_s * len(batch))
        boarded_at = env.now
        for passenger in batch:
            waits[passenger.passenger_id] = boarded_at - passenger.arrive_at
        current, destinations = origin, sorted({p.destination for p in batch})
        for destination in destinations:
            if destination == current:
                continue
            yield env.timeout(abs(destination - current) * config.per_floor_s + config.door_s)
            current = destination
        car.floor = current
        distance = abs(current - origin)
        if return_floor is not None and car.floor != return_floor:
            distance += abs(return_floor - car.floor)
            yield env.timeout(abs(return_floor - car.floor) * config.per_floor_s)
            car.floor = return_floor
        car.trips += 1
        car.energy_kwh += config.trip_kwh * min(1.0, distance / max(2 * (config.floors - 1), 1))

    def dispatch():
        while True:
            for car in cars:
                if not car.active or car.busy:
                    continue
                batch = _pick_batch(car, waiting, strategy, config)
                if not batch:
                    continue
                ids = {p.passenger_id for p in batch}
                waiting[:] = [p for p in waiting if p.passenger_id not in ids]
                car.busy = True
                env.process(_finally_free(env, car, serve(car, batch)))
            yield env.timeout(config.batch_s)

    env.process(monitor())
    env.process(dispatch())
    env.run(until=demand.horizon_s)

    served = len(waits)
    waited = list(waits.values()) + [demand.horizon_s - p.arrive_at for p in demand.arrivals if p.passenger_id not in waits]
    sorted_waits = sorted(waited)
    p95 = sorted_waits[min(len(sorted_waits) - 1, int(len(sorted_waits) * 0.95))] if sorted_waits else 0.0
    threshold = 20
    active = sum(1 for car in cars if car.active)
    trip_kwh = sum(car.energy_kwh for car in cars)
    standby_kwh = config.standby_kw * demand.horizon_s / 3600 * active
    return PassengerResult(
        strategy=strategy.name, mode=strategy.mode, arrivals=len(demand.arrivals), served=served,
        avg_wait_s=round(mean(waited), 1) if waited else 0.0, max_wait_s=round(max(waited), 1) if waited else 0.0,
        p95_wait_s=round(p95, 1), congestion_min=float(sum(1 for q in queue_samples if q >= threshold)),
        avg_queue=round(mean(queue_samples), 1) if queue_samples else 0.0,
        energy_kwh=round(trip_kwh + standby_kwh, 2), trips=sum(car.trips for car in cars),
        unserved=len(demand.arrivals) - served, trip_kwh=round(trip_kwh, 2), standby_kwh=round(standby_kwh, 2))


def _finally_free(env: simpy.Environment, car: _Car, process):
    """serve 结束后把轿厢标回空闲；用包装进程而不是在 serve 里改，避免异常时留下永久忙的梯。"""
    yield env.process(process)
    car.busy = False


def _assign_zones(cars: list[_Car], config: ElevatorConfig) -> None:
    """按需求与往返时间分车：高区层数多、单趟时间长，多分轿厢；至少给每个区留 1 台。"""
    total = len(cars)
    low_span = max(config.low_floors) - 1
    high_span = max(config.high_floors) - 1
    low_rtt = 2 * low_span * config.per_floor_s + 3 * config.door_s
    high_rtt = 2 * high_span * config.per_floor_s + 4 * config.door_s
    low_weight = len(config.low_floors) * low_rtt
    high_weight = len(config.high_floors) * high_rtt
    high_count = max(1, min(total - 1, round(total * high_weight / (low_weight + high_weight))))
    for index, car in enumerate(cars):
        car.zone = "low" if index < total - high_count else "high"


def _eligible(passenger: Passenger, car: _Car, strategy: PassengerStrategy, config: ElevatorConfig) -> bool:
    if strategy.name != "zone" or car.zone is None:
        return True
    floors = config.zone_floors(car.zone)
    if passenger.direction == "up":  # 上行只看目的层：首层是所有分区的共同起点
        return passenger.destination in floors
    if passenger.direction == "down":
        return passenger.origin in floors
    return passenger.origin in floors and passenger.destination in floors


def _pick_batch(car: _Car, waiting: list[Passenger], strategy: PassengerStrategy, config: ElevatorConfig) -> list[Passenger]:
    candidates = [p for p in waiting if _eligible(p, car, strategy, config)]
    if not candidates:
        return []
    by_origin: dict[int, list[Passenger]] = {}
    for passenger in candidates:
        by_origin.setdefault(passenger.origin, []).append(passenger)
    if strategy.mode == "peak_up" and 1 in by_origin:
        origin = 1
    elif car.floor in by_origin:
        origin = car.floor
    else:
        origin = min(by_origin, key=lambda floor: min(p.arrive_at for p in by_origin[floor]))
    batch = sorted(by_origin[origin], key=lambda p: p.arrive_at)[:car.spec.capacity]
    return batch


# ---------------------------------------------------------------- 货梯


@dataclass(frozen=True)
class FreightTask:
    task_id: str
    release_s: float
    origin: int = 1
    destination: int = 2
    urgent: bool = False


@dataclass(frozen=True)
class FreightStrategy:
    name: str  # fifo / priority
    avoid_passenger_peak: bool = False
    batch_at_peak: bool = False


@dataclass(frozen=True)
class FreightResult:
    strategy: str
    tasks: int
    done: int
    avg_wait_min: float
    max_wait_min: float
    urgent_avg_wait_min: float
    deferred: int
    batches: int
    energy_kwh: float


@dataclass
class _FreightCar:
    spec: CarSpec
    floor: int = 1
    busy: bool = False
    trips: int = 0
    energy_kwh: float = 0.0


def run_freight_sim(config: ElevatorConfig, tasks: tuple[FreightTask, ...], strategy: FreightStrategy, *,
                    passenger_peak_windows: tuple[tuple[float, float], ...] = (),
                    peak_price_windows: tuple[tuple[float, float], ...] = (),
                    horizon_s: float | None = None) -> FreightResult:
    """货梯排队。priority：紧急任务先走，非紧急任务在人梯高峰结束后再发，峰电时段合并批次。"""
    horizon = horizon_s if horizon_s is not None else max((t.release_s for t in tasks), default=0.0) + 3600
    env = simpy.Environment()
    cars = [_FreightCar(spec=spec) for spec in config.freight_cars]
    waiting: list[FreightTask] = []
    waits: dict[str, float] = {}
    urgent_waits: list[float] = []
    deferred = 0
    batches = 0

    def in_window(t: float, windows: tuple[tuple[float, float], ...]) -> bool:
        return any(start <= t < end for start, end in windows)

    def arrive(task: FreightTask):
        release = task.release_s
        if strategy.avoid_passenger_peak and not task.urgent and in_window(release, passenger_peak_windows):
            release = max(end for start, end in passenger_peak_windows if start <= release < end)
        yield env.timeout(max(0.0, release - env.now))
        waiting.append(task)

    for task in tasks:
        env.process(arrive(task))

    def load(car: _FreightCar, batch: list[FreightTask]):
        origin = min(task.origin for task in batch)
        travel = abs(car.floor - origin) * config.per_floor_s
        if travel:
            yield env.timeout(travel)
        car.floor = origin
        yield env.timeout(config.door_s + 4.0 * len(batch))
        boarded_at = env.now
        for task in batch:
            waits[task.task_id] = boarded_at - task.release_s
            if task.urgent:
                urgent_waits.append(waits[task.task_id])
        destination = max(task.destination for task in batch)
        yield env.timeout(abs(destination - origin) * config.per_floor_s + config.door_s)
        car.floor = destination
        car.trips += 1
        car.energy_kwh += config.freight_trip_kwh  # 一趟带多件只算一次往返

    def dispatch():
        nonlocal batches
        while True:
            for car in cars:
                if car.busy or not waiting:
                    continue
                peak = in_window(env.now, peak_price_windows)
                size = config.freight_capacity if strategy.batch_at_peak and peak else 1
                ordered = sorted(waiting, key=lambda t: (-int(strategy.name == "priority" and t.urgent), t.release_s))
                batch = ordered[:size]
                if strategy.batch_at_peak and peak:
                    batches += 1
                ids = {t.task_id for t in batch}
                waiting[:] = [t for t in waiting if t.task_id not in ids]
                car.busy = True
                env.process(_free_freight(env, car, load(car, batch)))
            yield env.timeout(config.batch_s)

    for task in tasks:
        if strategy.avoid_passenger_peak and not task.urgent and in_window(task.release_s, passenger_peak_windows):
            deferred += 1
    env.process(dispatch())
    env.run(until=horizon)

    values = list(waits.values())
    return FreightResult(
        strategy=strategy.name, tasks=len(tasks), done=len(values),
        avg_wait_min=round(mean(values) / 60, 2) if values else 0.0, max_wait_min=round(max(values) / 60, 2) if values else 0.0,
        urgent_avg_wait_min=round(mean(urgent_waits) / 60, 2) if urgent_waits else 0.0,
        deferred=deferred if strategy.avoid_passenger_peak else 0, batches=batches,
        energy_kwh=round(sum(car.energy_kwh for car in cars) + config.standby_kw * horizon / 3600 * len(cars), 2))


def _free_freight(env: simpy.Environment, car: _FreightCar, process):
    yield env.process(process)
    car.busy = False
