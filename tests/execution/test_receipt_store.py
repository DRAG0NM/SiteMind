"""Synthetic verifier exercises persistence, never production trust."""
import pytest

from plantkg.core.contracts import PlantkgError
from plantkg.decision.planner.receipt import SolveReceipt, SolveReceiptStore
from tests.gate.test_gate_action_integration import env  # noqa: F401

__all__ = ["env"]


def test_receipt_requires_trusted_source_and_survives_store_reconstruction(request):
    service, _, ctx, plan, _, writer, supplied = request.getfixturevalue("env")
    receipt = SolveReceipt.create({"plan": plan.model_dump(mode="json"),
                                   "request": supplied[0].request.model_dump(mode="json")})
    untrusted = SolveReceiptStore(writer, service.runs, service.versions)
    with pytest.raises(PlantkgError, match="来源未核验"):
        untrusted.save(ctx, receipt)
    trusted = SolveReceiptStore(writer, service.runs, service.versions, verifier=lambda _c, _r: True)
    assert trusted.save(ctx, receipt) == trusted.save(ctx, receipt) == receipt.sha256
    reconstructed = SolveReceiptStore(writer, service.runs, service.versions)
    assert reconstructed.load(ctx, plan.plan_id) == receipt
    altered = SolveReceipt.create(receipt.read() | {"synthetic_change": True})
    with pytest.raises(PlantkgError, match="不同求解回执"):
        trusted.save(ctx, altered)
    from plantkg.core.runs import SqliteRunStore
    from plantkg.core.versions import SqliteVersionStore
    from plantkg.storage.sqlite import SQLiteWriter
    path = writer.path
    writer.close()
    reopened = SQLiteWriter(path)
    request.addfinalizer(reopened.close)
    versions = SqliteVersionStore(reopened, ctx.plant_id)
    runs = SqliteRunStore(reopened, ctx.plant_id, versions)
    assert SolveReceiptStore(reopened, runs, versions).load(ctx, plan.plan_id) == receipt
