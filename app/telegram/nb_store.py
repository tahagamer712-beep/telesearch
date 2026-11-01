"""Shared store + helpers for the new-buttons program (nb:*).

Idempotent tables only — deliberately NOT a numbered migration (migration
011 is already claimed twice elsewhere: ER auto-accept and the MTProto
bridge). Call ensure_nb_tables() once at bot start and once in tests.

Privacy rule enforced by construction: every screen in this program shows
only counts, ids, statuses, file names, column names and job numbers.
Record field VALUES are never read here and must never appear in any
nb:* screen, message or log line. Tests seed a sentinel record value and
assert it never appears in captured output.
"""
from __future__ import annotations

import json
import sqlite3

from .. import audit
from ..errors import PermissionDenied

TABLES = {
    "nb_templates": """CREATE TABLE IF NOT EXISTS nb_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        name TEXT NOT NULL, conditions TEXT NOT NULL DEFAULT '{}',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_notif_prefs": """CREATE TABLE IF NOT EXISTS nb_notif_prefs (
        user_id INTEGER PRIMARY KEY, on_finish INTEGER NOT NULL DEFAULT 1,
        on_fail INTEGER NOT NULL DEFAULT 1, updated_at TEXT
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_issue_reports": """CREATE TABLE IF NOT EXISTS nb_issue_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        kind TEXT NOT NULL, job_id INTEGER, status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_perm_requests": """CREATE TABLE IF NOT EXISTS nb_perm_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        perm TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', decided_by INTEGER,
        decide_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_del_requests": """CREATE TABLE IF NOT EXISTS nb_del_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        target_type TEXT NOT NULL, target_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', decided_by INTEGER,
        decide_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_batches": """CREATE TABLE IF NOT EXISTS nb_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        created_by INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_batch_files": """CREATE TABLE IF NOT EXISTS nb_batch_files (
        batch_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
        PRIMARY KEY (batch_id, file_id))""",
    "nb_isolated_files": """CREATE TABLE IF NOT EXISTS nb_isolated_files (
        file_id INTEGER PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
        by_user INTEGER NOT NULL,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_settings_log": """CREATE TABLE IF NOT EXISTS nb_settings_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        setting TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '',
        new_value TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    "nb_export_lock": """CREATE TABLE IF NOT EXISTS nb_export_lock (
        id INTEGER PRIMARY KEY CHECK (id = 1), locked INTEGER NOT NULL DEFAULT 0,
        by_user INTEGER, reason TEXT NOT NULL DEFAULT '')""",
    "nb_maintenance": """CREATE TABLE IF NOT EXISTS nb_maintenance (
        id INTEGER PRIMARY KEY CHECK (id = 1), active INTEGER NOT NULL DEFAULT 0,
        by_user INTEGER, reason TEXT NOT NULL DEFAULT '')""",
    "nb_perm_groups": """CREATE TABLE IF NOT EXISTS nb_perm_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        perms TEXT NOT NULL DEFAULT '[]')""",
    "nb_group_members": """CREATE TABLE IF NOT EXISTS nb_group_members (
        group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        PRIMARY KEY (group_id, user_id))""",
    "nb_temp_perms": """CREATE TABLE IF NOT EXISTS nb_temp_perms (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        perm TEXT NOT NULL, expires_at TEXT NOT NULL,
        granted_by INTEGER NOT NULL)""",
    "nb_pending_text": """CREATE TABLE IF NOT EXISTS nb_pending_text (
        user_id INTEGER PRIMARY KEY, action TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}')""",
    "nb_retention": """CREATE TABLE IF NOT EXISTS nb_retention (
        kind TEXT PRIMARY KEY, days INTEGER NOT NULL DEFAULT 30)""",
}


def ensure_nb_tables(conn: sqlite3.Connection) -> None:
    for sql in TABLES.values():
        conn.execute(sql)
    conn.execute("INSERT OR IGNORE INTO nb_export_lock(id, locked) VALUES (1, 0)")
    conn.execute("INSERT OR IGNORE INTO nb_maintenance(id, active) VALUES (1, 0)")
    for kind, days in (("temp", 7), ("reports", 90), ("logs", 180)):
        conn.execute("INSERT OR IGNORE INTO nb_retention(kind, days) VALUES (?, ?)", (kind, days))
    for name, perms in (("report-reader", ["search", "reports"]),
                         ("processing-operator", ["search", "sources", "upload"]),
                         ("file-manager", ["search", "sources", "upload", "export", "reports"])):
        conn.execute("INSERT OR IGNORE INTO nb_perm_groups(name, perms) VALUES (?, ?)",
                     (name, json.dumps(perms)))
    try:
        conn.commit()
    except sqlite3.Error:
        pass


def require(user, perm: str) -> None:
    """Server-side permission check. 'any' = any authenticated user.
    'manage' = admin only. Anything else = existing user.require()."""
    if perm == "any":
        return
    if perm == "manage":
        if not user.is_admin:
            raise PermissionDenied("manage")
        return
    user.require(perm)


def audit_nb(conn, uid, action: str, target_type=None, target_id=None, details=None) -> None:
    audit.log(conn, uid, action, target_type, target_id, details or {})


def confirm_markup(yes_cb: str, no_cb: str):
    from .ui import kb, nav
    return kb([("✅ تأكيد", yes_cb), ("❌ إلغاء", no_cb)], nav())


def page_slice(rows, page: int, size: int = 10):
    page = max(0, int(page))
    total = len(rows)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages - 1)
    return rows[page * size:(page + 1) * size], pages, page


def set_pending(conn, uid: int, action: str, payload: dict | None = None) -> None:
    conn.execute("INSERT INTO nb_pending_text(user_id, action, payload) VALUES (?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET action=excluded.action, payload=excluded.payload",
                 (uid, action, json.dumps(payload or {}, ensure_ascii=False)))
    try:
        conn.commit()
    except sqlite3.Error:
        pass


def pop_pending(conn, uid: int):
    row = conn.execute("SELECT action, payload FROM nb_pending_text WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM nb_pending_text WHERE user_id=?", (uid,))
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    try:
        return row["action"], json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return row["action"], {}


def export_locked(conn) -> bool:
    # Hardened (router wiring): a DB without nb tables yet (old test DBs,
    # pre-boot) reads as unlocked, never crashes the caller.
    try:
        row = conn.execute("SELECT locked FROM nb_export_lock WHERE id=1").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def maintenance_active(conn) -> bool:
    # Hardened (router wiring): same rule as export_locked — missing
    # tables read as "maintenance off", never crash the caller.
    try:
        row = conn.execute("SELECT active FROM nb_maintenance WHERE id=1").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])
