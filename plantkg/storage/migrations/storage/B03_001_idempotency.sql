CREATE TABLE storage_idempotency (
    plant_id TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (plant_id, key)
);
