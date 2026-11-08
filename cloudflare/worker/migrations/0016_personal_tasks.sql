CREATE TABLE assistant_user_settings (
    user_id INTEGER PRIMARY KEY,
    timezone_name TEXT NOT NULL DEFAULT 'Africa/Addis_Ababa'
        CHECK (length(timezone_name) BETWEEN 1 AND 64),
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE personal_tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    source_update_id INTEGER UNIQUE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    due_at_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'completed', 'cancelled')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);

CREATE INDEX ix_personal_tasks_user_status_due
ON personal_tasks (user_id, status, due_at_ms, id);

CREATE TABLE personal_task_reminders (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES personal_tasks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    scheduled_at_ms INTEGER NOT NULL,
    message_text TEXT NOT NULL CHECK (length(message_text) BETWEEN 1 AND 500),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled', 'uncertain')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    sent_at_ms INTEGER,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 120),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_personal_task_reminders_due
ON personal_task_reminders (status, scheduled_at_ms, lease_until_ms, retry_after_ms);

CREATE TABLE assistant_pending_actions (
    user_id INTEGER PRIMARY KEY,
    source_update_id INTEGER NOT NULL,
    intent_json TEXT NOT NULL CHECK (length(intent_json) BETWEEN 1 AND 4000),
    expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_assistant_pending_actions_expiry
ON assistant_pending_actions (expires_at_ms);
