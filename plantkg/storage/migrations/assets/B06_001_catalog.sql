CREATE TABLE asset_versions (
    plant_id TEXT NOT NULL,
    asset_version_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, asset_version_id),
    UNIQUE (plant_id, asset_type, content_hash)
);
CREATE TABLE asset_sources (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    asset_version_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    FOREIGN KEY (plant_id, asset_version_id) REFERENCES asset_versions (plant_id, asset_version_id)
);
CREATE INDEX asset_sources_latest ON asset_sources (plant_id, asset_type, source_key, seq);
CREATE TABLE asset_entities (
    plant_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    record TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (plant_id, entity_id)
);
CREATE TABLE asset_entity_links (
    plant_id TEXT NOT NULL,
    asset_version_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    available_at TEXT NOT NULL,
    PRIMARY KEY (plant_id, asset_version_id, entity_id),
    FOREIGN KEY (plant_id, asset_version_id) REFERENCES asset_versions (plant_id, asset_version_id),
    FOREIGN KEY (plant_id, entity_id) REFERENCES asset_entities (plant_id, entity_id)
);
CREATE TABLE asset_activations (
    plant_id TEXT NOT NULL,
    asset_version_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('identified', 'in_graph', 'referenced')),
    ref TEXT NOT NULL,
    available_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (plant_id, asset_version_id, stage, ref),
    FOREIGN KEY (plant_id, asset_version_id) REFERENCES asset_versions (plant_id, asset_version_id)
);
