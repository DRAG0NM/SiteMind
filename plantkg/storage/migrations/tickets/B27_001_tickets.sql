-- B27 值守层：事件准入记录、事件单、订阅、派发轮次、研判记录与合批队列。
-- 订阅键为（图谱实体 ID，事件类型集合），多对多：一张事件单可订阅多个实体，一个实体可被多张事件单订阅。
CREATE TABLE b27_tickets (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    run_id TEXT NOT NULL,          -- 当前所属运行；知识发布分叉后改为子运行
    status TEXT NOT NULL,          -- open / waiting / blocked / resolved / closed
    round INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    rehearsal INTEGER NOT NULL,    -- 预演事件单：不执行、不对人说话
    summary TEXT NOT NULL,
    experts TEXT NOT NULL,         -- JSON 数组
    event_ids TEXT NOT NULL,       -- JSON 数组，按并入顺序
    running INTEGER NOT NULL,      -- 专家运行在跑：这期间到的后续事件排到下一次
    PRIMARY KEY (plant_id, ticket_id)
);

CREATE TABLE b27_subscriptions (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_types TEXT NOT NULL,     -- JSON 数组
    active INTEGER NOT NULL,       -- 事件单关闭时退订
    PRIMARY KEY (plant_id, ticket_id, entity_id)
);

CREATE INDEX b27_subscriptions_by_entity ON b27_subscriptions (plant_id, entity_id, active);

-- result：dropped_source / dropped_severity / dropped_whitelist / dropped_duplicate / merged / opened / instruction
CREATE TABLE b27_events (
    plant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_id TEXT,
    severity INTEGER NOT NULL,
    source TEXT NOT NULL,
    result TEXT NOT NULL,
    ticket_id TEXT,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, event_id)
);

-- 一次派发一轮：提案就绪钩子只收当前 round，旧轮提案不拼入新一轮
CREATE TABLE b27_rounds (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    trigger_at TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    experts TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (plant_id, ticket_id, round)
);

-- 研判记录：Jev 派域与门控，不一致才交主 Agent；domains 为本轮检索与采纳权重
CREATE TABLE b27_triage (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    at TEXT NOT NULL,
    gate_accept INTEGER NOT NULL,
    domains TEXT NOT NULL,         -- JSON：领域 → 概率
    source TEXT NOT NULL,          -- jev / main_agent / cache / deterministic
    agent_run_id TEXT,
    coverage_gap INTEGER NOT NULL,
    summary TEXT NOT NULL,
    PRIMARY KEY (plant_id, ticket_id, round)
);

-- 合批：主 Agent 运行期间积压的待决事项，下一轮一并注入；Jev 仍按事件逐条请求
CREATE TABLE b27_pending (
    plant_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,            -- triage / report
    ticket_id TEXT NOT NULL,
    record TEXT NOT NULL,          -- JSON
    taken INTEGER NOT NULL,
    PRIMARY KEY (plant_id, item_id)
);

CREATE INDEX b27_pending_open ON b27_pending (plant_id, taken, at);

-- 编号分配（事件单号等）：跨分叉连续，不复用
CREATE TABLE b27_counters (
    plant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (plant_id, name)
);

-- 值守层发起的主 Agent 运行（kind=triage）：来源与覆盖不足一并记下，供展示页与 PR 说明引用
CREATE TABLE b27_agent_runs (
    plant_id TEXT NOT NULL,
    agent_run_id TEXT NOT NULL,
    ticket_id TEXT,
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    coverage_gap INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (plant_id, agent_run_id)
);

