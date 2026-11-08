CREATE TABLE tutor_user_profiles (
    user_id INTEGER PRIMARY KEY,
    study_preferences TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE tutor_conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    context_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 2000),
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_tutor_conversation_context
ON tutor_conversation_messages (user_id, chat_id, context_key, id DESC);

CREATE INDEX ix_tutor_conversation_user
ON tutor_conversation_messages (user_id, id);
