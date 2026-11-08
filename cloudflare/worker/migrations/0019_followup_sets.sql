-- Keep each tutor answer's buttons bound to that answer. Older answer buttons remain valid.
CREATE TABLE tutor_followup_sets (
    user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    set_key TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id, set_key)
);

CREATE INDEX ix_tutor_followup_sets_expiry
    ON tutor_followup_sets (created_at_ms);
