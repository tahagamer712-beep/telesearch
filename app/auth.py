"""Roles: ADMIN and USER only.
ADMIN  = ids listed in .env AUTHORIZED_USER_IDS (the only source of truth; cannot be created from Telegram).
USER   = added by an ADMIN from the Admin Panel, with an explicit permission list.
A USER can never receive admin functions: 'manage' (and anything admin) is not grantable."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

PERMISSIONS = ("search", "smart", "sources", "stats", "reports", "export", "upload")
PERM_AR = {"search": "🔎 البحث", "smart": "🧠 البحث الذكي", "sources": "📁 المصادر", "stats": "📊 الإحصائيات", "reports": "📤 التقارير", "export": "📦 التصدير", "upload": "⬆️ رفع ملفات"}
ROLES = ("admin", "user")


@dataclass(frozen=True)
class User:
    telegram_id: int
    role: str
    display_name: str | None = None
    perms: frozenset = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, perm: str) -> bool:
        if perm == "view_full":
            return True
        if perm == "manage":
            return self.is_admin
        if self.is_admin:
            return True
        return perm in PERMISSIONS and perm in self.perms

    def require(self, perm: str) -> None:
        from .errors import PermissionDenied
        if not self.can(perm):
            raise PermissionDenied(perm)


def _perms(raw: str | None) -> frozenset:
    try:
        return frozenset(p for p in json.loads(raw or "[]") if p in PERMISSIONS)
    except ValueError:
        return frozenset()


def get_user(conn: sqlite3.Connection, telegram_id: int) -> User | None:
    r = conn.execute("SELECT telegram_id, role, display_name, permissions FROM users WHERE telegram_id=? AND enabled=1 AND role='user'", (telegram_id,)).fetchone()
    return User(r["telegram_id"], "user", r["display_name"], _perms(r["permissions"])) if r else None


def authorize(conn: sqlite3.Connection, cfg, telegram_id: int) -> User | None:
    """The ONLY access decision point. .env ids => ADMIN. Otherwise an enabled USER row. Else None (Access Denied)."""
    if telegram_id in cfg.authorized_user_ids:
        return User(telegram_id, "admin", "admin", frozenset(PERMISSIONS))
    return get_user(conn, telegram_id)


def sync_admins(conn: sqlite3.Connection, ids) -> None:
    """Make DB match .env: env ids are admins; any other 'admin' row is disabled (no hidden admins)."""
    ids = list(ids)
    for i in ids:
        conn.execute("INSERT INTO users(telegram_id, role, display_name) VALUES (?, 'admin', 'admin') ON CONFLICT(telegram_id) DO UPDATE SET role='admin', enabled=1", (i,))
    q = ",".join("?" * len(ids)) or "NULL"
    conn.execute(f"UPDATE users SET enabled=0 WHERE role='admin' AND telegram_id NOT IN ({q})", ids)


def add_user(conn, telegram_id: int, name: str | None = None, username: str | None = None, perms=("search",)) -> None:
    perms = [p for p in perms if p in PERMISSIONS]
    conn.execute("INSERT INTO users(telegram_id, role, display_name, username, permissions) VALUES (?, 'user', ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                 "display_name=COALESCE(excluded.display_name, display_name), username=COALESCE(excluded.username, username), permissions=excluded.permissions, enabled=1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                 "WHERE users.role='user'", (telegram_id, name, username, json.dumps(perms)))


def set_perms(conn, telegram_id: int, perms) -> None:
    conn.execute("UPDATE users SET permissions=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE telegram_id=? AND role='user'", (json.dumps([p for p in perms if p in PERMISSIONS]), telegram_id))


def set_enabled(conn, telegram_id: int, enabled: bool) -> None:
    conn.execute("UPDATE users SET enabled=? WHERE telegram_id=? AND role='user'", (1 if enabled else 0, telegram_id))


def remove_user(conn, telegram_id: int) -> None:
    conn.execute("DELETE FROM users WHERE telegram_id=? AND role='user'", (telegram_id,))


def mask_value(value, role):  # قديم: لم يعد مستخدماً (لا masking بين ADMIN/USER)
    return value
