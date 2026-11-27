-- B27 D61/D59：事件单级的小状态（残差报警跳变、试探基线读数）与试探清理。
CREATE TABLE b27_ticket_state (
    plant_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (plant_id, ticket_id, key)
);
