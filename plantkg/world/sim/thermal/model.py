"""区域温度一阶 RC 模型（B21）：参数、舒适区间与一分钟推进。

每区按集总热平衡推进：C·dT/dt = UA·(T_out − T) + Q_gain − Q_cool。
Q_cool 由比例控制给出：前馈项 = 维持当前温度所需冷量的 FEEDFORWARD 倍（打折，模拟负荷估计不准），
反馈项按设定温度的偏差修正（比例带 PROPORTIONAL_BAND_C），所以普通区域温度在设定值上下有随负荷变化的偏差；
硬约束区域（精密空调）前馈给满，温度贴住设定值。冷量上限由设备的额定电功率扣掉固定电耗后乘 COP 得出，
所以电功率合计不会超过额定；停机或故障的设备不计入可用容量。
电功率 = Q_cool / COP，写回同区供冷设备的分钟功率（B17 账本据此汇总）。

参数从 configs/hvac/thermal.yaml 读，标 configured；温度与功率轨迹标 simulated。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = ROOT / "configs" / "hvac" / "thermal.yaml"
COOLING_TYPES = {"ahu", "chiller", "crac"}  # 与 B17 的供冷设备集合一致；排风机只做通风，不供冷
HVAC_TYPES = COOLING_TYPES | {"exhaust_fan"}
PROPORTIONAL_BAND_C = 2.0  # 比例带：温度每高出设定 1 ℃，容量多用 50%
FEEDFORWARD = 0.5  # 前馈只给 50% 需冷量，稳态偏差 = 比例带 × 50% × 负荷率（满负荷时高出设定约 1 ℃）
RH_TAU_MIN = 30.0
RH_WHEN_OFF = 70.0

_ZONE_KEYS = {"label", "setpoint_c", "comfort_c", "hard", "c_kj_per_k", "ua_kw_per_k", "cop", "base_ratio", "people_kw",
              "equipment_heat_ratio", "rh_c", "rh_band", "cooling_hours"}


@dataclass(frozen=True)
class ZoneSpec:
    """一个区域的温度模型参数与舒适要求。cooling_hours 为当地钟面（含起、不含止），None 表示全天供冷。"""

    zone: str
    label: str
    setpoint_c: float
    comfort_c: tuple[float, float]
    hard: bool
    c_kj_per_k: float
    ua_kw_per_k: float
    cop: float = 3.2
    base_ratio: float = 0.0  # 风机、新风与除湿的固定电耗占供冷额定的比例，停机时归零
    people_kw: float = 0.1
    equipment_heat_ratio: float = 0.1
    rh_c: float = 50.0
    rh_band: tuple[float, float] = (30.0, 70.0)
    cooling_hours: tuple[time, time] | None = None

    def in_cooling_hours(self, local: datetime) -> bool:
        if self.cooling_hours is None:
            return True
        start, end = self.cooling_hours
        return start <= local.time() < end

    def in_comfort(self, temperature: float) -> bool:
        """0.1 ℃ 分辨率判越界：模型温度是连续量，不按 0.01 ℃ 记一次越界。"""
        value = round(temperature, 1)
        return self.comfort_c[0] <= value <= self.comfort_c[1]

    def in_humidity_band(self, humidity: float) -> bool:
        value = round(humidity, 1)
        return self.rh_band[0] <= value <= self.rh_band[1]


def load_zones(path: str | Path | None = None) -> dict[str, ZoneSpec]:
    """读区域参数；未知键或缺失键直接报错，避免参数写错却静默用默认值。"""
    data = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8")) or {}
    zones = data.get("zones")
    if not isinstance(zones, dict) or not zones:
        raise ValueError("thermal.yaml 缺少 zones")
    out = {}
    for name, item in zones.items():
        if not isinstance(item, dict) or set(item) != _ZONE_KEYS:
            raise ValueError(f"区域 {name} 的键须恰好为 {sorted(_ZONE_KEYS)}，实际 {sorted(item or ())}")
        lo, hi = item["comfort_c"]
        rh_lo, rh_hi = item["rh_band"]
        if lo >= hi or rh_lo >= rh_hi:
            raise ValueError(f"区域 {name} 的舒适区间须为 [低, 高]")
        hours = item["cooling_hours"]
        out[name] = ZoneSpec(
            zone=name, label=item["label"], setpoint_c=float(item["setpoint_c"]), comfort_c=(float(lo), float(hi)),
            hard=bool(item["hard"]), c_kj_per_k=float(item["c_kj_per_k"]), ua_kw_per_k=float(item["ua_kw_per_k"]),
            cop=float(item["cop"]), base_ratio=float(item["base_ratio"]), people_kw=float(item["people_kw"]),
            equipment_heat_ratio=float(item["equipment_heat_ratio"]),
            rh_c=float(item["rh_c"]), rh_band=(float(rh_lo), float(rh_hi)),
            cooling_hours=None if hours is None else (_clock(hours[0]), _clock(hours[1])))
    return out


def _clock(text: str) -> time:
    hour, minute = str(text).split(":")
    return time(int(hour), int(minute))


def control(temperature, setpoint_c, outdoor_c, gain_kw, rated_kw, ua_kw_per_k, cop, c_kj_per_k, base_ratio, *, allowed, secs=60.0,
            feedforward=FEEDFORWARD):
    """一步热平衡：参数接受标量或同形数组，返回（新温度 ℃，供冷电功率 kW，容量占比）。

    feedforward 为前馈系数（标量或同形数组）。allowed=False 表示停机或非供冷时段，供冷功率为 0，温度按热平衡自由漂移。
    rated_kw 是供冷设备的额定电功率；固定电耗（风机、新风、除湿）先占掉 base_ratio，剩下的才给压缩机：
    制冷冷量上限 = (1 − base_ratio) × rated_kw × COP，电功率 = 固定电耗 + 制冷用电，合计不超过额定。
    """
    temperature = np.asarray(temperature, dtype=float)
    rated = np.asarray(rated_kw, dtype=float)
    base = base_ratio * rated
    available = np.maximum(rated - base, 0.0)  # 留给压缩机的电功率
    q_max = available * cop  # 该区能提供的冷量
    need = ua_kw_per_k * (outdoor_c - temperature) + gain_kw  # 维持当前温度所需的冷量 kW
    ratio = np.where(q_max > 0, need / np.where(q_max > 0, q_max, 1.0), 0.0)
    modulation = np.clip(feedforward * ratio + (temperature - setpoint_c) / PROPORTIONAL_BAND_C, 0.0, 1.0)
    modulation = np.where(allowed, modulation, 0.0)
    new_temperature = temperature + (need - modulation * q_max) * secs / c_kj_per_k
    electric = np.where(allowed, base + modulation * available, 0.0)
    return new_temperature, electric, modulation
