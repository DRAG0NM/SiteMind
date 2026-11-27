CREATE TABLE version_manifests (
    plant_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, manifest_id)
);
CREATE TABLE version_active (
    plant_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL
);
CREATE TABLE version_events (
    seq INTEGER PRIMARY KEY,
    plant_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT,
    previous_active_id TEXT,
    occurred_at TEXT NOT NULL
);
CREATE TABLE runs (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    seed INTEGER NOT NULL,
    inputs TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id)
);
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY,
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_id TEXT,
    version_manifest_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (plant_id, run_id, seq)
);
CREATE TABLE evidence_events (
    seq INTEGER PRIMARY KEY,
    plant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    run_id TEXT,
    agent_run_id TEXT,
    trace_id TEXT NOT NULL,
    action_id TEXT,
    record TEXT NOT NULL,
    UNIQUE (plant_id, event_id)
);
CREATE INDEX evidence_events_run ON evidence_events (plant_id, run_id, seq);
CREATE INDEX evidence_events_agent_run ON evidence_events (plant_id, agent_run_id, seq);
CREATE TABLE jobs (
    plant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, job_id),
    UNIQUE (plant_id, idempotency_key)
);
CREATE TABLE pending_actions (
    plant_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (plant_id, action_id),
    UNIQUE (plant_id, idempotency_key)
);
CREATE TABLE action_decisions (
    plant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    PRIMARY KEY (plant_id, idempotency_key)
);
