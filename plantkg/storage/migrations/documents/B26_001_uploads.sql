CREATE TABLE b26_uploads (
    plant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    document_id TEXT NOT NULL,
    asset_version_id TEXT NOT NULL,
    expert TEXT NOT NULL,
    source_level TEXT NOT NULL,
    available_at TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (plant_id, idempotency_key)
);
