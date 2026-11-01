"""Persistent job queue (SQLite). Survives restarts; interrupted jobs resume from checkpoint."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import threading
import traceback
from typing import Callable

from .config import Config
from .db.connection import connect
from .errors import JobInterrupted

log = logging.getLogger("pdb.jobs")
TERMINAL = ("done", "failed", "cancelled")
# Phase 10: bounded auto-retry for transient job failures. Only kinds whose
# handlers are safe to re-run (export/er/master rebuild output files; a
# failed attempt leaves at most a "failed" report row, never a delivery).
# ingest IS retryable (2026-10-29 lockfix, proven by code): a fresh
# attempt purges the file first (pipeline._data calls
# Indexer.delete_file_records when the job checkpoint is empty), and a
# partial attempt resumes from the checkpoint instead of re-adding rows
# (indexer._commit writes the checkpoint in the SAME transaction as each
# batch, and the parser skip is deterministic) -- so a re-run cannot
# duplicate rows. notify stays excluded (a timed-out send may already
# have been delivered).
RETRYABLE_KINDS = ("export", "er_match", "er_export", "master_excel", "er_person_export", "ingest")
# File-producing export kinds. These run on the DEDICATED export lane
# (one worker, see JobQueue.start): an export job starts promptly even
# while ingests run, and exports serialize among themselves, so report
# builds and master part-jobs can never overlap or duplicate parts.
EXPORT_KINDS = ("export", "er_export", "er_person_export", "master_excel")
TRANSIENT_PATTERNS = ("locked", "timed out", "timeout", "ssl", "connection", "reset by peer", "temporarily")
MAX_JOB_RETRIES = 2


def is_transient_error(err: str) -> bool:
    e = (err or "").lower()
    return any(p in e for p in TRANSIENT_PATTERNS)


def retryable_kind(kind: str) -> bool:
    return kind in RETRYABLE_KINDS


class ProgressETA:
    """Progress + ETA estimator for long jobs (exports, merges).

    Pure (no DB): total work units in, (pct, eta) out. ETA comes from
    the MEASURED rate only: before enough progress exists it returns
    None ("measuring"), never a fabricated time. As progress advances
    the ETA refines from the recent rate.
    """

    def __init__(self, total: int, now_fn=None) -> None:
        self.total = max(0, int(total))
        self._now = now_fn or time.time
        self._samples: list[tuple[float, int]] = []  # (timestamp, done)
        self._last_log_pct = -1.0
        self._last_log_t = 0.0

    def update(self, done: int):
        """Record progress; return (pct 0-100, eta datetime or None)."""
        from datetime import datetime
        done = max(0, min(int(done), self.total)) if self.total else 0
        now = self._now()
        self._samples.append((now, done))
        if len(self._samples) > 20:
            self._samples.pop(0)
        pct = (100.0 * done / self.total) if self.total else 0.0
        eta = None
        if self.total and done > 0 and len(self._samples) >= 2:
            t0, d0 = self._samples[0]
            span = now - t0
            moved = done - d0
            # Require a real measurement window (>=2s and >=1% of total
            # or >=200 units) before trusting the rate.
            if span >= 2.0 and moved >= max(200, self.total // 100):
                rate = moved / span  # units per second
                if rate > 0:
                    eta = datetime.fromtimestamp(now + (self.total - done) / rate)
        if done >= self.total and self.total:
            pct, eta = 100.0, datetime.fromtimestamp(now)
        return pct, eta

    def should_log(self, pct: float) -> bool:
        """Throttle ETA log lines: every 10 pct points or 30 seconds."""
        now = self._now()
        if pct >= self._last_log_pct + 10.0 or now - self._last_log_t >= 30.0:
            self._last_log_pct = pct
            self._last_log_t = now
            return True
        return False



class JobContext:
    def __init__(self, queue: "JobQueue", job, conn) -> None:
        self.queue, self.job, self.conn = queue, job, conn
        self.id = job["id"]
        self.kind = job["kind"]
        self.payload = json.loads(job["payload"]) if job["payload"] else {}
        self.file_id = job["file_id"]
        self.created_by = job["created_by"]
        # Optional progress callback set by export handlers; _iter calls
        # it every 200 rows. None by default (no behavior change).
        self.tick = None

    @property
    def checkpoint(self) -> dict:
        row = self.conn.execute("SELECT checkpoint FROM jobs WHERE id=?", (self.id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def log(self, message: str, level: str = "info") -> None:
        self.conn.execute("INSERT INTO job_events(job_id, level, message) VALUES (?,?,?)", (self.id, level, message[:1000]))

    def progress(self, pct: float) -> None:
        self.conn.execute("UPDATE jobs SET progress=? WHERE id=?", (round(pct, 1), self.id))

    def stopping(self) -> bool:
        return self.queue.stop_event.is_set() or self.id in self.queue.controls

    def check(self) -> None:
        if self.stopping():
            raise JobInterrupted(self.queue.reason_for(self.id))


class JobQueue:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.handlers: dict[str, Callable[[JobContext], None]] = {}
        self.stop_event = threading.Event()
        self.controls: dict[int, str] = {}
        self.listeners: list[Callable[[str, int, str], None]] = []
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()
        self._local = threading.local()

    # ---- connections
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self.cfg.db_path)
        return c

    def close(self) -> None:
        """Close the calling thread's queue connection, if any. Idempotent."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    def register(self, kind: str, handler) -> None:
        self.handlers[kind] = handler

    def add_listener(self, fn) -> None:
        self.listeners.append(fn)

    def notify(self, event: str, ident: int, status: str) -> None:
        for fn in self.listeners:
            try:
                fn(event, ident, status)
            except Exception:
                log.exception("listener failed")

    # ---- API
    def submit(self, kind: str, file_id: int | None = None, payload: dict | None = None, created_by: int | None = None) -> int:
        # Retry on lock: the ER build holds short write windows; a submit must
        # not fail the user's button press on transient contention (2026-10-26).
        last: Exception | None = None
        for attempt in range(3):
            try:
                cur = self.conn().execute("INSERT INTO jobs(kind, file_id, payload, created_by) VALUES (?,?,?,?)",
                                          (kind, file_id, json.dumps(payload) if payload else None, created_by))
                self._wake.set()
                return cur.lastrowid
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def recover(self) -> int:
        c = self.conn()
        c.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
        n = 0
        if self.cfg.auto_resume:
            for r in c.execute("SELECT id FROM jobs WHERE status='interrupted'").fetchall():
                c.execute("UPDATE jobs SET status='queued' WHERE id=?", (r["id"],))
                c.execute("INSERT INTO job_events(job_id, level, message) VALUES (?,?,?)", (r["id"], "info", "استئناف تلقائي بعد اعادة التشغيل"))
                n += 1
        return n

    def control(self, job_id: int, action: str) -> str:
        c = self.conn()
        row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return "not_found"
        st = row["status"]
        if action == "pause":
            if st == "running":
                self.controls[job_id] = "pause"
            elif st == "queued":
                c.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
        elif action == "cancel":
            if st == "running":
                self.controls[job_id] = "cancel"
            elif st in ("queued", "paused", "interrupted"):
                c.execute("UPDATE jobs SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (job_id,))
        elif action in ("resume", "retry"):
            if st in ("paused", "interrupted", "failed", "cancelled"):
                c.execute("UPDATE jobs SET status='queued', error=NULL, finished_at=NULL WHERE id=?", (job_id,))
                self._wake.set()
        return self.conn().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]

    def reason_for(self, job_id: int) -> str:
        return self.controls.get(job_id) or ("shutdown" if self.stop_event.is_set() else "stop")

    # ---- workers
    def start(self, workers: int | None = None) -> None:
        """Start the pool: N general workers + ONE dedicated export worker.

        General workers handle ingest/er_match/notify/etc. The export
        worker handles ONLY export kinds, so an export starts promptly
        even while ingests run, and exports serialize among themselves
        (no overlapping report builds, no duplicate master parts).
        Threads share the process heap, so the extra thread costs
        almost no memory on the phone; the GIL means this fixes queue
        starvation, not linear CPU speedup.
        """
        n = max(1, min(int(workers or self.cfg.max_workers), 8))
        for i in range(n):
            t = threading.Thread(target=self._loop, kwargs={"lane": "general"}, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._loop, kwargs={"lane": "export"}, name="worker-export", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)

    def run_pending(self) -> int:
        """Run queued jobs synchronously in the calling thread (CLI / tests)."""
        n = 0
        while (job := self._claim()) is not None:
            self._run(job)
            n += 1
        return n

    def _claim(self, lane: str | None = None):
        """Atomically claim the oldest queued job for a lane.

        lane=None: any kind (CLI/tests via run_pending).
        lane="general": any kind EXCEPT export kinds.
        lane="export": only export kinds.
        The UPDATE...RETURNING is atomic, so two workers can never
        claim the same job even under concurrency.
        """
        if lane == "export":
            filt, params = "AND kind IN (?,?,?,?)", EXPORT_KINDS
        elif lane == "general":
            filt, params = "AND kind NOT IN (?,?,?,?)", EXPORT_KINDS
        else:
            filt, params = "", ()
        # Lockfix 2026-10-29: a dense er_match block holds BEGIN IMMEDIATE
        # for minutes, so a claim UPDATE can hit "database is locked" even
        # with the 300s busy timeout. Retry the claim itself (warning per
        # attempt, never a traceback per attempt) instead of failing the
        # claim outright. Budget: 3 attempts; each attempt already waits up
        # to busy_timeout inside SQLite, so total coverage is ~15 min
        # before _loop's own retry takes over (workers never die).
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self.conn().execute(
                    "UPDATE jobs SET status='running', started_at=COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    f"WHERE id=(SELECT id FROM jobs WHERE status='queued' {filt} ORDER BY id LIMIT 1) RETURNING *",
                    params).fetchone()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                last = exc
                if attempt < 2:
                    log.warning("claim locked, retry %s/3 after backoff", attempt + 1)
                    time.sleep(2.0 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _loop(self, lane: str = "general") -> None:
        while not self.stop_event.is_set():
            try:
                job = self._claim(lane)
            except Exception:
                log.exception("claim failed")
                self.stop_event.wait(2)
                continue
            if job is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job) -> None:
        c = self.conn()
        ctx = JobContext(self, job, c)
        status, err = "done", None
        attempts = 0
        try:
            while True:
                attempts += 1
                try:
                    handler = self.handlers.get(job["kind"])
                    if handler is None:
                        raise RuntimeError(f"لا يوجد handler للنوع {job['kind']}")
                    handler(ctx)
                    status, err = "done", None
                    break
                except JobInterrupted as exc:
                    reason = self.controls.get(job["id"]) or ("shutdown" if self.stop_event.is_set() else exc.reason)
                    if reason == "cancel":
                        status = "cancelled"
                    elif reason == "pause" or reason.startswith("paused"):
                        status, err = "paused", (reason if reason.startswith("paused") else None)
                    else:
                        status = "interrupted"
                    ctx.log(f"توقف: {reason}", "warn")
                    break
                except Exception as exc:
                    status, err = "failed", f"{type(exc).__name__}: {exc}"[:500]
                    log.error("job %s failed\n%s", job["id"], traceback.format_exc())
                    try:
                        ctx.log(err, "error")
                    except Exception:
                        pass
                    if (retryable_kind(job["kind"]) and is_transient_error(err)
                            and attempts <= MAX_JOB_RETRIES):
                        ctx.log(f"إعادة محاولة {attempts}/{MAX_JOB_RETRIES} بعد خطأ مؤقت", "warn")
                        time.sleep(0.5 * attempts)
                        continue
                    break
        finally:
            self.controls.pop(job["id"], None)
        fin = "strftime('%Y-%m-%dT%H:%M:%fZ','now')" if status in TERMINAL else "NULL"
        c.execute(f"UPDATE jobs SET status=?, error=?, finished_at={fin} WHERE id=?", (status, err, job["id"]))
        if status == "done":
            c.execute("UPDATE jobs SET progress=100 WHERE id=?", (job["id"],))
        self.notify("job", job["id"], status)
