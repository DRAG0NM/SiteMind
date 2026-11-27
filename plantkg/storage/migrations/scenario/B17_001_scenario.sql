-- B17 场景世界：分钟账本与槽汇总分表存；事件、动作、状态帧、分叉快照与业务记录按运行存。
CREATE TABLE b17_ledger_minutes (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    minute TEXT NOT NULL,
    balanced INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, minute)
);

CREATE TABLE b17_slot_summaries (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    summary TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, slot)
);

CREATE TABLE b17_frames (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    frame TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, at)
);

CREATE TABLE b17_snapshots (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, at)
);

-- kind：event / action / ticket / work_order / temp_rule / agent_run / knowledge_version / history
CREATE TABLE b17_records (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    at TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, run_id, kind, record_id)
);
CREATE INDEX b17_records_by_time ON b17_records (plant_id, run_id, kind, at);
