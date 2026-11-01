"""Command line: works with NO Telegram and NO internet (local search, ingest, users, backup...)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import auth
from .backup import create_backup, restore_backup
from .config import load_config
from .errors import AppError, IngestError
from .health import format_checks, run_checks, worst
from .logging_setup import setup_logging
from .search.dsl import SearchQuery, parse_quick
from .search.present import iter_results
from .service import App


def _app(require_telegram: bool = False) -> App:
    cfg = load_config(require_telegram=require_telegram)
    setup_logging(cfg)
    return App(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdb", description="Private Database CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="اضافة ملف ومعالجته")
    p.add_argument("path", type=Path)
    p.add_argument("--allow-duplicate", action="store_true")
    p = sub.add_parser("search", help="بحث محلي")
    p.add_argument("query")
    p.add_argument("--fuzzy", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--export", help="html,txt,pdf")
    p.add_argument("--level", default="evidence", choices=("results", "evidence", "metadata"))
    sub.add_parser("health")
    sub.add_parser("jobs")
    sub.add_parser("run-jobs", help="نفّذ المهام المعلّقة الان (بدون بوت)")
    p = sub.add_parser("user")
    p.add_argument("action", choices=("list", "add", "disable"))
    p.add_argument("telegram_id", type=int, nargs="?")
    sub.add_parser("backup")
    # Audit note (Phase 3/21): restore is TERMINAL-ONLY BY DESIGN — no
    # Telegram button exists for it (restoring replaces the live DB). Kept
    # this way deliberately; do NOT add a button for it.
    p = sub.add_parser("restore")
    p.add_argument("path", type=Path)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "restore":
            print("restored:", restore_backup(load_config(require_telegram=False), a.path))
            return 0
        app = _app()
        db = app.db()
        if a.cmd == "health":
            checks = run_checks(app.cfg, deep=True)
            print(format_checks(checks))
            return 0 if worst(checks) != "FAIL" else 1
        if a.cmd == "ingest":
            try:
                res = app.ingest_path(a.path, 0, a.allow_duplicate)
            except IngestError as exc:
                print(f"ingest refused: {exc}")
                return 1
            print(res.status, res.name, f"id={res.file_id}" if res.file_id else f"duplicate_of={res.duplicate_of}")
            n = app.queue.run_pending()
            if res.file_id:
                r = db.execute("SELECT status, record_count, error FROM files WHERE id=?", (res.file_id,)).fetchone()
                print(f"jobs run: {n} | status={r['status']} records={r['record_count']} {r['error'] or ''}")
            return 0
        if a.cmd in ("run-jobs",):
            print("jobs run:", app.queue.run_pending())
            return 0
        if a.cmd == "jobs":
            for j in db.execute("SELECT id, kind, status, progress, error FROM jobs ORDER BY id DESC LIMIT 20"):
                print(f"JOB-{j['id']:06d} {j['kind']:8} {j['status']:11} {j['progress']:5.1f}% {j['error'] or ''}")
            return 0
        if a.cmd == "search":
            q = parse_quick(a.query)
            q.fuzzy = a.fuzzy
            s = app.search(q, 0, "quick")
            print(f"results={s.total} files={s.files} time={s.seconds:.2f}s")
            for r in iter_results(db, s.search_id, limit=a.limit):
                print(f"#{r.rank} {r.label} {r.label_text} | {r.file_name} | {r.locator}\n   " + " | ".join(f"{n}={v}" for n, _, v in r.fields))
            if a.export:
                admin = auth.User(0, "admin", "cli")
                jid = app.reports.request(admin, s.search_id, a.export.split(","), a.level)
                app.queue.run_pending()
                for rep in app.reports.reports_for_job(db, jid):
                    print("report:", rep["path"])
            return 0
        if a.cmd == "user":
            if a.action == "list":
                for u in db.execute("SELECT telegram_id, role, enabled FROM users"):
                    print(u["telegram_id"], u["role"], "enabled" if u["enabled"] else "disabled")
            else:
                print('ERROR: ادارة المستخدمين تتم من Admin Panel في Telegram فقط (ADMIN ids من .env).', file=sys.stderr)
                return 2
            return 0
        if a.cmd == "backup":
            print("backup:", create_backup(app.cfg, db))
            return 0
    except AppError as exc:
        print("ERROR:", exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
