"""Conservative default material provider for forecasts persisted by B14."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from plantkg.core.contracts import ErrorCode, Plan, PlanningRequest, PlantkgError
from plantkg.decision.forecasting.service import ForecastService
from .b14 import load_b14_forecasts
from .policy import PolicyMaterials


class B14MaterialsProvider:
    """Resolve a registered planning request and B14 artifact, without invented evidence.

    B19 must record ``planning_request`` as the original PlanningRequest JSON
    object and ``forecast_key`` as the B14 artifact key in registration metadata.
    Neither is an MCP tool argument. Missing registrations return incomplete
    materials, so policy requires review and blocks human approval. A malformed
    registration or a contradictory artifact fails closed with a typed error.

    The current repository does not expose a public provenance-complete price,
    rule dependency, calibration or baseline provider. Consequently this class
    does not assert any of those: production approval remains blocked until a
    trusted provider supplies them after actual verification.
    """
    def __init__(self, forecast_service: ForecastService):
        self.forecast_service = forecast_service

    def __call__(self, plan: Plan, metadata: Mapping[str, Any]) -> PolicyMaterials:
        raw_request = metadata.get("planning_request")
        if raw_request is None:
            return PolicyMaterials()
        try:
            request = PlanningRequest.model_validate(raw_request)
        except (ValueError, TypeError, ValidationError) as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, "已登记的规划请求无效") from exc
        key = metadata.get("forecast_key")
        if key is None:
            return PolicyMaterials(request=request)
        if not isinstance(key, str) or not key:
            raise PlantkgError(ErrorCode.PRECONDITION, "已登记的预测产物键无效")
        try:
            forecasts = load_b14_forecasts(self.forecast_service, request.context, plan, key)
        except FileNotFoundError as exc:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "已登记的 B14 预测产物不存在") from exc
        except ValueError as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, "B14 预测产物与固定运行或计划不匹配") from exc
        return PolicyMaterials(request=request, forecasts=forecasts)
