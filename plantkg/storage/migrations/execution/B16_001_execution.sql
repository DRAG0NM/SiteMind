CREATE TABLE b16_executions (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    seq_before INTEGER NOT NULL,
    seq_after INTEGER NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    visible_after TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    result TEXT NOT NULL,
    projection TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, idempotency_key),
    UNIQUE (plant_id, run_id, seq_before),
    UNIQUE (plant_id, run_id, seq_after),
    UNIQUE (plant_id, run_id, slot_start)
);
