CREATE TABLE IF NOT EXISTS b32_sensor_artifacts (
    plant_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (plant_id, digest)
);
