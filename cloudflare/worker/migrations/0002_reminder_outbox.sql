ALTER TABLE runtime_probe
ADD COLUMN claimed_reminders INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS reminder_outbox (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    send_time_ms INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    sent_at_ms INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS ix_reminder_outbox_due
ON reminder_outbox (status, send_time_ms, lease_until_ms, retry_after_ms);
