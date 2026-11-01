"""Health / compatibility checks (also used by status.sh and the Diagnostics screen)."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from .archive.manager import ArchiveManager
from .config import Config
from .db.connection import connect, has_fts5
from .db.migrator import MIGRATIONS_DIR, current_version
from .resources import ResourceMonitor

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def run_checks(cfg: Config, deep: bool = False) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    add = lambda n, s, d="": out.append((n, s, d))
    add("Python", OK, sys.version.split()[0])
    add("SQLite", OK, sqlite3.sqlite_version)
    try:
        conn = connect(cfg.db_path)
        try:
            want = len(list(MIGRATIONS_DIR.glob("*.sql")))
            ver = current_version(conn)
            add("Database schema", OK if ver == want else FAIL, f"version {ver}/{want}")
            add("FTS5", OK if has_fts5(conn) else WARN, "available" if has_fts5(conn) else "غير متوفر: سيُستخدم بحث LIKE أبطأ")
            if deep or os.path.getsize(cfg.db_path) < 512 * 1024 * 1024:
                r = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
                add("Database integrity", OK if r == "ok" else FAIL, r)
            c = conn.execute("SELECT (SELECT COUNT(*) FROM files) f, (SELECT COUNT(*) FROM records) r, (SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')) j").fetchone()
            add("Data", OK, f"files={c['f']} records={c['r']} active_jobs={c['j']}")
        finally:
            # Audit fix (Phase 9/16): the old close() ran only on the happy
            # path — any exception above leaked the connection.
            conn.close()
    except Exception as exc:
        add("Database", FAIL, str(exc)[:120])
    res = ResourceMonitor(cfg)
    snap = res.snapshot()
    add("Storage", OK if snap["free_gb"] >= cfg.min_free_storage_gb else FAIL, f"free {snap['free_gb']} GB (min {cfg.min_free_storage_gb})")
    add("RAM", OK if snap["ram_percent"] is not None else WARN, f"{snap['ram_percent']}%" if snap["ram_percent"] is not None else "غير مقروء (fallback)")
    add("CPU", OK if snap["cpu_percent"] is not None else WARN, f"{snap['cpu_percent']}%" if snap["cpu_percent"] is not None else "غير مقروء (قد يمنعه Android)")
    add("Temperature", OK if snap["temp_c"] is not None else WARN, f"{snap['temp_c']}C" if snap["temp_c"] is not None else "غير متاح - يُستخدم CPU/RAM بدلاً منه")
    bad = []
    for sub in ("data/incoming", "data/originals", "database", "reports", "logs", "backups"):
        p = cfg.base_dir / sub
        if not (p.is_dir() and os.access(p, os.W_OK)):
            bad.append(sub)
    add("Permissions", FAIL if bad else OK, ("غير قابل للكتابة: " + ", ".join(bad)) if bad else "writable")
    add("Telegram config", OK if cfg.telegram_token and cfg.authorized_user_ids else FAIL, "token+users set" if cfg.telegram_token else "TELEGRAM_BOT_TOKEN مفقود")
    add("OpenRouter config", OK if cfg.ai_enabled else WARN, f"model={cfg.openrouter_model}" if cfg.ai_enabled else "AI = Disabled (البحث العادي يعمل)")
    tools = ArchiveManager.tools()
    add("7z (7z/rar)", OK if tools["7z"] else WARN, tools["7z"] or "غير مثبت: pkg install p7zip")
    add("unrar", OK if tools["unrar"] else WARN, tools["unrar"] or "اختياري")
    add("pdftotext (PDF)", OK if tools["pdftotext"] else WARN, tools["pdftotext"] or "غير مثبت: pkg install poppler")
    add("openssl (backup encryption)", OK if tools["openssl"] else WARN, tools["openssl"] or "غير مثبت: النسخ المشفرة غير ممكنة")
    from .lock import InstanceLock
    add("Bot", OK if InstanceLock.is_locked(cfg.base_dir / "run" / "app.lock") else WARN, "يعمل" if InstanceLock.is_locked(cfg.base_dir / "run" / "app.lock") else "متوقف")
    try:
        conn = connect(cfg.db_path)
        try:
            j = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
            add("Jobs", WARN if j.get("failed") or j.get("interrupted") else OK, ", ".join(f"{k}={v}" for k, v in j.items()) or "لا مهام")
            if has_fts5(conn) and conn.execute("SELECT 1 FROM sqlite_master WHERE name='record_fts'").fetchone():
                fts = conn.execute("SELECT COUNT(*) FROM record_fts").fetchone()[0]
                rec = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] if deep else None
                add("Indexes (FTS5)", OK if rec is None or fts >= rec else WARN, f"fts_rows={fts}" + (f" records={rec}" if rec is not None else " (استخدم --deep للمقارنة)"))
        finally:
            conn.close()
    except Exception as exc:
        add("Jobs/Indexes", FAIL, str(exc)[:100])
    try:
        conn = connect(cfg.db_path)
        try:
            u = conn.execute("SELECT SUM(role='user' AND enabled=1), SUM(role='user' AND enabled=0) FROM users").fetchone()
            add("Users", OK, f"ADMIN(.env)={len(cfg.authorized_user_ids)} USER active={u[0] or 0} disabled={u[1] or 0}")
            from .er import entities as _er
            st = _er.statistics(conn)
            pend = st["needs_review"] + st["conflicts"] + st["probable"] + st["possible"]
            add("Entity Resolution", WARN if pend or st["skipped_oversized_blocks"] else OK,
                f"entities={st['entities']} review_items={pend} conflicts={st['conflicts']} skipped_blocks={st['skipped_oversized_blocks']} matcher=v{st['matcher_version']} accuracy=UNVERIFIED(real data)")
            fsn = conn.execute("SELECT (SELECT value FROM settings WHERE key='force_sub_enabled'), (SELECT COUNT(*) FROM force_sub_channels WHERE enabled=1)").fetchone()
            add("Force Subscription", OK, f"{'ON' if fsn[0] == '1' else 'OFF'}, enabled channels={fsn[1]} (وحدة مستقلة؛ ADMIN يتجاوز؛ fail closed)")
            add("Schema mapping review", WARN if st["fields_needing_review"] else OK, f"fields_needing_review={st['fields_needing_review']}")
        finally:
            conn.close()
    except Exception as exc:
        add("Users/ER", WARN, str(exc)[:100])
    env = cfg.base_dir / ".env"
    mode = (env.stat().st_mode & 0o777) if env.exists() else None
    add("Security: admins (.env)", OK if cfg.authorized_user_ids else FAIL, f"{len(cfg.authorized_user_ids)} admin id(s)")
    add("Security: .env perms", OK if mode is not None and mode & 0o077 == 0 else WARN, oct(mode) if mode is not None else ".env غير موجود")
    add("Security: backup passphrase", OK if cfg.backup_passphrase else WARN, "مضبوطة" if cfg.backup_passphrase else "غير مضبوطة: النسخ الاحتياطي معطّل")
    add("Security: DB encryption", WARN, "SQLite غير مشفرة (انظر SECURITY.md)")
    # Audit fix (Phase 1 P2): lazy import so a broken reports module fails at
    # App boot (service._register_reports) with the real error, not here at
    # health-module import time with a confusing traceback.
    from .reports.exporter import resolve_font
    font = resolve_font(cfg)
    add("PDF font", OK if font else WARN, str(font) if font else "لا يوجد خط: PDF بدون عربي")
    return out


def format_checks(checks) -> str:
    icon = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    return "\n".join(f"{icon[s]} {n}: {d}" for n, s, d in checks)


def worst(checks) -> str:
    return FAIL if any(s == FAIL for _, s, _ in checks) else WARN if any(s == WARN for _, s, _ in checks) else OK
