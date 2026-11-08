CREATE TABLE assistant_recurring_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source_update_id INTEGER NOT NULL UNIQUE,
    prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 300),
    mode TEXT NOT NULL CHECK (mode IN ('reminder', 'fact')),
    interval_ms INTEGER NOT NULL CHECK (interval_ms BETWEEN 300000 AND 31536000000),
    next_run_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'sending', 'cancelled')),
    lease_until_ms INTEGER,
    last_sent_at_ms INTEGER,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 120),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_assistant_recurring_reminders_due
ON assistant_recurring_reminders (status, next_run_at_ms, lease_until_ms);

CREATE INDEX ix_assistant_recurring_reminders_user_status
ON assistant_recurring_reminders (user_id, status, id);
