"""B19 场景预测：把 B17/B18 的分钟输入按 B14 契约汇总成 15 分钟日前预测（persistence，derived）。

场景世界没有 B03 时序库，B14 的 `ForecastService` 需要持久化的实测与参数清单；这里用同一份 B18 装配
（D05 × 倍数 + 作息合成）与 PVGIS 插值得到 15 分钟负荷与光伏，作为“场景内的预测产物”：它满足 B09
要求的 Forecast 契约（步长、覆盖、单位、发布时刻、上下界），性质标 derived，来源写进 params。
B14 正式服务接上后把 `build_forecasts` 换成 `ForecastService.predict_and_save` 即可，本模块不碰 B14。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from plantkg.core.contracts import Context, Forecast, ForecastBundle, InputSlice, Interval

MODEL_VERSION = "b19-scenario-persistence-1"
STEP_MINUTES = 15
BAND = 0.08  # 预测区间上下界：场景内按 ±8% 给出，不冒充校准结果


def _slots(horizon: Interval, step_minutes: int) -> list[Interval]:
    step = timedelta(minutes=step_minutes)
    count = int((horizon.end - horizon.start) / step)
    if count < 1:
        raise ValueError(f"预测区间 {horizon.start.isoformat()}～{horizon.end.isoformat()} 不足一个 15 分钟时段")
    return [Interval(start=horizon.start + i * step, end=horizon.start + (i + 1) * step) for i in range(count)]


def _forecast(quantity: str, values: list[float], horizon: Interval, context: Context, provider_id: str) -> Forecast:
    lower = tuple(max(0.0, v * (1 - BAND)) for v in values)
    upper = tuple(v * (1 + BAND) + 1.0 for v in values)
    return Forecast(
        forecast_id=f"{MODEL_VERSION}:{quantity}:{context.run_id}:{horizon.start.isoformat()}",
        quantity=quantity, unit="kW", issued_at=context.as_of, target=horizon, step_minutes=STEP_MINUTES,
        values=tuple(values), lower=lower, upper=upper, model_version=MODEL_VERSION,
        params={"plant_id": context.plant_id, "run_id": context.run_id, "version_manifest_id": context.version_manifest_id,
                "provider": provider_id, "method": "persistence_from_b18_scenario_inputs", "interval_status": "scenario_band",
                "nature": "derived"},
        input_slice=InputSlice(source_ids=(provider_id,), visible_until=context.as_of, window=None,
                               note="场景内由 B18 装配与 PVGIS 插值得到的持久性预测"),
        coverage=1.0)


def build_forecasts(provider, horizon: Interval, context: Context, *,
                    extra_load_kw: Callable[[datetime], float] | None = None) -> ForecastBundle:
    """由 `DemoScenarioInputProvider.slot_profile` 汇总出 base_load 与 pv_available 两条预测。

    `extra_load_kw` 把仍在生效的事件附加负荷（如 08:05 的突增）纳入重解预测，让 15 分钟层看见它；
    日前计划不传，保持“事件前按剧本正常负荷规划”。
    """
    base, pv = [], []
    for slot in _slots(horizon, STEP_MINUTES):
        profile = provider.slot_profile(slot.start)
        base.append(profile.load_kw + (extra_load_kw(slot.start) if extra_load_kw else 0.0))
        pv.append(profile.pv_kw)
    return ForecastBundle(forecasts=(_forecast("base_load", base, horizon, context, provider.provider_id),
                                     _forecast("pv_available", pv, horizon, context, provider.provider_id)),
                          uncertainty_summary={"band": BAND, "calibrated": 0.0})
