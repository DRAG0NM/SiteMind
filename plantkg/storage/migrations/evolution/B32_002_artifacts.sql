CREATE TABLE b32_policy_artifacts (
    plant_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (plant_id, digest)
);

CREATE TABLE b32_skill_artifacts (
    plant_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (plant_id, digest)
);
