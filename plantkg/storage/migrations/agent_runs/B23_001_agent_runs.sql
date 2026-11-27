CREATE TABLE b23_agent_runs (
    plant_id TEXT NOT NULL,
    agent_run_id TEXT NOT NULL,
    run_id TEXT,
    ticket_id TEXT,
    kind TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, agent_run_id)
);
CREATE INDEX b23_agent_runs_ticket ON b23_agent_runs (plant_id, ticket_id, requested_at);
CREATE INDEX b23_agent_runs_run ON b23_agent_runs (plant_id, run_id, requested_at);
