CREATE TABLE reports_new (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id      INTEGER NOT NULL,
    search_id    INTEGER REFERENCES search_history(id) ON DELETE SET NULL,
    format       TEXT NOT NULL CHECK (format IN ('html','txt','pdf','xlsx')),
    level        TEXT NOT NULL CHECK (level IN ('results','evidence','metadata','unified')),
    result_count INTEGER,
    path         TEXT NOT NULL,
    job_id       INTEGER,
    size_bytes   INTEGER,
    status       TEXT NOT NULL DEFAULT 'ready'
);
INSERT INTO reports_new(id, ts, user_id, search_id, format, level, result_count, path, job_id, size_bytes, status)
SELECT id, ts, user_id, search_id, format, level, result_count, path, job_id, size_bytes, status FROM reports;
DROP TABLE reports;
ALTER TABLE reports_new RENAME TO reports;
