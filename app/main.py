"""Service entrypoint: python -m app.main"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from . import maintenance
from .backup import create_backup
from .config import load_config
from .errors import AppError, BackupError
from .ingest.watcher import IncomingWatcher
from .lock import AlreadyRunning, InstanceLock
from .logging_setup import setup_logging
from .service import App
from .telegram.api import TelegramAPI, TelegramError
from .telegram.bot import Bot


def main() -> int:
    try:
        cfg = load_config(require_telegram=True)
    except AppError as exc:
        print(f"❌ خطأ في الإعدادات: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)
    log = logging.getLogger("pdb.main")
    lock = InstanceLock(cfg.base_dir / "run" / "app.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        print("⚠️ Bot already running — لن اشغل نسخة ثانية (تجنباً لـTelegram Conflict).", file=sys.stderr)
        return 3
    app = App(cfg)
    stop = threading.Event()

    pid = cfg.base_dir / "run" / "app.pid"
    pid.parent.mkdir(exist_ok=True)
    pid.write_text(str(os.getpid()))

    api = TelegramAPI(cfg.telegram_token, cfg.telegram_api_base, timeout=cfg.telegram_poll_timeout + 15)
    bot = Bot(app, api)
    watcher = IncomingWatcher(app, bot.on_watcher_event)
    resumed = app.queue.recover()
    app.queue.start()
    watcher.start()
    log.info("started (pid=%s, workers=%s, ai=%s, resumed_jobs=%s)", os.getpid(), cfg.max_workers, "on" if cfg.ai_enabled else "off", resumed)

    def maint() -> None:
        last_backup = time.time()
        while not stop.wait(3600):
            try:
                maintenance.run_retention(app.db(), app.storage, cfg)
                if cfg.auto_backup_hours and time.time() - last_backup > cfg.auto_backup_hours * 3600:
                    create_backup(cfg, app.db())
                    last_backup = time.time()
            except BackupError as exc:
                log.warning("auto backup skipped: %s", exc)
            except Exception:
                log.exception("maintenance failed")

    threading.Thread(target=maint, name="maint", daemon=True).start()

    def shutdown(signum, frame) -> None:  # SIGINT/SIGTERM: لا مهام جديدة، احفظ الحالة، اغلق
        log.info("signal %s: shutting down", signum)
        stop.set()
        bot.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        me = api.call("getMe")
        log.info("telegram bot: @%s", me.get("username"))
    except TelegramError as exc:
        log.warning("getMe failed (offline?): %s - البوت سيعيد المحاولة", exc)
    bot.publish_commands()

    def sched() -> None:
        while not stop.wait(30):
            try:
                app.notify.tick()
            except Exception:
                log.exception("notification tick failed")

    threading.Thread(target=sched, name="notify-scheduler", daemon=True).start()
    t = threading.Thread(target=bot.run, name="bot", daemon=True)
    t.start()
    stop.wait()
    watcher.stop()
    app.queue.stop(timeout=60)
    pid.unlink(missing_ok=True)
    lock.release()
    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
