CREATE TABLE IF NOT EXISTS runtime_probe (
    id INTEGER PRIMARY KEY,
    last_cron_at TEXT,
    cron_runs INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO runtime_probe (id, cron_runs) VALUES (1, 0);
