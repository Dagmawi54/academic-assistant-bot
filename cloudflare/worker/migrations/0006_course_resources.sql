CREATE TABLE IF NOT EXISTS course_resources (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    source_message_id INTEGER NOT NULL,
    message_link TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('document', 'photo', 'video', 'audio', 'voice', 'link')),
    title TEXT NOT NULL,
    caption TEXT,
    file_id TEXT,
    url TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (chat_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS ix_course_resources_chat_thread
ON course_resources (chat_id, thread_id, created_at_ms DESC);
