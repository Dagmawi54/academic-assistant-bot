CREATE TABLE tutor_ai_daily_usage_replacement (
    usage_date TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (usage_date, chat_id, user_id)
);

INSERT INTO tutor_ai_daily_usage_replacement (
    usage_date, chat_id, user_id, request_count, updated_at_ms
)
SELECT usage_date, chat_id, user_id, request_count, updated_at_ms
FROM tutor_ai_daily_usage;

DROP INDEX ix_tutor_ai_daily_usage_group;
DROP TABLE tutor_ai_daily_usage;
ALTER TABLE tutor_ai_daily_usage_replacement RENAME TO tutor_ai_daily_usage;

CREATE INDEX ix_tutor_ai_daily_usage_group
ON tutor_ai_daily_usage (usage_date, chat_id, request_count);
