CREATE TABLE storage_artifacts (
    plant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    reference TEXT NOT NULL,
    checksum TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    content_checksum TEXT,
    PRIMARY KEY (plant_id, kind, name),
    UNIQUE (plant_id, kind, reference)
);
