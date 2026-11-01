-- Scheduled notifications: ONLY non-record content (aggregate stats / operational summary / task events).
CREATE TABLE notification_schedules (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    created_by    INTEGER NOT NULL,
    targets       TEXT NOT NULL DEFAULT '[]',
    content_type  TEXT NOT NULL CHECK (content_type IN ('stats','ops_summary','task_events')),
    frequency     TEXT NOT NULL CHECK (frequency IN ('interval','daily','weekly')),
    interval_min  INTEGER,
    time_of_day   TEXT,
    weekday       INTEGER,
    max_items     INTEGER NOT NULL DEFAULT 10,
    format        TEXT NOT NULL DEFAULT 'text' CHECK (format IN ('text')),
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    next_run      TEXT,
    last_run      TEXT,
    last_status   TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    retry_targets TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
