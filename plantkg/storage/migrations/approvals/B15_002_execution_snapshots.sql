CREATE TABLE b15_execution_snapshots (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision)
);
