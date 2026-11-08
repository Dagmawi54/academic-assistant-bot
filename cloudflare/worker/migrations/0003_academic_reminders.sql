CREATE TABLE IF NOT EXISTS academic_items (
    source_item_id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    item_type TEXT NOT NULL CHECK (item_type IN ('assignment', 'exam', 'quiz')),
    title TEXT,
    deadline_ms INTEGER,
    room TEXT,
    coverage TEXT,
    item_status TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

ALTER TABLE reminder_outbox ADD COLUMN source_item_id INTEGER;
ALTER TABLE reminder_outbox ADD COLUMN source_reminder_id INTEGER;
ALTER TABLE reminder_outbox ADD COLUMN group_id INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS ux_academic_items_source
ON academic_items (source_item_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reminder_outbox_source
ON reminder_outbox (source_item_id, source_reminder_id);

CREATE INDEX IF NOT EXISTS ix_academic_items_group_deadline
ON academic_items (group_id, deadline_ms);
