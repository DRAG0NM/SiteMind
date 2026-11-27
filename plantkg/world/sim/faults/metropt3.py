"""MetroPT-3 小样本读取与同期基线（B22）。

MetroPT-3 是地铁空压机（APU）的实测数据（UCI 791，10 秒采样，CC BY 4.0），不是本厂设备。
本模块只读仓库里的两个小窗口：

- 泄漏：2020-05-29 23:30～05-30 06:00（官方第 2 次空气泄漏）；
- 对照：2020-05-27 23:30～05-28 06:00（前两晚同一时段，夜间对夜间）。

官方统计：运转占比（电机电流 > 2 A 的时间占比）约 23% → 100%，电机电流均值约 1.0 → 5.6 A，
油温约 65 → 76 ℃。07-15 泄漏前几小时运转占比已近 100%，不作对照。
白天正常运转占比约 50～70%，所以研判必须与同时段基线比，不能拿夜间基线判白天：
`baseline_for(hour)` 白天返回配置的日间基线（configured），夜间返回对照窗口的统计（measured）。

窗口文件的第一行是 `# nature=...; source=...` 注释；`scripts/data/fetch_metropt3.py` 跑完会把
synthetic 小样本换成 measured 真实切片，列名与读取逻辑不变。
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.contracts import TransformStep

ROOT = Path(__file__).resolve().parents[4]
WINDOW_DIR = ROOT / "fixtures" / "equipment" / "metropt3"
TRANSFORM_VERSION = "b22-metropt3-1"
RUN_THRESHOLD_A = 2.0  # 电机电流 > 2 A 判为运转（与官方统计口径一致）
NIGHT_WINDOW = "23:30～06:00"
DAY_WINDOW = "08:00～19:00"
# 白天正常运转占比 50～70%（configured，按 MetroPT-3 公开统计取中值）；不用夜间基线判白天。
DAY_BASELINE: dict = {"window": DAY_WINDOW, "nature": "configured", "running_ratio": 0.6, "current_mean_a": 3.4,
                      "oil_mean_c": 70.0, "pressure_drop_bar_per_h": 0.0, "samples": 0}


@dataclass(frozen=True)
class Sample:
    at: datetime
    current_a: float
    oil_c: float
    pressure_bar: float
    on: bool

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "current_a": round(self.current_a, 3), "oil_c": round(self.oil_c, 2),
                "pressure_bar": round(self.pressure_bar, 3), "on": self.on}


@dataclass(frozen=True)
class WindowStats:
    samples: int
    minutes: float
    running_ratio: float
    current_mean_a: float
    oil_mean_c: float
    oil_start_c: float
    oil_end_c: float
    pressure_start_bar: float
    pressure_end_bar: float
    pressure_drop_bar_per_h: float
    window: str = ""
    nature: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("window", "nature", "samples", "minutes", "running_ratio", "current_mean_a",
                                             "oil_mean_c", "oil_start_c", "oil_end_c", "pressure_start_bar", "pressure_end_bar",
                                             "pressure_drop_bar_per_h")}


@dataclass(frozen=True)
class Window:
    name: str
    path: Path
    samples: tuple[Sample, ...]
    stats: WindowStats
    nature: str
    source: str = "MetroPT-3 (UCI 791, CC BY 4.0)"

    def reading_state(self, *, limit: int | None = None) -> dict:
        """给 EquipmentExpert 的 readings 结构。"""
        samples = self.samples[-limit:] if limit else self.samples
        return {"window": f"{self.samples[0].at.isoformat()}～{self.samples[-1].at.isoformat()}", "sampling_s": 10,
                "nature": self.nature, "samples": [s.to_dict() for s in samples]}


def summarize(samples: list[Sample] | tuple[Sample, ...], *, window: str = "", nature: str = "", sampling_s: float = 10.0) -> WindowStats:
    n = len(samples)
    if n < 2:
        raise ValueError("至少两个采样点才能算基线与斜率")
    ratio = sum(1 for s in samples if s.on) / n
    current = sum(s.current_a for s in samples) / n
    oil = sum(s.oil_c for s in samples) / n
    t0 = samples[0].at
    xs = [(s.at - t0).total_seconds() / 3600 for s in samples]
    ys = [s.pressure_bar for s in samples]
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return WindowStats(samples=n, minutes=round(n * sampling_s / 60, 1), running_ratio=round(ratio, 4), current_mean_a=round(current, 3),
                       oil_mean_c=round(oil, 2), oil_start_c=round(samples[0].oil_c, 2), oil_end_c=round(samples[-1].oil_c, 2),
                       pressure_start_bar=round(samples[0].pressure_bar, 3), pressure_end_bar=round(samples[-1].pressure_bar, 3),
                       pressure_drop_bar_per_h=round(slope, 4), window=window, nature=nature)


def _comment(path: Path) -> dict:
    """读首行 `# key=value; key2=value2` 注释；没有注释返回空字典。"""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        first = f.readline()
    if not first.startswith("#"):
        return out
    for part in first[1:].split(";"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def load_window(path: str | Path, *, name: str | None = None) -> Window:
    path = Path(path)
    meta = _comment(path)
    samples: list[Sample] = []
    with path.open(encoding="utf-8", newline="") as f:
        lines = (line for line in f if not line.startswith("#"))
        for row in csv.DictReader(lines):
            at = datetime.fromisoformat(row["timestamp"].strip())
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            samples.append(Sample(at=at.astimezone(timezone.utc), current_a=float(row["motor_current"]), oil_c=float(row["oil_temperature"]),
                                  pressure_bar=float(row["reservoir_pressure_bar"]), on=bool(int(row["compressor_on"]))))
    if not samples:
        raise ValueError(f"{path} 没有数据行")
    window = name or meta.get("window") or path.stem
    stats = summarize(samples, window=window, nature=meta.get("nature", "synthetic"))
    return Window(name=window, path=path, samples=tuple(samples), stats=stats, nature=meta.get("nature", "synthetic"))


def baseline_for(hour: int) -> dict:
    """按厂区当地小时取同期基线：白天用 configured 日间基线，夜间用对照窗口的实测统计。"""
    if 8 <= hour < 19:
        return dict(DAY_BASELINE)
    return dict(load_calibration().baseline.stats.to_dict())


@dataclass(frozen=True)
class Calibration:
    baseline: Window
    leak: Window
    manifest: dict = field(default_factory=dict)

    def baseline_for(self, hour: int) -> dict:
        return dict(DAY_BASELINE) if 8 <= hour < 19 else dict(self.baseline.stats.to_dict())


_CALIBRATION: Calibration | None = None


def load_calibration(directory: str | Path | None = None) -> Calibration:
    global _CALIBRATION
    if directory is None and _CALIBRATION is not None:
        return _CALIBRATION
    folder = Path(directory) if directory is not None else WINDOW_DIR
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8")) if (folder / "manifest.json").is_file() else {}
    windows = manifest.get("windows", {})
    names = {"baseline": "baseline_2020-05-27_2330_to_2020-05-28_0600.csv",
             "leak": "leak_2020-05-29_2330_to_2020-05-30_0600.csv"}
    files = {k: (folder / (windows.get(k, {}).get("file") or names[k])) for k in names}
    calibration = Calibration(baseline=load_window(files["baseline"], name="夜间对照 23:30～06:00"),
                              leak=load_window(files["leak"], name="泄漏 23:30～06:00"), manifest=manifest)
    if directory is None:
        _CALIBRATION = calibration
    return calibration


def plant_rated_current_a(rated_kw: float, *, voltage_v: float = 380.0, power_factor: float = 0.9, phases: int = 3) -> float:
    return rated_kw * 1000 / (math.sqrt(phases) * voltage_v * power_factor)


def scale_steps(rated_kw: float, *, source_full_scale_a: float = 9.0, target_system_bar: float = 7.0) -> tuple[TransformStep, ...]:
    """MetroPT-3 信号形状 → 本厂空压机的缩放说明（写进注入记录与提案依据）。"""
    return (
        TransformStep(op="map_signal_shape", params={"source": "MetroPT-3", "source_device": "地铁空压机，非本厂设备",
                                                     "window": "2020-05-29 23:30～05-30 06:00", "sampling_s": 10,
                                                     "baseline_window": "2020-05-27 23:30～05-28 06:00"}, version=TRANSFORM_VERSION,
                      note="只借用泄漏时段的信号形状与统计，不声称是本厂实测"),
        TransformStep(op="scale_current_to_plant_rated", params={"source_full_scale_a": source_full_scale_a, "target_rated_kw": rated_kw,
                                                                 "target_rated_a": round(plant_rated_current_a(rated_kw), 1)}, version=TRANSFORM_VERSION,
                      note="按本厂空压机额定功率折算电流比例；研判用比值与残差，不用绝对电流"),
        TransformStep(op="map_pressure_to_plant_system", params={"source_mean_bar": 8.6, "target_system_bar": target_system_bar},
                      version=TRANSFORM_VERSION, note="压力按本厂管网运行压力平移，斜率保留"),
    )
