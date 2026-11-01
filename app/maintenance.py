"""Retention + cleanup (never touches originals/)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .config import Config
from .storage import StorageManager


def _rm_children(d: Path, older_than_days: float = 0.0) -> int:
    n = 0
    cutoff = time.time() - older_than_days * 86400
    for p in d.iterdir() if d.exists() else []:
        try:
            if p.stat().st_mtime > cutoff and older_than_days:
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            n += 1
        except OSError:
            pass
    return n


def clean_temporary(storage: StorageManager) -> int:
    return _rm_children(storage.dir("temporary"), 0.0)


def clean_cache(storage: StorageManager) -> int:
    return _rm_children(storage.dir("cache"), 0.0)


def clean_failed_extraction(conn, storage: StorageManager) -> int:
    n = 0
    for r in conn.execute("SELECT id FROM files WHERE status IN ('failed','quarantined') AND parent_file_id IS NULL"):
        d = storage.dir("extracted") / str(r["id"])
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def clean_old_reports(conn, storage: StorageManager, days: int) -> int:
    if days <= 0:
        return 0
    n = 0
    for r in conn.execute("SELECT id, path FROM reports WHERE ts < strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)", (f"-{days} days",)).fetchall():
        Path(r["path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM reports WHERE id=?", (r["id"],))
        n += 1
    return n


def purge_search_history(conn, days: int) -> int:
    if days <= 0:
        return 0
    cur = conn.execute("DELETE FROM search_history WHERE ts < strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)", (f"-{days} days",))
    conn.execute("DELETE FROM callback_state WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')")
    return cur.rowcount


def run_retention(conn, storage: StorageManager, cfg: Config) -> dict:
    return {"reports": clean_old_reports(conn, storage, cfg.report_retention_days),
            "searches": purge_search_history(conn, cfg.search_history_retention_days)}
