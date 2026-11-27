-- B27 提案表：ProposalSink.save 落库时分配 proposal_id；回答经 attach_reply 挂到同一提案上。
-- 按事件单与轮次可查，供提案就绪钩子只收当前轮。
CREATE TABLE b27_proposals (
    plant_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    seq INTEGER NOT NULL,          -- 厂区内递增序号，proposal_id 由它分配
    ticket_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL,
    expert TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    replied INTEGER NOT NULL,      -- 专家回答是否已挂上
    record TEXT NOT NULL,          -- ExpertProposal JSON
    PRIMARY KEY (plant_id, proposal_id)
);

CREATE UNIQUE INDEX b27_proposals_seq ON b27_proposals (plant_id, seq);
CREATE INDEX b27_proposals_by_round ON b27_proposals (plant_id, ticket_id, round);
