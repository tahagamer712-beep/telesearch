"""Indexer: parser records -> records/record_values/FTS/vocab, in atomic batches.
The checkpoint is written in the SAME transaction as each batch, so a crash can
never leave a batch half-applied or a checkpoint that lies."""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
from collections import Counter
from typing import Callable

from ..config import Config
from ..db.connection import has_fts5
from ..errors import JobInterrupted, ParserError
from ..ingest.classify import FileInfo
from ..norm.arabic import normalize_text
from ..norm.values import detect_role, normalize_value
from ..parsers.base import Parser, Record
from ..resources import ResourceMonitor

log = logging.getLogger("pdb.indexer")
SAMPLE = 200
# lockfix-v4 (2026-10-30): delete_file_records يُنفَّذ لملف واحد فقط في كل مرة
# داخل العملية. كل دفعة حذف معاملة كتابة طويلة (cascade + FTS + ER + search_groups)،
# وتشغيل 3 عمال معًا كان يجعل الثالث ينتظر > busy_timeout (300s) فيفشل بـ"database is locked".
_DELETE_LOCK = threading.Lock()
_SLOW_SEC = 5.0  # أي انتظار/احتجاز أطول من هذا يُسجَّل تحذيراً (لكشف حامل القفل)
DELETE_CHUNK = 1000
_TOKEN_OK = re.compile(r"^[^\W\d_]{2,32}$", re.UNICODE)

FTS_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5(body, tokenize='unicode61 remove_diacritics 0')"


def ensure_fts(conn: sqlite3.Connection) -> bool:
    if not has_fts5(conn):
        return False
    conn.execute(FTS_DDL)
    # الحل الجذري: records.id بدون AUTOINCREMENT فيُعاد استخدام الأرقام بعد الحذف،
    # وأي صف يتيم بـrecord_fts بنفس rowid يطلّع "constraint failed".
    # التريغر يمسح صف FTS مع كل حذف لسجل (حتى الحذف المتسلسل CASCADE من files).
    try:
        conn.execute("CREATE TRIGGER IF NOT EXISTS records_fts_ad AFTER DELETE ON records "
                     "BEGIN DELETE FROM record_fts WHERE rowid = old.id; END")
    except sqlite3.Error:
        log.exception("fts orphan trigger not created")
    return True


def field_name(parser: Parser, dataset: str, col: str) -> str:
    return f"{dataset}.{col}" if parser.multi_dataset else col


class Indexer:
    def __init__(self, conn: sqlite3.Connection, config: Config, resources: ResourceMonitor | None = None) -> None:
        self.conn, self.cfg, self.res = conn, config, resources
        self.fts = ensure_fts(conn)

    # ---- fields
    def _load_fields(self, file_id: int) -> dict[str, tuple[int, str]]:
        return {r["name"]: (r["id"], r["detected_type"] or "text")
                for r in self.conn.execute("SELECT id, name, detected_type FROM fields WHERE file_id=?", (file_id,))}

    def _ensure_field(self, file_id: int, name: str, role: str, cache: dict) -> tuple[int, str]:
        if name in cache:
            return cache[name]
        pos = len(cache)
        self.conn.execute("INSERT OR IGNORE INTO fields(file_id, name, position, detected_type) VALUES (?,?,?,?)", (file_id, name, pos, role))
        row = self.conn.execute("SELECT id, detected_type FROM fields WHERE file_id=? AND name=?", (file_id, name)).fetchone()
        cache[name] = (row["id"], row["detected_type"] or "text")
        return cache[name]

    # ---- main entry
    def index_file(self, file_id: int, parser: Parser, checkpoint: dict | None, stopping: Callable[[], bool],
                   on_progress: Callable[[int, dict], None] | None = None) -> dict:
        """Index everything the parser yields. Returns stats dict. Raises JobInterrupted."""
        done = int((checkpoint or {}).get("done", 0))
        base_count = int((checkpoint or {}).get("records", 0))
        fields = self._load_fields(file_id)
        decided: dict[str, dict[str, str]] = {}  # dataset -> column -> role (نقرأها من DB عند الاستئناف)
        for fname, (_, role) in fields.items():
            ds, _, col = fname.partition(".") if parser.multi_dataset else ("main", "", fname)
            decided.setdefault(ds, {})[col] = role
        ordinal = done
        stats = {"records": base_count, "errors": 0}
        batch: list[tuple[int, Record]] = []
        sample_ds: str | None = None
        sample_buf: list[tuple[int, Record]] = []

        def decide_roles() -> None:
            nonlocal sample_ds, sample_buf
            if sample_ds is None:
                return
            cols: dict[str, list] = {}
            for _, r in sample_buf:
                for c, v in r.values.items():
                    cols.setdefault(c, []).append(v)
            roles = decided.setdefault(sample_ds, {})
            for c, vals in cols.items():
                role = detect_role(c, vals)
                roles[c] = role
                self._ensure_field(file_id, field_name(parser, sample_ds, c), role, fields)
            batch.extend(sample_buf)
            sample_ds, sample_buf = None, []

        def flush() -> None:
            if not batch:
                return
            last_ord = batch[-1][0]
            self._commit(file_id, parser, batch, fields, decided, last_ord, stats)
            batch.clear()
            if on_progress:
                on_progress(stats["records"], {"done": last_ord, "records": stats["records"]})
            if self.res:
                self.res.gate(stopping)
            if stopping():
                raise JobInterrupted("stop")

        try:
            for rec in parser.records(skip=done):
                ordinal += 1
                if rec.dataset not in decided:
                    if sample_ds is not None and sample_ds != rec.dataset:
                        decide_roles()
                    sample_ds = rec.dataset
                    sample_buf.append((ordinal, rec))
                    if len(sample_buf) >= SAMPLE:
                        decide_roles()
                else:
                    if sample_ds is not None:
                        decide_roles()
                    batch.append((ordinal, rec))
                if len(batch) >= self.cfg.batch_size:
                    flush()
            decide_roles()
            flush()
        except JobInterrupted:
            raise
        except ParserError as exc:
            decide_roles()
            flush()
            stats["parser_error"] = str(exc)
        stats["errors"] += parser.error_count
        stats["warnings"] = parser.warnings[:20]
        return stats

    def _commit(self, file_id, parser, batch, fields, decided, last_ord, stats) -> None:
        c = self.conn
        # lockfix-v3 (2026-10-30): التطبيع (CPU ثقيل) صار قبل BEGIN IMMEDIATE؛
        # القفل يُحجز فقط لحظة الإدخال الفعلي، فتقصر مدة احتجاز الكاتب لكل دفعة.
        prepped: list[tuple] = []  # (locator, vals, joined_bodies)
        vocab: Counter = Counter()
        for _, r in batch:
            vals = []
            bodies = []
            for col, v in r.values.items():
                if v is None or str(v).strip() == "":
                    continue
                fname = field_name(parser, r.dataset, col)
                if fname not in fields:
                    role = detect_role(col, [v])
                    self._ensure_field(file_id, fname, role, fields)
                fid, role = fields[fname]
                try:
                    norm = normalize_value(role, v)
                except Exception:  # قيمة شاذة لا توقف الفهرسة
                    norm = normalize_text(v)
                    stats["errors"] += 1
                vals.append((fid, str(v), norm))
                if norm:
                    bodies.append(norm)
                    if role in ("name", "text"):
                        for t in norm.split():
                            if _TOKEN_OK.match(t):
                                vocab[t] += 1
            if not vals:
                continue
            prepped.append((r.locator, vals, " ".join(bodies) if bodies else ""))
        own_tx = not c.in_transaction
        if own_tx:
            c.execute("BEGIN IMMEDIATE")
        try:
            # لا حساب يدوي لأي id أبدًا: records.id هو INTEGER PRIMARY KEY
            # فيولّده SQLite تلقائيًا، ونقرأه عبر cursor.lastrowid لكل صف.
            val_rows, fts_rows = [], []
            n_rec = 0
            for locator, vals, body in prepped:
                cur = c.execute(
                    "INSERT INTO records(file_id, locator) VALUES (?,?)",
                    (file_id, locator),
                )
                rid = cur.lastrowid  # الـid الحقيقي الذي ولّده SQLite — ليس محسوبًا
                if not rid:
                    raise RuntimeError("lastrowid was not set after INSERT into records")
                val_rows.extend((rid, fid, orig, norm) for fid, orig, norm in vals)
                if self.fts and body:
                    fts_rows.append((rid, body))
                n_rec += 1
            c.executemany(
                "INSERT INTO record_values(record_id, field_id, original_value, normalized_value) VALUES (?,?,?,?)",
                val_rows,
            )
            if fts_rows:
                # حماية: امسح أي بقايا يتيمة بنفس الـrowid قبل الإدخال (idempotent)
                c.executemany("DELETE FROM record_fts WHERE rowid=?", [(rid_,) for rid_, _ in fts_rows])
                c.executemany("INSERT INTO record_fts(rowid, body) VALUES (?,?)", fts_rows)
            if vocab:
                c.executemany(
                    "INSERT INTO vocab(token, n) VALUES (?,?) ON CONFLICT(token) DO UPDATE SET n = n + excluded.n",
                    vocab.items(),
                )
            stats["records"] += n_rec
            c.execute("UPDATE files SET record_count=? WHERE id=?", (stats["records"], file_id))
            c.execute(
                "UPDATE jobs SET checkpoint=? WHERE id=(SELECT id FROM jobs WHERE file_id=? AND status='running' ORDER BY id DESC LIMIT 1)",
                (json.dumps({"done": last_ord, "records": stats["records"]}), file_id),
            )
            if own_tx:
                c.execute("COMMIT")
        except BaseException:
            if own_tx and c.in_transaction:
                c.execute("ROLLBACK")
            raise
    # ---- maintenance
    def delete_file_records(self, file_id: int, chunk: int = DELETE_CHUNK) -> None:
        """Remove previously indexed records of a file (used by re-index).

        Serialized process-wide (lockfix-v4): one file's purge at a time, so
        concurrent workers never queue behind each other's long write
        transactions past busy_timeout. Waiting here holds NO database lock.
        """
        t_wait = time.monotonic()
        with _DELETE_LOCK:
            waited = time.monotonic() - t_wait
            if waited > _SLOW_SEC:
                log.warning("delete_file_records(file=%s) waited %.1fs for purge lock", file_id, waited)
            t_run = time.monotonic()
            self._delete_file_records_locked(file_id, chunk)
            log.info("delete_file_records(file=%s) done in %.1fs (chunk=%s)", file_id, time.monotonic() - t_run, chunk)

    def _delete_file_records_locked(self, file_id: int, chunk: int) -> None:
        while True:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM records WHERE file_id=? LIMIT ?", (file_id, chunk))]
            if not ids:
                break
            q = ",".join("?" * len(ids))
            t_tx = time.monotonic()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if self.fts:
                    self.conn.execute(f"DELETE FROM record_fts WHERE rowid IN ({q})", ids)
                # Audit fix 2026-10-27 (F1): purge ER data referencing the deleted
                # ids in the SAME transaction. entity_matches / match_decisions
                # carry no FK to records, so without this purge stale edges
                # survive reindex and wedge every later recluster (FK failure
                # on INSERT INTO entity_records) — including rebuilds, once a
                # stale edge has been decided. er_blocks has no FK either, so
                # its rows for dead ids are purged too (er_records and
                # entity_records cascade via records -> er_records).
                self.conn.execute(f"DELETE FROM entity_matches WHERE record_a IN ({q}) OR record_b IN ({q})", ids + ids)
                self.conn.execute(f"DELETE FROM match_decisions WHERE record_a IN ({q}) OR record_b IN ({q})", ids + ids)
                self.conn.execute(f"DELETE FROM er_blocks WHERE record_id IN ({q})", ids)
                # FTS-purge fix 2026-10-29: stored search results reference
                # record ids too. Without this, a search run BEFORE the purge
                # keeps its hits/groups pointing at the purged ids, and
                # "عرض النتائج" afterwards shows missing records (bare "-"
                # entries, or a stolen next record via keyset pagination).
                # New searches were already clean (FTS delete + EXISTS guard);
                # this closes the old-search path. Groups are rebuilt from
                # the surviving hits (same SQL as SearchEngine._groups) and
                # result_count recomputed, so no purged id can be returned
                # by any search, old or new. Scoped to this chunk's ids only
                # — no whole-index rebuild, no touch to other files' hits.
                sids = [r[0] for r in self.conn.execute(f"SELECT DISTINCT search_id FROM search_hits WHERE record_id IN ({q})", ids)]
                self.conn.execute(f"DELETE FROM search_hits WHERE record_id IN ({q})", ids)
                self.conn.execute(f"DELETE FROM records WHERE id IN ({q})", ids)
                for sid in sids:
                    self.conn.execute("DELETE FROM search_groups WHERE search_id=?", (sid,))
                    self.conn.execute(
                        "INSERT INTO search_groups(search_id, rank, entity_id, record_id, match_type, score, hit_count)"
                        " SELECT ?, ROW_NUMBER() OVER (ORDER BY MIN(h.rank)), COALESCE(en.entity_id, -h.record_id), MIN(h.record_id),"
                        " MIN(h.match_type), MIN(h.score), COUNT(*) FROM search_hits h"
                        " LEFT JOIN entity_records en ON en.record_id=h.record_id AND en.active=1"
                        " WHERE h.search_id=? GROUP BY COALESCE(en.entity_id, -h.record_id)", (sid, sid))
                    self.conn.execute(
                        "UPDATE search_history SET result_count=(SELECT COUNT(*) FROM search_hits WHERE search_id=?) WHERE id=?", (sid, sid))
                self.conn.execute("COMMIT")
                held = time.monotonic() - t_tx
                if held > _SLOW_SEC:
                    log.warning("delete_file_records(file=%s): write tx held %.1fs for %s ids", file_id, held, len(ids))
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
        # كانا autocommit منفصلين؛ الآن معاملة قصيرة واحدة (ذرّية) تحت نفس busy_timeout.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM fields WHERE file_id=?", (file_id,))
            self.conn.execute("UPDATE files SET record_count=0, indexed_at=NULL WHERE id=?", (file_id,))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def verify(self, file_id: int, expected: int) -> tuple[bool, str]:
        """Post-index verification: record count, a sample lookup, and consistency."""
        got = self.conn.execute("SELECT COUNT(*) FROM records WHERE file_id=?", (file_id,)).fetchone()[0]
        if got != expected:
            return False, f"عدد السجلات {got} لا يطابق المتوقع {expected}"
        if got:
            off = random.randrange(got)
            row = self.conn.execute(
                "SELECT rv.normalized_value AS nv FROM record_values rv JOIN records r ON r.id=rv.record_id "
                "WHERE r.file_id=? AND rv.normalized_value IS NOT NULL LIMIT 1 OFFSET ?", (file_id, min(off, got - 1))).fetchone()
            if row is not None:
                hit = self.conn.execute("SELECT 1 FROM record_values WHERE normalized_value=? LIMIT 1", (row["nv"],)).fetchone()
                if hit is None:
                    return False, "فحص العينة فشل: القيمة غير قابلة للبحث"
        return True, "ok"
