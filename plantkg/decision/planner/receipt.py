"""Per-call immutable evidence captured from a successful planner invocation.

This is a transport object, not an approval or evidence of trusted sources.
Persisting and validating the referenced source materials remain the caller's
responsibility; in particular, a digest by itself does not establish trust.
"""
from dataclasses import dataclass
from dataclasses import fields, is_dataclass
from collections.abc import Mapping
from datetime import datetime
import hashlib
import json

from pydantic import BaseModel
from plantkg.core.contracts import ErrorCode, Plan, PlantkgError, PlanningRequest
from plantkg.core.versions import atomic, read


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported receipt evidence type {type(value).__name__}")


def pack_evidence(packs):
    """Capture stable public domain-pack inputs consumed by a solve."""
    names = ("config", "parameters", "params_config_id", "operations", "extensions", "grid_entity", "category", "base_prices", "config_ref", "prices")
    return {name: {key: _plain(getattr(pack, key)) for key in names
                   if hasattr(pack, key) and not callable(getattr(pack, key))}
            for name, pack in packs.items()}


@dataclass(frozen=True)
class SolveReceipt:
    canonical_json: str
    sha256: str

    @classmethod
    def create(cls, body):
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(raw, hashlib.sha256(raw.encode("utf-8")).hexdigest())

    def read(self):
        if hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("solve receipt digest mismatch")
        return json.loads(self.canonical_json)


class SolveReceiptStore:
    """Internal persistent receipt store with an explicit trusted verifier.

    The verifier must establish that the receipt came from the actual solve and
    its configuration and source references match the fixed manifest. A hash
    or an arbitrary request argument alone is never a sufficient verifier.
    """
    def __init__(self, writer, runs, versions, *, verifier=None):
        self.writer, self.runs, self.versions, self.verifier = writer, runs, versions, verifier

    def save(self, context, receipt):
        if not context.can_write or not context.run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.NO_CTX, "回执保存需要固定运行的可信上下文")
        if context.plant_id != self.runs.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, "厂区不匹配")
        run = self.runs.get_run(context.run_id)
        if run.version_manifest_id != context.version_manifest_id or context.as_of != run.as_of:
            raise PlantkgError(ErrorCode.PRECONDITION, "回执上下文与固定运行不一致")
        self.versions.usable(context.version_manifest_id)
        try:
            body = receipt.read()
            plan = Plan.model_validate(body["plan"])
            request = PlanningRequest.model_validate(body["request"])
            if (plan.run_id, plan.version_manifest_id, request.context.run_id,
                request.context.version_manifest_id, request.context.plant_id, request.context.as_of) != (
                    run.run_id, run.version_manifest_id, run.run_id, run.version_manifest_id, run.plant_id, run.as_of):
                raise ValueError("identity")
            if (plan.status != "draft" or plan.valid_interval != request.horizon or
                    plan.snapshot_id != request.snapshot_id or plan.forecast_ids != request.forecast_ids or plan.policy != request.policy):
                raise ValueError("plan")
        except (AttributeError, KeyError, ValueError, TypeError) as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, "求解回执无效") from exc
        if self.verifier is None or self.verifier(context, receipt) is not True:
            raise PlantkgError(ErrorCode.PRECONDITION, "求解回执来源未核验")
        def write(con):
            old = con.execute("SELECT sha256,record FROM b09_solve_receipts WHERE plant_id=? AND run_id=? AND plan_id=?",
                              (run.plant_id, run.run_id, plan.plan_id)).fetchone()
            if old:
                if old != (receipt.sha256, receipt.canonical_json):
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划已有不同求解回执")
                return receipt.sha256
            con.execute("INSERT INTO b09_solve_receipts VALUES (?,?,?,?,?,?)", (run.plant_id, run.run_id,
                        run.version_manifest_id, plan.plan_id, receipt.sha256, receipt.canonical_json))
            return receipt.sha256
        return atomic(self.writer, write)

    def load(self, context, plan_id):
        if not context.run_id or not context.version_manifest_id or context.plant_id != self.runs.plant_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "回执读取需要固定运行")
        run = self.runs.get_run(context.run_id)
        if run.version_manifest_id != context.version_manifest_id or context.as_of < run.as_of:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "回执不属于当前可见运行")
        row = read(self.writer, lambda con: con.execute(
            "SELECT sha256,record FROM b09_solve_receipts WHERE plant_id=? AND run_id=? AND manifest_id=? AND plan_id=?",
            (run.plant_id, run.run_id, run.version_manifest_id, plan_id)).fetchone())
        if not row:
            raise PlantkgError(ErrorCode.NOT_FOUND, "求解回执不存在")
        receipt = SolveReceipt(row[1], row[0])
        try:
            receipt.read()
        except ValueError as exc:
            raise PlantkgError(ErrorCode.PRECONDITION, "求解回执完整性校验失败") from exc
        return receipt
