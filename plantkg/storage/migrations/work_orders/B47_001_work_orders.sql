-- B47 分级执行、操作工单、执行幂等、抽查/纠错队列、临时规则与自主等级。
-- 只新增本模块的表；事件单、提案与场景账本仍归 B27/B17 的表。
CREATE TABLE b47_work_orders (
    plant_id TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    kind TEXT NOT NULL,              -- work_order（操作工单）/ human_decision（人工待办）
    status TEXT NOT NULL,            -- proposed / confirmed / in_progress / done / rejected / expired
    playbook_id TEXT,
    actions TEXT NOT NULL,           -- JSON ProposedAction[]
    pending_action_id TEXT,
    confirmed_at TEXT,
    done_at TEXT,
    updated_at TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, work_order_id)
);
CREATE INDEX b47_work_orders_ticket ON b47_work_orders (plant_id, ticket_id, status);

CREATE TABLE b47_executions (
    plant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,               -- (proposal_id, 动作序号, round) 或 (playbook, 预案 ID, 动作序号, round)
    ticket_id TEXT NOT NULL,
    proposal_id TEXT,
    round INTEGER NOT NULL,
    action_index INTEGER NOT NULL,
    entity_id TEXT,
    command TEXT,
    status TEXT NOT NULL,            -- executed / skipped_duplicate / infeasible / queued / proposed / rejected
    at TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (plant_id, run_id, key)
);
CREATE INDEX b47_executions_ticket ON b47_executions (plant_id, ticket_id);

CREATE TABLE b47_queues (
    plant_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- correction / sampling / review
    item_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    run_id TEXT,
    at TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending / correct / wrong / accepted / rejected
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, kind, item_id)
);
CREATE INDEX b47_queues_ticket ON b47_queues (plant_id, ticket_id, kind);

CREATE TABLE b47_temp_rules (
    plant_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    text TEXT NOT NULL,
    entity_ids TEXT NOT NULL,
    estimated_cost_cny REAL,
    source_event_id TEXT,
    status TEXT NOT NULL,            -- active / expired / cancelled
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, rule_id)
);
CREATE INDEX b47_temp_rules_window ON b47_temp_rules (plant_id, start_at, end_at);

CREATE TABLE b47_category_levels (
    plant_id TEXT NOT NULL,
    category TEXT NOT NULL,          -- 事件类型 或 事件类型|设备类型
    level TEXT NOT NULL,             -- L0 / L1 / L2 / L3
    seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plant_id, category)
);

CREATE TABLE b47_counters (
    plant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (plant_id, name)
);
