-- 011: MTProto large-download bridge — pending oversized downloads
-- (Bot API getFile refuses >20MB on the hosted API; the bridge fetches
-- such files via the owner's account into data/incoming).
CREATE TABLE IF NOT EXISTS large_downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  file_name TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  bot_file_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  downloaded_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_large_downloads_status
  ON large_downloads(status);
