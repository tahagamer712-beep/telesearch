"""Job wiring for Entity Resolution (separate, resumable, auditable job)."""
from __future__ import annotations

import json
import logging

from .. import audit
from ..jobs import JobContext, JobQueue
from . import engine

log = logging.getLogger("pdb.er")


class ERService:
    def __init__(self, cfg, queue: JobQueue, resources=None) -> None:
        self.cfg, self.queue, self.res = cfg, queue, resources
        queue.register("er_match", self.handle)
        queue.register("er_export", self.handle_export)
        queue.register("er_backfill", self.handle_backfill)
        queue.add_listener(self._auto)

    def submit(self, user_id: int | None, rebuild: bool = False, auto_export: bool = False) -> int:
        c = self.queue.conn()
        r = c.execute("SELECT id FROM jobs WHERE kind='er_match' AND status IN ('queued','running','paused','interrupted') ORDER BY id LIMIT 1").fetchone()
        if r:
            return r["id"]  # مهمة مطابقة واحدة فقط في نفس الوقت
        return self.queue.submit("er_match", None, {"rebuild": rebuild, "auto_export": auto_export}, user_id)

    def handle(self, ctx: JobContext) -> None:
        cp = ctx.checkpoint

        def save(c):
            ctx.conn.execute("UPDATE jobs SET checkpoint=? WHERE id=?", (json.dumps(c), ctx.id))
            # v2: تقدم حقيقي اثناء التشغيل (سابقاً بقي 0% حتى النهاية فبدا العمل معلّقاً)
            total = c.get("total_blocks") or 0
            if total:
                ctx.progress(min(99.0, 100.0 * c.get("blocks", 0) / total))
        stats = engine.run_matching(ctx.conn, cp or None, bool(ctx.payload.get("rebuild")), ctx.stopping, save)
        ctx.log("ER: " + json.dumps({k: v for k, v in stats.items() if k != "prepared"}, ensure_ascii=False)[:900])
        audit.log(ctx.conn, ctx.created_by, audit.INDEX, "entity_resolution", ctx.id, {k: stats.get(k) for k in ("compared", "stored", "skipped_blocks", "seconds")})
        ctx.progress(100)

    def handle_export(self, ctx: JobContext) -> None:
        import os
        from datetime import datetime
        from pathlib import Path
        from .export import export_unified
        storage_dir = Path(self.cfg.base_dir) / "reports"
        rid = ctx.conn.execute("INSERT INTO reports(user_id, search_id, format, level, result_count, path, job_id, status) VALUES (?,NULL,'xlsx','unified',0,'',?,'building')", (ctx.created_by or 0, ctx.id)).lastrowid
        path = storage_dir / f"unified_{datetime.now():%Y-%m-%d_%H%M%S}_EXPORT-{rid:06d}.xlsx"
        try:
            info = export_unified(ctx.conn, path, ctx.stopping)
        except BaseException:
            ctx.conn.execute("UPDATE reports SET status='failed' WHERE id=?", (rid,))
            path.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)
        ctx.conn.execute("UPDATE reports SET path=?, size_bytes=?, result_count=?, status='ready' WHERE id=?", (str(path), path.stat().st_size, info["entities"], rid))
        audit.log(ctx.conn, ctx.created_by, audit.EXPORT, "report", rid, {"format": "xlsx", "unified": True, "entities": info["entities"]})
        ctx.log(f"Unified Excel: {info['entities']} كيان، conflicts={info['conflicts']}، needs_review={info['needs_review_items']}")

    def submit_export(self, user_id: int | None) -> int:
        return self.queue.submit("er_export", None, {}, user_id)

    def submit_backfill(self, user_id: int | None, chunk: int | None = None) -> int:
        """Submit (or return the existing) backfill job. One at a time, like er_match."""
        c = self.queue.conn()
        r = c.execute("SELECT id FROM jobs WHERE kind='er_backfill' AND status IN ('queued','running','paused','interrupted') ORDER BY id LIMIT 1").fetchone()
        if r:
            return r["id"]
        payload: dict = {}
        if chunk:
            payload["chunk"] = chunk
        return self.queue.submit("er_backfill", None, payload, user_id)

    def handle_backfill(self, ctx: JobContext) -> None:
        """One chunk of the held review queue per invocation (see autoaccept).

        Resume state travels in the chained job's payload (last_id, done_n,
        total_held, merged, skipped, separated); a retried job re-reads
        its own checkpoint the same way. Per-job progress is per-chunk
        (the queue wrapper forces 100 on done); overall progress is the
        latest backfill job's checkpoint (done_n / total_held). Turning
        the setting OFF mid-backfill makes the next chunk fail cleanly
        with no merges, and chaining stops. Refined policy: each chunk
        merges PROBABLE/POSSIBLE rows and separates NEEDS_REVIEW rows
        (never merged, never queued); CONFLICT rows are never fetched.
        """
        from . import autoaccept
        if not autoaccept.is_enabled(ctx.conn):
            raise RuntimeError("er_auto_accept is off: turn it on from Entity Resolution settings before running the backfill")
        payload = ctx.payload or {}
        cp = ctx.checkpoint or {}
        chunk = int(payload.get("chunk") or cp.get("chunk") or autoaccept.DEFAULT_CHUNK)
        chunk = max(1, min(chunk, autoaccept.MAX_CHUNK))
        last_id = int(payload.get("last_id") or cp.get("last_id") or 0)
        total = int(payload.get("total_held") or cp.get("total_held") or 0)
        if not total:
            total = ctx.conn.execute(
                "SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW')").fetchone()[0]
        stats = autoaccept.apply_backfill_chunk(ctx.conn, chunk, last_id, ctx.stopping)
        done_n = int(payload.get("done_n") or cp.get("done_n") or 0) + stats["processed"]
        merged = int(payload.get("merged") or cp.get("merged") or 0) + stats["merged"]
        skipped = int(payload.get("skipped") or cp.get("skipped") or 0) + stats["skipped"]
        separated = int(payload.get("separated") or cp.get("separated") or 0) + stats["separated"]
        new_state = {"last_id": stats["last_id"], "total_held": total, "done_n": done_n,
                     "merged": merged, "skipped": skipped, "separated": separated, "chunk": chunk}
        ctx.conn.execute("UPDATE jobs SET checkpoint=? WHERE id=?", (json.dumps(new_state), ctx.id))
        if stats["done"]:
            ctx.progress(100)
        else:
            ctx.progress(min(99.0, 100.0 * done_n / total) if total else 0.0)
        ctx.log(f"ER backfill: chunk {stats['processed']} (merged {stats['merged']}, separated {stats['separated']}, skipped {stats['skipped']}), total {done_n}/{total}")
        audit.log(ctx.conn, ctx.created_by, audit.INDEX, "er_backfill", ctx.id,
                  {"chunk": stats["processed"], "merged": stats["merged"], "separated": stats["separated"], "skipped": stats["skipped"],
                   "done_n": done_n, "total_held": total})
        if stats["done"]:
            return
        # Full chunk: re-check the remaining count before chaining, so an
        # exact-multiple total ends here instead of costing one no-op job.
        remaining = ctx.conn.execute(
            "SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state IN"
            " ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW') AND id > ?",
            (stats["last_id"],)).fetchone()[0]
        if remaining == 0:
            return
        # Chain the next chunk to the BACK of the queue: ingests submitted
        # meanwhile run first, so the backfill never monopolizes the worker.
        # Skip chaining if another backfill is already queued (it continues
        # from its own payload; two chains would double-process nothing --
        # rows are claimed by decision -- but one chain is enough).
        other = ctx.conn.execute(
            "SELECT id FROM jobs WHERE kind='er_backfill' AND status IN ('queued','running') AND id != ? ORDER BY id LIMIT 1",
            (ctx.id,)).fetchone()
        if other is None:
            self.queue.submit("er_backfill", None, {**new_state}, ctx.created_by)

    def _auto(self, event: str, ident: int, status: str) -> None:
        """When a top-level file finishes indexing, schedule matching (setting er_auto, default on)."""
        if event != "file" or status not in ("indexed", "partial"):
            return
        try:
            c = self.queue.conn()
            r = c.execute("SELECT value FROM settings WHERE key='er_auto'").fetchone()
            if r is None or r[0] == "1":
                self.submit(None)
        except Exception:
            log.exception("auto ER schedule failed")
