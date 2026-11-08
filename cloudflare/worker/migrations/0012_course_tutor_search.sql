CREATE TABLE course_tutor_index_jobs (
    source_id INTEGER PRIMARY KEY REFERENCES course_quiz_sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'indexed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_course_tutor_index_jobs_due
ON course_tutor_index_jobs (status, retry_after_ms, created_at_ms);

CREATE TABLE course_tutor_passages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES course_quiz_sources(id) ON DELETE CASCADE,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    semester_label TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    message_link TEXT,
    title TEXT NOT NULL,
    passage_index INTEGER NOT NULL,
    passage_text TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (source_id, passage_index)
);

CREATE INDEX ix_course_tutor_passages_scope
ON course_tutor_passages (college_id, chat_id, course_id, semester_label, thread_id, source_id);

CREATE VIRTUAL TABLE course_tutor_passages_fts USING fts5(
    title,
    passage_text,
    content='course_tutor_passages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER course_tutor_passages_ai AFTER INSERT ON course_tutor_passages BEGIN
    INSERT INTO course_tutor_passages_fts (rowid, title, passage_text)
    VALUES (new.id, new.title, new.passage_text);
END;

CREATE TRIGGER course_tutor_passages_ad AFTER DELETE ON course_tutor_passages BEGIN
    INSERT INTO course_tutor_passages_fts (course_tutor_passages_fts, rowid, title, passage_text)
    VALUES ('delete', old.id, old.title, old.passage_text);
END;

CREATE TRIGGER course_tutor_passages_au AFTER UPDATE ON course_tutor_passages BEGIN
    INSERT INTO course_tutor_passages_fts (course_tutor_passages_fts, rowid, title, passage_text)
    VALUES ('delete', old.id, old.title, old.passage_text);
    INSERT INTO course_tutor_passages_fts (rowid, title, passage_text)
    VALUES (new.id, new.title, new.passage_text);
END;

CREATE TRIGGER course_quiz_source_tutor_index_ai
AFTER INSERT ON course_quiz_sources
WHEN new.extraction_state = 'ready'
BEGIN
    INSERT INTO course_tutor_index_jobs (source_id, created_at_ms, updated_at_ms)
    VALUES (new.id, new.updated_at_ms, new.updated_at_ms)
    ON CONFLICT(source_id) DO UPDATE SET
        status = 'pending', attempt_count = 0, lease_until_ms = NULL,
        retry_after_ms = NULL, last_error = NULL, updated_at_ms = excluded.updated_at_ms;
END;

CREATE TRIGGER course_quiz_source_tutor_index_au
AFTER UPDATE OF extraction_state, extracted_text, title, message_link ON course_quiz_sources
WHEN new.extraction_state = 'ready'
BEGIN
    INSERT INTO course_tutor_index_jobs (source_id, created_at_ms, updated_at_ms)
    VALUES (new.id, new.updated_at_ms, new.updated_at_ms)
    ON CONFLICT(source_id) DO UPDATE SET
        status = 'pending', attempt_count = 0, lease_until_ms = NULL,
        retry_after_ms = NULL, last_error = NULL, updated_at_ms = excluded.updated_at_ms;
END;

INSERT INTO course_tutor_index_jobs (source_id, created_at_ms, updated_at_ms)
SELECT id, created_at_ms, updated_at_ms
FROM course_quiz_sources
WHERE extraction_state = 'ready'
ON CONFLICT(source_id) DO NOTHING;

CREATE TABLE tutor_ai_daily_usage (
    usage_date TEXT NOT NULL,
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    user_id INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (usage_date, chat_id, user_id)
);

CREATE INDEX ix_tutor_ai_daily_usage_group
ON tutor_ai_daily_usage (usage_date, chat_id, request_count);
