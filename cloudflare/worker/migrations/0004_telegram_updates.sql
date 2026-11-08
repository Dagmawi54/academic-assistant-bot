CREATE TABLE IF NOT EXISTS telegram_update_receipts (
    update_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('processing', 'failed', 'processed', 'ignored')),
    attempt_count INTEGER NOT NULL DEFAULT 1,
    lease_until_ms INTEGER,
    received_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS ix_telegram_update_retry
ON telegram_update_receipts (status, lease_until_ms, received_at_ms);

ALTER TABLE academic_items ADD COLUMN source_update_id INTEGER;
ALTER TABLE academic_items ADD COLUMN source_message_id INTEGER;
ALTER TABLE academic_items ADD COLUMN source_message_link TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_academic_items_source_update
ON academic_items (source_update_id);
