CREATE TABLE b15_plan_heads (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id)
);
CREATE TABLE b15_plan_revisions (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision)
);
CREATE TABLE b15_decisions (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    record TEXT NOT NULL,
    policy_snapshot TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision)
);
CREATE TABLE b15_approvals (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action_id TEXT,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision),
    UNIQUE (plant_id, action_id)
);
CREATE TABLE b15_action_links (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision),
    UNIQUE (plant_id, action_id)
);
CREATE TABLE b15_proposal_intents (
    plant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, plan_id, revision)
);
CREATE TABLE b15_idempotency (
    plant_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (plant_id, namespace, key)
);
