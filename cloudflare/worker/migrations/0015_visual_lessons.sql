CREATE TABLE tutor_visual_lessons (
    lesson_id TEXT PRIMARY KEY CHECK (length(lesson_id) BETWEEN 16 AND 48),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 180),
    article_html TEXT NOT NULL CHECK (length(article_html) BETWEEN 1 AND 16000),
    visual_svg TEXT NOT NULL CHECK (length(visual_svg) BETWEEN 1 AND 16000),
    sources_json TEXT NOT NULL DEFAULT '[]' CHECK (length(sources_json) <= 6000),
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_tutor_visual_lessons_expiry
ON tutor_visual_lessons (expires_at_ms);
