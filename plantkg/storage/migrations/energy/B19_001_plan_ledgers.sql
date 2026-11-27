-- B19 能源专家：计划账本（B08 口径）按运行、清单与计划存，供 ledger 工具的 kind=plan 查询。
CREATE TABLE b19_plan_ledgers (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ledger_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, plan_id)
);
CREATE INDEX b19_plan_ledgers_by_run ON b19_plan_ledgers (plant_id, run_id, manifest_id, plan_id);
