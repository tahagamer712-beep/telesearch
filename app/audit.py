"""Audit log. Never stores secrets."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

_SECRET_KEY = re.compile(r"token|key|secret|pass|auth", re.I)
# Audit fix (Phase 6 9.2): secret-SHAPED values under innocent keys used to be
# stored verbatim (only secret-NAMED keys were redacted). Telegram bot tokens
# (digits + ':' + 20+ token chars) and sk- API keys are scrubbed wherever they
# appear. Deliberately narrow: plain hex hashes (e.g. file SHA-256) and phone
# numbers do NOT match these shapes and are left intact.
_SECRET_VALUE = re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b|\bsk-[A-Za-z0-9]{10,}\b")


def _scrub(v):
    if isinstance(v, str):
        return _SECRET_VALUE.sub("***", v)
    if isinstance(v, dict):
        return {k: _scrub(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v

# actions
SEARCH, OPEN, EXPORT, IMPORT, INDEX, SETTINGS, RETRY, DELETE = "search", "open", "export", "import", "index", "settings", "retry", "delete"
DENIED, JOB, BACKUP, USERS = "denied", "job", "backup", "users"

_log = logging.getLogger("pdb.audit")


def log(conn: sqlite3.Connection, user_id: int | None, action: str, target_type: str | None = None,
        target_id=None, details: dict | None = None) -> None:
    clean = None
    if details:
        clean = json.dumps({k: ("***" if _SECRET_KEY.search(k) else _scrub(v)) for k, v in details.items()}, ensure_ascii=False)[:2000]
    # Audit must never break a user action: retry briefly on lock, then drop
    # the entry with a warning instead of raising (2026-10-26 export buttons).
    for attempt in range(3):
        try:
            conn.execute("INSERT INTO audit_log(user_id, action, target_type, target_id, details) VALUES (?,?,?,?,?)",
                         (user_id, action, target_type, None if target_id is None else str(target_id), clean))
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            time.sleep(0.5 * (attempt + 1))
    _log.warning("audit dropped after lock retries: %s %s", action, target_type)


def recent(conn: sqlite3.Connection, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
