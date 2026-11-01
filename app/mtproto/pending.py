"""Pending large-file downloads fetched via the owner's MTProto account.

Bot API getFile refuses files over 20MB on the hosted API, so documents
larger than the bot's download limit that arrive FROM an authorized
account are not rejected: they are recorded here as pending and a
separate bridge process (bridge.py, Telethon, running as the owner's
account) downloads them into data/incoming, where the existing watcher
ingests them.

Nothing here touches Telegram or the owner's session; it only manages
rows in the bot's own database. The bot's connections are autocommit
(isolation_level=None, see app/db/connection.py), so no explicit
commit is needed or performed here.

Schema is created by migration 011_large_downloads.sql; ensure_schema()
recreates it idempotently for fresh/test databases.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid

log = logging.getLogger("pdb.pending")

QUEUE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "queue", "pending_files")

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
MAX_ATTEMPTS = 10

TABLE_SQL = """
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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the large_downloads table/index if missing (idempotent)."""
    conn.executescript(TABLE_SQL)


def decide_document(user_id: int, authorized_ids, size_bytes: int,
                    limit_mb: int) -> str:
    """Route an incoming document: 'normal', 'bridge', or 'reject'.

    - size at or under the bot download limit -> 'normal' (Bot API path).
    - size over the limit AND sender is authorized (the owner set) ->
      'bridge' (record pending; the MTProto bridge fetches it).
    - size over the limit AND sender is anyone else -> 'reject'
      (the bridge runs as the owner's account and can only see the
      owner's own messages, so other senders' files can never be
      fetched this way; they keep the original rejection).
    """
    if size_bytes <= limit_mb * 1024 * 1024:
        return "normal"
    return "bridge" if user_id in authorized_ids else "reject"


def _ensure_queue_dir() -> str:
    os.makedirs(QUEUE_DIR, exist_ok=True)
    return QUEUE_DIR


def _queue_to_disk(user_id: int, chat_id: int, message_id,
                   file_name: str, file_size: int,
                   bot_file_id) -> str:
    qdir = _ensure_queue_dir()
    qid = "%d_%s" % (int(time.time()*1000), uuid.uuid4().hex[:8])
    qpath = os.path.join(qdir, qid + ".json")
    payload = {
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_name": file_name,
        "file_size": file_size,
        "bot_file_id": bot_file_id,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(qpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return qpath


def drain_queue(conn: sqlite3.Connection) -> int:
    qdir = _ensure_queue_dir()
    moved = 0
    try:
        qfiles = sorted(f for f in os.listdir(qdir) if f.endswith(".json"))
    except OSError:
        return 0
    for qf in qfiles:
        qpath = os.path.join(qdir, qf)
        try:
            with open(qpath, "r", encoding="utf-8") as f:
                p = json.load(f)
            cur = conn.execute(
                "INSERT INTO large_downloads"
                "(user_id, chat_id, message_id, file_name, file_size, bot_file_id)"
                " VALUES (?,?,?,?,?,?)",
                (p["user_id"], p["chat_id"], p.get("message_id"),
                 p["file_name"], p["file_size"], p.get("bot_file_id")))
            moved += 1
            os.remove(qpath)
            log.info("drained queued file %s -> id %s", p["file_name"], cur.lastrowid)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                break
            log.exception("drain_queue failed for %s", qf)
            break
        except Exception:
            log.exception("drain_queue failed for %s", qf)
    return moved


def create_pending(conn: sqlite3.Connection, user_id: int, chat_id: int,
                   message_id: int | None, file_name: str,
                   file_size: int, bot_file_id: str | None = None) -> int:
    """Record a pending large download. Returns new row id, or -1 if queued to disk."""
    try:
        drain_queue(conn)
    except Exception:
        log.exception("drain_queue pre-check failed")
    try:
        cur = conn.execute(
            "INSERT INTO large_downloads"
            "(user_id, chat_id, message_id, file_name, file_size, bot_file_id)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, chat_id, message_id, file_name, file_size, bot_file_id))
        return cur.lastrowid
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            qpath = _queue_to_disk(user_id, chat_id, message_id,
                                   file_name, file_size, bot_file_id)
            log.warning("db locked, queued to disk: %s -> %s", file_name, qpath)
            return -1
        raise


def get_pending(conn: sqlite3.Connection, pending_id: int) -> dict | None:
    """Fetch one pending row as a plain dict, or None."""
    row = conn.execute(
        "SELECT * FROM large_downloads WHERE id=?", (pending_id,)).fetchone()
    return dict(row) if row else None


def claim_pending(conn: sqlite3.Connection) -> dict | None:
    """Atomically claim the oldest pending row for download.

    Sets status='downloading' and bumps attempts. Returns the claimed
    row, or None when nothing is pending. A row that exhausted its
    attempts is failed instead of claimed again.
    """
    row = conn.execute(
        "SELECT * FROM large_downloads WHERE status=? ORDER BY id ASC "
        "LIMIT 1", (STATUS_PENDING,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["attempts"] >= MAX_ATTEMPTS:
        conn.execute(
            "UPDATE large_downloads SET status=?, error=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (STATUS_FAILED, "too many attempts", row["id"]))
        return None
    cur = conn.execute(
        "UPDATE large_downloads SET status=?, attempts=attempts+1, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id=? AND status=?",
        (STATUS_DOWNLOADING, row["id"], STATUS_PENDING))
    if cur.rowcount != 1:
        return None
    row["status"] = STATUS_DOWNLOADING
    row["attempts"] += 1
    return row


def complete_pending(conn: sqlite3.Connection, pending_id: int,
                     downloaded_path: str) -> None:
    """Mark a pending row done with the final incoming path."""
    conn.execute(
        "UPDATE large_downloads SET status=?, downloaded_path=?, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
        (STATUS_DONE, downloaded_path, pending_id))


def fail_pending(conn: sqlite3.Connection, pending_id: int,
                 error: str) -> None:
    """Mark a pending row failed with a (truncated) reason."""
    conn.execute(
        "UPDATE large_downloads SET status=?, error=?, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
        (STATUS_FAILED, (error or "unknown")[:500], pending_id))


def release_pending(conn: sqlite3.Connection, pending_id: int) -> None:
    """Return a claimed row to pending for a later retry (transient error)."""
    conn.execute(
        "UPDATE large_downloads SET status=?, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id=? AND status=?",
        (STATUS_PENDING, pending_id, STATUS_DOWNLOADING))
