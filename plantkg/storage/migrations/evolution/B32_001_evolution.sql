CREATE TABLE b32_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id TEXT NOT NULL,
    ticket TEXT NOT NULL,
    body TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE b32_candidates (
    plant_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (plant_id, candidate_id)
);

CREATE TABLE b32_reports (
    plant_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (plant_id, report_id)
);

CREATE TABLE b32_timeline (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id TEXT NOT NULL,
    event_key TEXT,
    body TEXT NOT NULL
);

CREATE INDEX b32_timeline_plant_seq ON b32_timeline(plant_id, seq);
CREATE UNIQUE INDEX b32_timeline_plant_event ON b32_timeline(plant_id, event_key);
