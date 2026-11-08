CREATE TABLE IF NOT EXISTS quiz_sets (
    id INTEGER PRIMARY KEY,
    college_id TEXT NOT NULL REFERENCES colleges(college_id),
    chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
    thread_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    created_by_user_id INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    published_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS ix_quiz_sets_group_status
ON quiz_sets (college_id, chat_id, status, created_at_ms);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id INTEGER PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quiz_sets(id),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 20),
    prompt TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_option_index INTEGER NOT NULL CHECK (correct_option_index BETWEEN 0 AND 3),
    explanation TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (quiz_id, position)
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quiz_sets(id),
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'completed')),
    score INTEGER NOT NULL DEFAULT 0 CHECK (score >= 0),
    total_questions INTEGER NOT NULL DEFAULT 0 CHECK (total_questions >= 0),
    started_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    UNIQUE (quiz_id, user_id, started_at_ms)
);

CREATE INDEX IF NOT EXISTS ix_quiz_attempts_student
ON quiz_attempts (user_id, quiz_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS ux_quiz_attempts_one_open
ON quiz_attempts (quiz_id, user_id) WHERE status = 'in_progress';

CREATE TABLE IF NOT EXISTS quiz_answers (
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id),
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
    selected_option_index INTEGER NOT NULL CHECK (selected_option_index BETWEEN 0 AND 3),
    is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    answered_at_ms INTEGER NOT NULL,
    PRIMARY KEY (attempt_id, question_id)
);
