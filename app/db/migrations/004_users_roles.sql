-- ADMIN / USER فقط. ADMIN = ids في .env (المصدر الوحيد). USER يضيفه الادمن مع صلاحيات.
CREATE TABLE users_new (
    telegram_id  INTEGER PRIMARY KEY,
    display_name TEXT,
    username     TEXT,
    role         TEXT NOT NULL CHECK (role IN ('admin','user')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    permissions  TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT
);
INSERT INTO users_new(telegram_id, display_name, role, enabled, permissions, created_at)
SELECT telegram_id, display_name,
       CASE WHEN role='admin' THEN 'admin' ELSE 'user' END, enabled,
       CASE role WHEN 'searcher' THEN '["search"]'
                 WHEN 'reader' THEN '["search","sources","stats"]'
                 WHEN 'exporter' THEN '["search","sources","stats","reports","export"]'
                 ELSE '[]' END,
       created_at
FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;
