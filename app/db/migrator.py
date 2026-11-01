"""Forward-only SQL migrations: NNN_name.sql, each applied atomically."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from ..errors import MigrationError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def _discover(directory: Path) -> list[tuple[int, str, str, str]]:
    items = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME.match(path.name)
        if not m:
            raise MigrationError(f"اسم migration غير صالح: {path.name}")
        sql = path.read_text(encoding="utf-8")
        items.append((int(m.group(1)), m.group(2), sql, hashlib.sha256(sql.encode()).hexdigest()))
    versions = [i[0] for i in items]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"ارقام migrations لازم تكون متسلسلة من 1: {versions}")
    return items


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations. Returns list of newly applied versions."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    newly: list[int] = []
    for version, name, sql, checksum in _discover(directory):
        if version in applied:
            if applied[version] != checksum:
                raise MigrationError(f"migration {version:03d} تغيّر بعد تطبيقه. لا تعدّل migrations قديمة، اكتب واحد جديد.")
            continue
        script = (
            "BEGIN;\n" + sql + "\n"
            f"INSERT INTO schema_migrations(version, name, checksum) VALUES ({version}, '{name}', '{checksum}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"فشل migration {version:03d}_{name}: {exc}") from exc
        newly.append(version)
    return newly


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return row["v"] or 0
    except sqlite3.Error:
        return 0


def ensure_scope_column(conn: sqlite3.Connection) -> bool:
    """Additive work/main scope column on files (2026-10-29 dbformat).

    Existing rows get 'main' via the NOT NULL DEFAULT (SQLite backfills
    the default on ALTER), so the current pool is preserved exactly.
    Returns True if the column was added, False if it already existed.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "scope" in cols:
        return False
    conn.execute("ALTER TABLE files ADD COLUMN scope TEXT NOT NULL DEFAULT 'main'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_scope ON files(scope)")
    conn.commit()
    return True
