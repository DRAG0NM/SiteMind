CREATE TABLE b09_solve_receipts (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, plan_id)
);
