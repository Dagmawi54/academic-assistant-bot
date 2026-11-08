CREATE TABLE IF NOT EXISTS colleges (
    college_id TEXT PRIMARY KEY,
    college_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS college_invite_codes (
    code_hash TEXT PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    remaining_uses INTEGER NOT NULL CHECK (remaining_uses >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS academic_groups (
    chat_id TEXT PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    group_title TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_by_user_id INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_academic_groups_college
ON academic_groups (college_id, active);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    course_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at_ms INTEGER NOT NULL,
    UNIQUE (chat_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS group_topics (
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    thread_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    created_by_user_id INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS group_representatives (
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    user_id INTEGER NOT NULL,
    added_by_user_id INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

ALTER TABLE academic_items ADD COLUMN college_id TEXT;
ALTER TABLE academic_items ADD COLUMN course_id INTEGER;
ALTER TABLE course_resources ADD COLUMN college_id TEXT;

CREATE INDEX IF NOT EXISTS ix_academic_items_college_chat_deadline
ON academic_items (college_id, chat_id, deadline_ms);

CREATE INDEX IF NOT EXISTS ix_course_resources_college_chat
ON course_resources (college_id, chat_id, thread_id);
