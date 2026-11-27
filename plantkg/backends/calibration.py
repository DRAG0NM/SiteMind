"""按后端与模型隔离的温度校准；产物与概率可序列化，不用测试划分拟合。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from plantkg.core.contracts import FrozenSplit, JudgeResult


def average_distribution(result: JudgeResult) -> dict[str, float]:
    if result.distribution:
        return result.distribution
    if not result.orders:
        raise ValueError("校准需要完整的原始分布")
    return {key: sum(order.distribution[key] for order in result.orders) / len(result.orders)
            for key in result.orders[0].order}


def temperature_distribution(values, temperature):
    logits = {key: math.log(max(value, 1e-15)) / temperature for key, value in values.items()}
    maximum = max(logits.values())
    weights = {key: math.exp(value - maximum) for key, value in logits.items()}
    total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


class TemperatureCalibrator:
    def __init__(self):
        self.artifact = None

    @property
    def version(self):
        return self.artifact["version"] if self.artifact else None

    def fit(self, results: list[JudgeResult], labels: list[str], split: FrozenSplit) -> str:
        if split.purpose != "calibration":
            raise ValueError("只能在独立 calibration 划分拟合")
        if not results or len(results) != len(labels):
            raise ValueError("校准结果与标签数量须相同且非空")
        identities = {(r.backend, r.model) for r in results}
        if len(identities) != 1:
            raise ValueError("校准产物必须按后端与模型隔离")
        distributions = [average_distribution(r) for r in results]
        if any(label not in values for label, values in zip(labels, distributions)):
            raise ValueError("校准真值必须属于候选集")
        candidates = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)
        temperature = min(candidates, key=lambda t: sum(
            -math.log(max(temperature_distribution(values, t)[label], 1e-15))
            for values, label in zip(distributions, labels)))
        backend, model = next(iter(identities))
        artifact = {"backend": backend, "model": model, "temperature": temperature,
                    "split": split.model_dump(mode="json"), "samples": [r.candidate_id for r in results],
                    "sample_hash": hashlib.sha256(json.dumps([distributions, labels], sort_keys=True).encode()).hexdigest()}
        version = "cal-" + hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()[:16]
        self.artifact = artifact | {"version": version}
        return version

    def predict(self, result: JudgeResult) -> float | None:
        if self.artifact is None:
            return None
        if (result.backend, result.model) != (self.artifact["backend"], self.artifact["model"]):
            return None
        return temperature_distribution(average_distribution(result), self.artifact["temperature"])[result.choice]

    def save(self, path: Path) -> None:
        if self.artifact is None:
            raise ValueError("尚未拟合校准产物")
        path.write_text(json.dumps(self.artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        version = artifact.pop("version")
        expected = "cal-" + hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()[:16]
        if expected != version or artifact["split"]["purpose"] != "calibration":
            raise ValueError("校准产物校验失败")
        split = FrozenSplit.model_validate(artifact["split"])
        temperature = artifact["temperature"]
        if split.purpose != "calibration" or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or temperature <= 0:
            raise ValueError("校准产物温度无效")
        instance = cls()
        instance.artifact = artifact | {"version": version}
        return instance
