CREATE TABLE observed_group_topics (
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id) ON DELETE CASCADE,
    thread_id INTEGER NOT NULL,
    topic_title TEXT NOT NULL CHECK (length(topic_title) BETWEEN 1 AND 255),
    observed_by_user_id INTEGER,
    observed_at_ms INTEGER NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE INDEX ix_observed_group_topics_chat_time
ON observed_group_topics (chat_id, observed_at_ms, thread_id);

CREATE TABLE group_setup_runs (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id) ON DELETE CASCADE,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    semester_label TEXT NOT NULL CHECK (length(semester_label) BETWEEN 1 AND 40),
    started_by_user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'reviewing'
        CHECK (status IN ('reviewing', 'completed', 'undone', 'stale')),
    created_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);

CREATE INDEX ix_group_setup_runs_latest
ON group_setup_runs (chat_id, id DESC, status);

CREATE TABLE group_setup_changes (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES group_setup_runs(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id) ON DELETE CASCADE,
    thread_id INTEGER NOT NULL,
    previous_course_id INTEGER REFERENCES courses(id),
    applied_course_id INTEGER NOT NULL REFERENCES courses(id),
    applied_by_user_id INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    undone_at_ms INTEGER,
    UNIQUE (run_id, thread_id)
);

CREATE INDEX ix_group_setup_changes_run
ON group_setup_changes (run_id, id);
