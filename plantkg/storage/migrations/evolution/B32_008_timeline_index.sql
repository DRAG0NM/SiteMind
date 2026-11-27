CREATE INDEX IF NOT EXISTS b32_timeline_event_lookup ON b32_timeline(
    plant_id, json_extract(body, '$.event'), json_extract(body, '$.event_type'), json_extract(body, '$.skill_name'));
