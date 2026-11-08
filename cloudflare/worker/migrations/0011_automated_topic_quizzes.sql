ALTER TABLE academic_groups ADD COLUMN current_semester TEXT;

CREATE TABLE course_offerings (
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    semester_label TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (chat_id, course_id, semester_label)
);

CREATE INDEX ix_course_offerings_current
ON course_offerings (college_id, chat_id, semester_label, active);

CREATE TABLE quiz_topic_settings (
    chat_id TEXT PRIMARY KEY REFERENCES academic_groups(chat_id),
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    thread_id INTEGER NOT NULL,
    set_by_user_id INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE quiz_generation_runs (
    id INTEGER PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    semester_label TEXT NOT NULL,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    target_thread_id INTEGER NOT NULL,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('regular', 'exam')),
    run_key TEXT NOT NULL,
    exam_item_id INTEGER REFERENCES academic_items(source_item_id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'generating', 'ready', 'sending', 'completed', 'failed', 'uncertain')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    last_error TEXT,
    UNIQUE (chat_id, run_key)
);

CREATE INDEX ix_quiz_generation_due
ON quiz_generation_runs (status, retry_after_ms, created_at_ms);

CREATE TABLE generated_quiz_questions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES quiz_generation_runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 20),
    prompt TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_option_index INTEGER NOT NULL CHECK (correct_option_index BETWEEN 0 AND 3),
    source_message_id INTEGER NOT NULL,
    source_excerpt TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, position)
);

CREATE TABLE quiz_poll_deliveries (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES quiz_generation_runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 20),
    delivery_kind TEXT NOT NULL CHECK (delivery_kind IN ('header', 'poll')),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'uncertain', 'failed')),
    poll_id TEXT,
    created_at_ms INTEGER NOT NULL,
    attempted_at_ms INTEGER,
    sent_at_ms INTEGER,
    last_error TEXT,
    UNIQUE (run_id, position)
);

CREATE INDEX ix_quiz_poll_delivery_due
ON quiz_poll_deliveries (status, run_id, position);

CREATE TABLE quiz_ai_daily_usage (
    usage_date TEXT PRIMARY KEY,
    extraction_calls INTEGER NOT NULL DEFAULT 0,
    generation_calls INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE course_quiz_sources (
    id INTEGER PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    semester_label TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    message_link TEXT,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('document', 'photo', 'note')),
    title TEXT NOT NULL,
    caption TEXT,
    file_id TEXT,
    mime_type TEXT,
    file_size INTEGER,
    extracted_text TEXT,
    content_sha256 TEXT,
    extraction_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_state IN ('pending', 'processing', 'ready', 'ignored', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (chat_id, source_message_id)
);

CREATE INDEX ix_course_quiz_sources_due
ON course_quiz_sources (extraction_state, retry_after_ms, created_at_ms);

CREATE INDEX ix_course_quiz_sources_course
ON course_quiz_sources (college_id, chat_id, course_id, semester_label, extraction_state, created_at_ms);
