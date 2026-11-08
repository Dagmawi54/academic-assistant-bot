-- Short-lived copy of the last exercise sheet a student sent, so "do them", "continue"
-- and "question 5" work in later messages. Deleted by /forget_me and expires after 24h.
CREATE TABLE tutor_documents (
    user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    done_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);

-- The last set of context-aware follow-up suggestions shown under a tutor answer (button payloads
-- only carry an index, so the full questions live here).
CREATE TABLE tutor_followups (
    user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);
