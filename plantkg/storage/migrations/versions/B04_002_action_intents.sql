CREATE TABLE action_intents (
    plant_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, action_id)
);
