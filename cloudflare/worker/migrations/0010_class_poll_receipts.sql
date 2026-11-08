CREATE TABLE IF NOT EXISTS class_poll_receipts (
    update_id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed')),
    poll_id TEXT,
    created_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS ix_class_poll_receipts_chat_created
ON class_poll_receipts (chat_id, created_at_ms);
