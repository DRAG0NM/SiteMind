-- B27 D53/D55/D61：事件单记录快慢通道与严重程度，慢通道调查、值班提问与试探窗口。
ALTER TABLE b27_tickets ADD COLUMN channel TEXT NOT NULL DEFAULT 'fast';  -- fast / slow / urgent / straight_through
ALTER TABLE b27_tickets ADD COLUMN severity TEXT;                          -- SeverityEstimate JSON
ALTER TABLE b27_tickets ADD COLUMN probe TEXT;                             -- 试探提案、状态、观察窗口与读数

CREATE TABLE b27_investigations (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    at TEXT NOT NULL,
    source TEXT NOT NULL,          -- deterministic / cache / nexent
    agent_run_id TEXT,
    coverage_gap INTEGER NOT NULL,
    diagnosis TEXT,
    experts TEXT NOT NULL,         -- JSON：主 Agent 指定要问的专家
    evidence_refs TEXT NOT NULL,   -- JSON
    probe TEXT,                    -- JSON：本轮返回的试探提案（D61）
    PRIMARY KEY (plant_id, ticket_id, round)
);

-- 值班人员提问（D54 ask_operator）：答案由 B35 回答卡片回写，回写后继续本轮调查
CREATE TABLE b27_questions (
    plant_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    status TEXT NOT NULL,          -- pending / answered / expired
    at TEXT NOT NULL,
    PRIMARY KEY (plant_id, question_id)
);

CREATE INDEX b27_questions_by_ticket ON b27_questions (plant_id, ticket_id, status);
