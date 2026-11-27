CREATE TABLE b32_backtest_baselines (
    plant_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (plant_id, cache_key)
);

CREATE TABLE b32_feedback (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    system_error INTEGER NOT NULL DEFAULT 1,
    ticket_id TEXT,
    note TEXT,
    at TEXT NOT NULL
);

CREATE INDEX b32_feedback_plant_type_seq ON b32_feedback(plant_id, event_type, seq);

CREATE TABLE b32_autonomy_state (
    plant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    pending_level TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plant_id, event_type)
);
