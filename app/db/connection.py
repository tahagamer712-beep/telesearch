"""SQLite connection factory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..errors import DatabaseError


def connect(db_path: Path, read_only: bool = False, busy_timeout_ms: int = 300000) -> sqlite3.Connection:
    """Open a connection with safe defaults (WAL, FK on, busy timeout).

    busy_timeout 300s (was 120s): a single dense er_match block holds
    BEGIN IMMEDIATE while its pairwise compare loop runs, and observed
    holds exceeded 121s (claim failures at ~121s under the old timeout).
    300s is a MITIGATION, not a guarantee: a dense block (up to 300
    members = 44,850 pairs) has no measured upper bound, so the claim
    retry (jobs._claim) and job retry (jobs._run) cover longer holds.
    """
    db_path = Path(db_path)
    try:
        if read_only:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, isolation_level=None)  # نتحكم بالـtransactions يدوياً
    except sqlite3.Error as exc:
        raise DatabaseError(f"تعذر فتح قاعدة البيانات: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def has_fts5(conn: sqlite3.Connection) -> bool:
    """True if this SQLite build supports FTS5 (tested for real, not assumed)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.Error:
        return False
