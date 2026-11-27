CREATE TABLE replay_clock_history (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq >= 0),
    instant TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, seq),
    UNIQUE (plant_id, run_id, instant)
);
