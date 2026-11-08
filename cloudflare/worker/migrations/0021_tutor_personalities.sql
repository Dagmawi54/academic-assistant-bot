CREATE TABLE tutor_personalities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 50),
    name_key TEXT NOT NULL CHECK (length(name_key) BETWEEN 1 AND 150),
    instructions TEXT NOT NULL CHECK (length(instructions) BETWEEN 1 AND 4000),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (user_id, name_key)
);

CREATE INDEX ix_tutor_personalities_user
ON tutor_personalities (user_id, created_at_ms, id);
