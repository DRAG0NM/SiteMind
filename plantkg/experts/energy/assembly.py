"""B19 演示装配：把场景库、门控、求解回执与计划账本接到同一份 SQLite 上。

dev/demo 用，只供 run_day：门控材料提供者、回执核验器与执行依据提供者是演示替身（数据 synthetic），只装在这里，
bootstrap 的默认装配不带，prod 也不装。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import Actor, Role
from plantkg.core.registry import Registry
from plantkg.decision.gate.execution_basis import StoredExecutionBasisProvider
from plantkg.decision.gate.service import GateService
from plantkg.decision.planner.receipt import SolveReceiptStore
from plantkg.experts.energy.ledger import PlanLedgerStore
from plantkg.experts.energy.materials import DemoConfigResolver, DemoMaterialsProvider, demo_source_verifier, verify_receipt
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.scenario.runner import ScenarioDB

DEFAULT_DB = Path(__file__).resolve().parents[3] / "runtime" / "energy_expert.db"
DEMO_REVIEWER = Actor(actor_id="b19-demo-reviewer", roles=(Role.reviewer, Role.operator))


@dataclass
class DemoAssembly:
    db: ScenarioDB
    registry: Registry
    actions: SqlitePendingActionStore
    gate: GateService
    receipts: SolveReceiptStore
    ledgers: PlanLedgerStore
    plant_id: str

    def close(self) -> None:
        self.db.close()


def open_scenario_db(db_path, plant_id: str) -> ScenarioDB:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = SQLiteWriter(path)
    MigrationRunner(writer).apply()
    return ScenarioDB.from_writer(writer, plant_id)


def build_demo_assembly(db_path=DEFAULT_DB, plant_id: str = "demo-electronics", *, ttl_s: int = 4 * 3600) -> DemoAssembly:
    """场景库 + B15 门控 + 回执 + 计划账本。回执核验器与来源核验器是演示替身（数据 synthetic）。"""
    db = open_scenario_db(db_path, plant_id)
    registry = Registry()
    actions = SqlitePendingActionStore(db.writer, plant_id, registry.action_handlers, ttl_s=ttl_s)
    receipts = SolveReceiptStore(db.writer, db.runs, db.versions, verifier=verify_receipt)
    basis = StoredExecutionBasisProvider(receipts, DemoConfigResolver(), demo_source_verifier)
    gate = GateService(db.writer, plant_id, actions, db.runs, db.versions, materials_provider=DemoMaterialsProvider(),
                       execution_basis_provider=basis,
                       run_clock=lambda run_id: db.runs.get_run(run_id).as_of)
    registry.register_action_handler("plan_confirmation", gate)
    return DemoAssembly(db=db, registry=registry, actions=actions, gate=gate, receipts=receipts,
                        ledgers=PlanLedgerStore(db.writer, plant_id), plant_id=plant_id)
