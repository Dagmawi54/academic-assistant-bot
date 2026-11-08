CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    private_started_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES bot_users(user_id),
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    scope_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (user_id, chat_id, scope_key)
);

CREATE INDEX IF NOT EXISTS ix_digest_subscriptions_active
ON digest_subscriptions (active, id);

CREATE TABLE IF NOT EXISTS weekly_digest_deliveries (
    id INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES digest_subscriptions(id),
    week_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    retry_after_ms INTEGER,
    sent_at_ms INTEGER,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (subscription_id, week_key)
);

CREATE INDEX IF NOT EXISTS ix_weekly_digest_delivery_due
ON weekly_digest_deliveries (status, retry_after_ms, lease_until_ms, created_at_ms);
