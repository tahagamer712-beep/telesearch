"""Ingestion pipeline handlers: ingest (classify -> extract -> parse -> index -> verify)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from . import audit
from .archive.manager import ArchiveManager
from .config import Config
from .errors import ArchiveError, InsufficientSpaceError, JobInterrupted, ParserUnavailableError, ToolUnavailableError
from .index.indexer import Indexer
from .ingest.classify import ARCHIVE_KINDS, classify
from .ingest.intake import register_extracted, stored_source
from .jobs import JobContext, JobQueue
from .parsers.registry import get_parser_class
from .resources import ResourceMonitor
from .storage import StorageManager

log = logging.getLogger("pdb.pipeline")
TERMINAL_FILE = ("indexed", "partial", "failed", "quarantined", "unsupported")


class Pipeline:
    def __init__(self, config: Config, storage: StorageManager, queue: JobQueue, resources: ResourceMonitor | None = None) -> None:
        self.cfg, self.storage, self.queue, self.res = config, storage, queue, resources
        self.archives = ArchiveManager(config)
        queue.register("ingest", self.handle_ingest)

    # ------------------------------------------------------------ helpers
    def _set(self, conn, file_id: int, status: str, error: str | None = None, warnings: list[str] | None = None, parser: str | None = None) -> None:
        sets, args = ["status=?", "error=?"], [status, error]
        if warnings is not None:
            sets.append("warnings=?")
            args.append(json.dumps(warnings, ensure_ascii=False) if warnings else None)
        if parser:
            sets.append("parser=?")
            args.append(parser)
        if status in ("indexed", "partial"):
            sets.append("indexed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        conn.execute(f"UPDATE files SET {', '.join(sets)} WHERE id=?", (*args, file_id))
        row = conn.execute("SELECT parent_file_id FROM files WHERE id=?", (file_id,)).fetchone()
        if status in TERMINAL_FILE:
            if row["parent_file_id"]:
                self.refresh_parent(conn, row["parent_file_id"])
                self.queue.notify("file", file_id, status)
            else:
                self.queue.notify("file", file_id, status)

    def refresh_parent(self, conn, parent_id: int) -> None:
        kids = conn.execute("SELECT status, record_count FROM files WHERE parent_file_id=?", (parent_id,)).fetchall()
        if not kids or any(k["status"] not in TERMINAL_FILE for k in kids):
            return
        prow = conn.execute("SELECT status, warnings FROM files WHERE id=?", (parent_id,)).fetchone()
        ok = sum(1 for k in kids if k["status"] == "indexed")
        bad = sum(1 for k in kids if k["status"] in ("failed", "quarantined"))
        unsup = sum(1 for k in kids if k["status"] == "unsupported")
        part = sum(1 for k in kids if k["status"] == "partial")
        warn = json.loads(prow["warnings"]) if prow["warnings"] else []
        arch = conn.execute("SELECT status FROM archives WHERE file_id=? ORDER BY id DESC LIMIT 1", (parent_id,)).fetchone()
        clean = not arch or arch["status"] == "extracted"
        if ok == len(kids) and clean:
            status, err = "indexed", None
        elif ok + part == 0:
            status, err = "failed", f"لم ينجح اي ملف داخل الارشيف ({bad} فشل، {unsup} غير مدعوم)"
        else:
            status, err = "partial", f"{ok} نجح، {part} جزئي، {bad} فشل، {unsup} غير مدعوم"
        total = sum(k["record_count"] for k in kids)
        conn.execute("UPDATE files SET record_count=? WHERE id=?", (total, parent_id))
        self._set(conn, parent_id, status, err)

    def quarantine(self, conn, file_id: int, reason: str) -> None:
        src, row = stored_source(conn, file_id)
        paths = src if isinstance(src, list) else [src]
        qdir = self.storage.dir("quarantine") / f"file_{file_id}"
        qdir.mkdir(parents=True, exist_ok=True)
        newp = []
        for p in paths:
            if p.exists():
                dst = qdir / p.name
                shutil.move(str(p), str(dst))
                newp.append(dst)
        if newp:
            conn.execute("UPDATE files SET stored_path=? WHERE id=?", (str(newp[0]), file_id))
            if isinstance(src, list):
                for i, p in enumerate(newp, 1):
                    conn.execute("UPDATE file_parts SET path=? WHERE file_id=? AND part_index=?", (str(p), file_id, i))
        self._set(conn, file_id, "quarantined", reason)
        audit.log(conn, None, audit.INDEX, "file", file_id, {"quarantine": reason[:200]})

    # ------------------------------------------------------------ handler
    def handle_ingest(self, ctx: JobContext) -> None:
        conn = ctx.conn
        fid = ctx.file_id
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if row is None:
            raise RuntimeError(f"file {fid} غير موجود")
        kind = row["detected_type"]
        ctx.log(f"بدء المعالجة: {row['original_name']} ({kind})")
        conn.execute("UPDATE files SET status='processing', error=NULL WHERE id=?", (fid,))
        # Heal stale types (2026-10-29 dbformat): files stored before their
        # parser existed (old "unknown" .doc/.mdb/.accdb) are re-classified
        # by content; if the fresh kind differs, the stored type heals and
        # dispatch follows the fresh kind. Normal files skip this entirely.
        if kind not in ARCHIVE_KINDS and get_parser_class(kind) is None:
            src_try, _ = stored_source(conn, fid)
            if not isinstance(src_try, list) and Path(src_try).exists():
                fresh = classify(src_try, name=row["original_name"])
                if fresh.kind != kind:
                    conn.execute(
                        "UPDATE files SET detected_type=?, mime=?, warnings=? WHERE id=?",
                        (fresh.kind, fresh.mime, json.dumps(fresh.notes, ensure_ascii=False) if fresh.notes else None, fid))
                    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
                    kind = row["detected_type"]
        try:
            if kind in ARCHIVE_KINDS:
                self._archive(ctx, row)
            else:
                self._data(ctx, row)
        except JobInterrupted as exc:
            if exc.reason.startswith("paused"):
                why = {"paused:storage": "⏸ متوقف: مساحة التخزين اقل من الحد الآمن (MIN_FREE_STORAGE_GB)", "paused:quota": "⏸ متوقف: تجاوز MAX_STORAGE_USAGE_GB"}.get(exc.reason, exc.reason)
                conn.execute("UPDATE files SET error=? WHERE id=?", (why, fid))
            raise  # يبقى processing وسيُستأنف من checkpoint
        except Exception as exc:
            self._set(conn, fid, "failed", f"{type(exc).__name__}: {exc}"[:300])
            raise

    # ------------------------------------------------------------ archives
    def _depth(self, conn, file_id: int) -> int:
        d = 0
        cur = conn.execute("SELECT parent_file_id FROM files WHERE id=?", (file_id,)).fetchone()
        while cur and cur["parent_file_id"]:
            d += 1
            cur = conn.execute("SELECT parent_file_id FROM files WHERE id=?", (cur["parent_file_id"],)).fetchone()
        return d

    def _archive(self, ctx: JobContext, row) -> None:
        conn, fid, kind = ctx.conn, row["id"], row["detected_type"]
        if self._depth(conn, fid) >= self.cfg.max_archive_depth:
            self.quarantine(conn, fid, f"عمق الارشيفات المتداخلة تجاوز الحد ({self.cfg.max_archive_depth})")
            return
        if self.res:
            self.res.gate(ctx.stopping)
        src, _ = stored_source(conn, fid)
        dest = self.storage.dir("extracted") / str(fid)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)  # الاستخراج غير قابل للاستئناف: نبدأ نظيفاً
        indexer = Indexer(conn, self.cfg, self.res)
        for kid in [r["id"] for r in conn.execute("SELECT id FROM files WHERE parent_file_id=?", (fid,))]:
            indexer.delete_file_records(kid)  # يزيل FTS ايضاً قبل حذف صف الملف
        conn.execute("DELETE FROM files WHERE parent_file_id=?", (fid,))
        try:
            res = self.archives.extract(src, kind, dest, stop=ctx.check)
        except ToolUnavailableError as exc:
            self._set(conn, fid, "unsupported", str(exc))
            ctx.log(str(exc), "warn")
            return
        except InsufficientSpaceError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            self._set(conn, fid, "failed", f"⚠️ مساحة غير كافية. المطلوب: {exc.required / 1024**3:.2f}GB المتاح: {exc.available / 1024**3:.2f}GB")
            raise
        except ArchiveError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            self.quarantine(conn, fid, f"ارشيف تالف/مشبوه: {exc}")
            return
        except JobInterrupted:
            raise
        warnings = list(res.warnings) + [f"{n}: {why}" for n, why in res.skipped]
        conn.execute("INSERT INTO archives(file_id, format, member_count, uncompressed_bytes, extract_dir, status) VALUES (?,?,?,?,?,?)",
                     (fid, kind, len(res.files), sum(f.size for f in res.files), str(dest), "extracted" if not (res.skipped or res.suspicious) else "extracted_with_skips"))
        if res.suspicious:
            warnings.append("تحذير: الارشيف يحتوي مسارات/روابط غير آمنة (تم رفضها)")
        if not res.files:
            why = "؛ ".join(f"{nm}: {r}" for nm, r in res.skipped[:3])
            self._set(conn, fid, "failed", ("الارشيف لا يحتوي ملفات قابلة للاستخراج" + (f" — {why}" if why else ""))[:300], warnings)
            return
        child_ids = []
        for ef in res.files:
            cid = register_extracted(conn, ef.path, ef.rel, fid, self.cfg.chunk_size)
            child_ids.append(cid)
        conn.execute("UPDATE files SET warnings=?, status='processing' WHERE id=?", (json.dumps(warnings, ensure_ascii=False) if warnings else None, fid))
        for cid in child_ids:
            st = conn.execute("SELECT status FROM files WHERE id=?", (cid,)).fetchone()["status"]
            if st == "unsupported":
                continue
            conn.execute("UPDATE files SET status='queued' WHERE id=?", (cid,))
            self.queue.submit("ingest", cid, created_by=ctx.created_by)
        ctx.log(f"تم استخراج {len(res.files)} ملف، تخطي {len(res.skipped)}")
        self.refresh_parent(conn, fid)

    # ------------------------------------------------------------ data files
    def _data(self, ctx: JobContext, row) -> None:
        conn, fid = ctx.conn, row["id"]
        cls = get_parser_class(row["detected_type"])
        if cls is None:
            self._set(conn, fid, "unsupported", f"لا يوجد Parser للصيغة {row['detected_type']} (الملف محفوظ كما هو)")
            return
        src, _ = stored_source(conn, fid)
        if isinstance(src, list) or not Path(src).exists():
            # Audit fix (Phase 5 A27b): a split NON-archive file used to fail
            # with 're-index the parent archive' — there is no parent archive.
            # Say what actually happened and what to do instead.
            if isinstance(src, list) and row["detected_type"] not in ARCHIVE_KINDS:
                self._set(conn, fid, "failed", "الأجزاء المحفوظة ليست أرشيفاً معروفاً؛ ادمجها في ملف واحد ثم أعد إرساله (التجزئة .001 مدعومة للأرشيفات فقط)")
            else:
                self._set(conn, fid, "failed", "الملف المصدر غير موجود (ربما حُذف بعد الفهرسة). اعد فهرسة الارشيف الاب")
            return
        info = classify(src, name=row["original_name"])
        parser = cls(src, info, self.cfg)
        indexer = Indexer(conn, self.cfg, self.res)
        cp = ctx.checkpoint
        if not cp:
            indexer.delete_file_records(fid)
        else:
            ctx.log(f"استئناف من السجل {cp.get('done')}")

        try:
            stats = indexer.index_file(fid, parser, cp, ctx.stopping, None)
        except ParserUnavailableError as exc:
            self._set(conn, fid, "unsupported", str(exc), parser=cls.name)
            return
        except JobInterrupted:
            raise
        finally:
            parser.close()
        warnings = list(info.notes) + list(stats.get("warnings", []))
        n = stats["records"]
        ok, why = indexer.verify(fid, n)
        if os.path.getsize(self.cfg.db_path) < 512 * 1024 * 1024:
            qc = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            if qc != "ok":
                ok, why = False, f"quick_check: {qc}"
        if not ok:
            warnings.append(f"فشل التحقق: {why}")
        if "parser_error" in stats:
            status, err = ("partial", stats["parser_error"]) if n else ("failed", stats["parser_error"])
        elif n == 0:
            status, err = "partial", "لم يُستخرج اي سجل من الملف"
        elif not ok or stats["errors"]:
            status, err = "partial", why if not ok else f"{stats['errors']} خطأ/سطر تالف"
        else:
            status, err = "indexed", None
        conn.execute("UPDATE files SET record_count=? WHERE id=?", (n, fid))
        self._set(conn, fid, status, err, warnings, cls.name)
        audit.log(conn, ctx.created_by, audit.INDEX, "file", fid, {"status": status, "records": n})
        # Audit fix (Phase 7/18): migration 010's fast ER-build plan only
        # holds while sqlite_stat1 is fresh — on a fresh DB the planner falls
        # back to the slow field-first plan (0.349s vs 0.088s per chunk at
        # 10k records, 180x on production). Refresh statistics after bulk
        # ingests (>=1000 records); never fail the job for ANALYZE itself.
        if status in ("indexed", "partial") and n >= 1000:
            try:
                _t = time.time()
                conn.execute("ANALYZE")
                ctx.log(f"ANALYZE بعد الفهرسة ({n} سجل): {time.time() - _t:.2f}s")
            except Exception as exc:
                ctx.log(f"ANALYZE تخطي: {exc}", "warn")
        ctx.progress(100)
        if status in ("indexed", "partial") and row["parent_file_id"] and not self.cfg.keep_extracted:
            try:
                Path(src).unlink()  # نسخة مشتقة مؤقتة؛ الاصل (الارشيف) باقٍ
            except OSError:
                pass
