"""Cumulative Master Excel export, v3 (streaming, resumable, split delivery).

v2 loaded ALL records + ALL record_values into one in-RAM dict, then wrote
row-by-row with no progress calls: on the Nokia (3.8GB RAM, ~1GB free) with
a 1.05GB database, job 513 ground ~28h (temp XML 88MB and growing) while
reading as "stuck at 0%", and the single worker blocked 151 queued jobs.
v3 keeps v2's sheets/columns/values semantics exactly, and changes the
mechanics:
- STREAM: records via keyset pagination (id > last_id, LIMIT batch);
  values per batch via the indexed record_values(record_id) range.
  Only one batch of values is ever in RAM.
- PROGRESS + CHECKPOINT per batch (the same jobs.checkpoint pattern the
  ER job uses): an interrupted part resumes from its last completed batch
  by reopening its temp sheet XML, truncated to the last checkpointed row.
  Completed parts are never rebuilt (the manifest lives in the job payload).
- SPLIT DELIVERY: every part file is size-verified after build; a part
  over PART_MAX_BYTES is split by record range and rebuilt in halves
  (recursion, exact). Delivery sends each part as its own document via
  the existing send path; a failed send fails the job with the exact
  reason and never claims an unsent part as sent.
- INTERLEAVE: one job builds one part-range, then submits a continuation
  job carrying the manifest. The worker claims FIFO by id, so the
  continuation runs AFTER jobs already queued: the merge advances one
  part per queue round and never monopolizes the worker again.
  A run covers the record snapshot at its start (ids <= max_id); files
  ingested mid-run belong to a later run.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import audit
from ..er.xlsx_writer import Sheet, XlsxBook
from ..errors import JobInterrupted
from ..jobs import ProgressETA

log = logging.getLogger("pdb.master")

DOMAIN = ["الطبيب", "الاختصاص", "المنطقة", "المنطقة الفرعية", "الكلاس",
          "الصيدلية المرتبطة", "رابط موقع الصيدلية", "أوردراته",
          "ديلر او ساينتفك", "ملاحظات", "موعد و يوم الاستقبال"]
GENERIC = ["الاسم", "الهاتف", "السنة", "الكود", "المعرف",
           "ملاحظة", "النص", "الصفحة", "السطر"]

SHEET = "taha survey all."
MAP = {
    SHEET + "الطبيب": "الطبيب",
    SHEET + "الاختصاص": "الاختصاص",
    SHEET + "المنطقة": "المنطقة",
    SHEET + "المنطقة الفرعية": "المنطقة الفرعية",
    SHEET + "الكلاس": "الكلاس",
    SHEET + "الصيدلية المرتبطة": "الصيدلية المرتبطة",
    SHEET + "رابط موقع الصيدلية": "رابط موقع الصيدلية",
    SHEET + "أوردراته": "أوردراته",
    SHEET + "ديلر او ساينتفك": "ديلر او ساينتفك",
    SHEET + "ملاحظات": "ملاحظات",
    SHEET + "موعد و يوم الاستقبال": "موعد و يوم الاستقبال",
    "doctors.name": "الطبيب",
    "doctors.specialty": "الاختصاص",
    "pharmacies.name": "الصيدلية المرتبطة",
    "pharmacies.area": "المنطقة",
    "persons.name": "الاسم",
    "Sheet1.name": "الاسم",
    "s.name": "الاسم",
    "t.name": "الاسم",
    "name": "الاسم",
    "persons.phone": "الهاتف",
    "Sheet1.phone": "الهاتف",
    "phone": "الهاتف",
    "persons.year": "السنة",
    "Sheet1.year": "السنة",
    "year": "السنة",
    "notes.note": "ملاحظة",
    "text": "النص",
    "page": "الصفحة",
    "line": "السطر",
}


def canonical(field: str) -> str:
    if field in MAP:
        return MAP[field]
    base = field.rsplit(".", 1)[-1]
    if base == "code":
        return "الكود"
    if base == "id":
        return "المعرف"
    return base


# ---- tuning (module constants so tests can monkeypatch them) ----
BATCH_RECORDS = 2000
PART_TARGET_BYTES = 40_000_000  # aim each part at ~40MB of xlsx
PART_MAX_BYTES = 49_000_000  # hard cap: Telegram Bot API rejects >=50MB
PART_MIN_RECORDS = 10_000
PART_MAX_RECORDS = 150_000
INITIAL_PART_RECORDS = 50_000


def columns_for(conn: sqlite3.Connection) -> list[str]:
    """Header content columns, exactly as v2 computed them (global)."""
    fields = [r[0] for r in conn.execute("SELECT DISTINCT name FROM fields")]
    columns = list(DOMAIN) + list(GENERIC)
    for f in fields:
        c = canonical(f)
        if c not in columns:
            columns.append(c)
    return columns


def snapshot(conn: sqlite3.Connection) -> tuple[int, int]:
    """(total records, max record id) at run start; 0,0 on an empty DB."""
    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    mx = conn.execute("SELECT MAX(id) FROM records").fetchone()[0]
    return int(total), int(mx or 0)


def _batch_records(conn, last_id: int, end_id: int, limit: int):
    return conn.execute(
        "SELECT r.id, f.original_name, r.locator FROM records r "
        "JOIN files f ON f.id = r.file_id "
        "WHERE r.id > ? AND r.id <= ? ORDER BY r.id LIMIT ?",
        (last_id, end_id, limit)).fetchall()


def _batch_values(conn, lo_id: int, hi_id: int) -> dict[int, dict[str, str]]:
    """Values for one batch only, grouped per record, duplicate fields
    joined with '؛ ' (v2 semantics). Ordered by record_id, rowid so the
    join order matches v2's full-scan order."""
    values: dict[int, dict[str, str]] = {}
    for rid, fname, val in conn.execute(
            "SELECT rv.record_id, fl.name, rv.original_value "
            "FROM record_values rv JOIN fields fl ON fl.id = rv.field_id "
            "WHERE rv.record_id > ? AND rv.record_id <= ? "
            "AND rv.original_value IS NOT NULL AND rv.original_value != '' "
            "ORDER BY rv.record_id, rv.rowid", (lo_id, hi_id)):
        cell = values.setdefault(rid, {})
        c = canonical(fname)
        if c in cell:
            cell[c] += "؛ " + (val or "")
        else:
            cell[c] = val or ""
    return values


def _sources_rows(conn):
    return conn.execute(
        "SELECT id, original_name, status, record_count FROM files "
        "ORDER BY id").fetchall()


def _truncate_tmp_to_rows(tmp_path: str, rows: int) -> None:
    """Truncate a sheet temp XML to just after its rows-th </row>.
    rows counts the header row. Anything beyond it (un-checkpointed
    complete rows, or a torn tail from a kill mid-write) is discarded,
    so a resume never duplicates rows and never keeps a broken row.
    The scan carries the last bytes of each chunk into the next one,
    so a marker straddling a chunk boundary is never missed or
    double-counted (a marker fully inside the carry is impossible:
    the carry is shorter than the marker)."""
    if rows < 1:
        raise ValueError(f"bad checkpoint rows={rows}")
    marker = b"</row>"
    mlen = len(marker)
    found = 0
    offset = 0  # absolute offset of the current chunk's first byte
    carry = b""
    with open(tmp_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf = carry + chunk
            base = offset - len(carry)  # absolute offset of buf[0]
            start = 0
            while True:
                i = buf.find(marker, start)
                if i < 0:
                    break
                found += 1
                if found == rows:
                    with open(tmp_path, "r+b") as out:
                        out.truncate(base + i + mlen)
                    return
                start = i + 1
            carry = buf[-(mlen - 1):] if len(buf) >= mlen - 1 else buf
            offset += len(chunk)
    raise ValueError(
        f"checkpoint rows={rows} exceeds complete rows={found} in {tmp_path}")


def _resume_sheet(book: XlsxBook, header, widths, tmp_path: str, rows: int):
    """Rebuild a Sheet object around an existing temp XML (no __init__:
    __init__ would create a fresh temp file and rewrite the header)."""
    sh = Sheet.__new__(Sheet)
    sh.book, sh.base, sh.header, sh.widths = book, "Master", header, widths
    sh.part = 1
    sh.rows = rows
    sh.tmp = open(tmp_path, "a", encoding="utf-8")
    book.sheets.append(("Master", tmp_path, sh))
    book._sheet_objs.append(sh)
    return sh


def build_part_file(conn, staging_path, start_after: int, end_id: int,
                    max_records: int, *, resume=None, on_batch=None,
                    stopping=None) -> dict:
    """Stream one part-range into staging_path (an .xlsx).

    start_after is exclusive, end_id inclusive (snapshot max). At most
    max_records data rows are written. on_batch(info) fires per batch
    with {last_id, rows, records_done, filled, tmpdir, master_tmp};
    stopping() checked after each batch raises JobInterrupted.
    resume (a checkpoint dict from on_batch) continues the same range
    from its last completed batch instead of rebuilding it; an unusable
    checkpoint falls back to a fresh build (logged, never fatal).
    Returns {staging, from_id, to_id, records, filled}. When the range
    holds no records, no file is created and records == 0.
    """
    columns = columns_for(conn)
    header = columns + ["record_id", "source_file", "locator"]
    cidx = {c: i for i, c in enumerate(columns)}
    widths = [26] * len(columns) + [10, 30, 24]

    # An empty range creates no file and no temp dir: peek first.
    if not _batch_records(conn, start_after, end_id, 1):
        return {"staging": str(staging_path), "from_id": start_after,
                "to_id": start_after, "records": 0, "filled": 0}
    book = XlsxBook(Path(staging_path))
    last_id, records_done, filled = start_after, 0, 0
    sheet = None
    if resume:
        try:
            tmp_path = resume["master_tmp"]
            rows = int(resume["rows"])
            rlast = int(resume["last_id"])
            if not (rows >= 1 and rlast > start_after
                    and Path(tmp_path).is_file()):
                raise ValueError("checkpoint out of range")
            _truncate_tmp_to_rows(tmp_path, rows)
            sheet = _resume_sheet(book, header, widths, tmp_path, rows)
            last_id = rlast
            records_done = int(resume["records_done"])
            filled = int(resume.get("filled", 0))
            log.info("master part resumed: range %s..%s from id %s "
                     "(%s rows already written)",
                     start_after, end_id, last_id, records_done)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            log.warning("master part checkpoint unusable (%s); "
                        "rebuilding range %s..%s fresh", exc,
                        start_after, end_id)
            sheet = None
    if sheet is None:
        sheet = book.sheet("Master", header, widths)
    while records_done < max_records and last_id < end_id:
        limit = min(BATCH_RECORDS, max_records - records_done)
        batch = _batch_records(conn, last_id, end_id, limit)
        if not batch:
            break
        vals = _batch_values(conn, last_id, batch[-1][0])
        for rid, fname, loc in batch:
            row = [""] * len(columns) + [rid, fname, loc or ""]
            for name, val in vals.get(rid, {}).items():
                row[cidx[name]] = val
                filled += 1
            sheet.write(row)
            records_done += 1
            last_id = rid
        if on_batch is not None:
            on_batch({"last_id": last_id, "rows": sheet.rows,
                      "records_done": records_done, "filled": filled,
                      "tmpdir": book.tmpdir, "master_tmp": sheet.tmp.name})
        if stopping is not None and stopping():
            raise JobInterrupted("stopped")
    src = book.sheet("Sources",
                     ["file_id", "file_name", "status", "records"],
                     [10, 40, 14, 10])
    for fid, name, status, cnt in _sources_rows(conn):
        src.write([fid, name, status, cnt])
    book.close()
    return {"staging": str(staging_path), "from_id": start_after,
            "to_id": last_id, "records": records_done, "filled": filled}


def _median_split_id(conn, start_after: int, end_id: int):
    """Id of the k-th record of a range, where k = n // 2 (so the left
    half holds exactly k records and the right half n - k). Returns None
    when the range holds <= 1 record (cannot split further).
    NOTE: the split point must be the k-th record, NOT the (k+1)-th:
    splitting at the (k+1)-th record leaves the FULL range on the left
    when n == 2 (left = both records), which recursed forever (job 513
    rebuild, first test run: 970+ nested splits before RecursionError).
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM records WHERE id > ? AND id <= ?",
        (start_after, end_id)).fetchone()[0]
    if n <= 1:
        return None
    row = conn.execute(
        "SELECT id FROM records WHERE id > ? AND id <= ? "
        "ORDER BY id LIMIT 1 OFFSET ?", (start_after, end_id, n // 2 - 1)
        ).fetchone()
    return row[0] if row else None


def build_verified_parts(conn, reports_dir, run: str, start_after: int,
                         end_id: int, max_records: int, *, resume=None,
                         on_batch=None, stopping=None, _depth=0) -> list:
    """Build one part-range into size-verified staging files.

    Returns a list of (staging_path, info) in range order. A part over
    PART_MAX_BYTES is split by record range and rebuilt in halves
    (recursion, exact size check). The oversized staging file is
    unlinked (it is a build artifact, never a delivered file). A range
    whose single record alone exceeds the cap raises RuntimeError with
    the exact reason instead of hanging or delivering a broken part.
    A depth guard converts any unforeseen split pathology into a clean
    FAILED job instead of unbounded recursion (never hang).
    """
    if _depth > 64:
        raise RuntimeError(
            f"master split depth exceeded for range {start_after}..{end_id}; "
            f"aborting instead of recursing")
    staging = Path(reports_dir) / (
        f"Master_{run}_staging_{start_after}_{end_id}.xlsx")
    info = build_part_file(conn, staging, start_after, end_id, max_records,
                           resume=resume, on_batch=on_batch,
                           stopping=stopping)
    if info["records"] == 0:
        return []
    size = staging.stat().st_size
    if size <= PART_MAX_BYTES:
        return [(staging, info)]
    mid = _median_split_id(conn, start_after, info["to_id"])
    if mid is None:
        raise RuntimeError(
            f"master part over size cap: single record range "
            f"{start_after}..{info['to_id']} produced {size} bytes, "
            f"over the {PART_MAX_BYTES} cap; cannot split further")
    log.warning("master part %s..%s oversized (%s bytes); splitting at %s",
                start_after, info["to_id"], size, mid)
    staging.unlink(missing_ok=True)
    left = build_verified_parts(conn, reports_dir, run, start_after, mid,
                                max_records, resume=None, on_batch=on_batch,
                                stopping=stopping, _depth=_depth + 1)
    # Split only the range actually written (start_after..to_id]; the
    # remainder beyond to_id belongs to the next part-job, not this split.
    right = build_verified_parts(conn, reports_dir, run, mid, info["to_id"],
                                 max_records, resume=None, on_batch=on_batch,
                                 stopping=stopping, _depth=_depth + 1)
    return left + right


def _part_records_for(bpr) -> int:
    """Adaptive part size: aim at PART_TARGET_BYTES using the observed
    bytes/record, clamped to sane bounds; 50k records before any data."""
    if bpr and bpr > 0:
        return max(PART_MIN_RECORDS,
                   min(PART_MAX_RECORDS, int(PART_TARGET_BYTES / bpr)))
    return INITIAL_PART_RECORDS


def run_master_job(ctx, api, reports_dir) -> dict:
    """One master_excel job: build one part-range, or deliver the run.

    ctx is duck-typed (real JobContext in production): .conn, .id,
    .payload (dict), .checkpoint (dict), .created_by, .queue.submit,
    .progress(pct), .log(msg), .stopping(). api needs send_document
    and (best-effort) send_message. reports_dir is the reports folder.

    State machine, all state in the job payload (survives restarts):
    - fresh submit -> snapshot (total, max_id), build part 1, submit a
      continuation carrying the manifest; each continuation builds the
      next range. The worker claims FIFO by id, so continuations run
      after jobs already queued: the merge interleaves with the queue.
    - a job whose range already reached max_id delivers every manifest
      part in order (skipping parts already sent, recorded in the
      payload, so a retried delivery never resends a part).
    A failed send raises with the exact part/reason; sent parts are
    logged and never re-claimed, unsent parts are never claimed sent.
    """
    conn = ctx.conn
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(ctx.payload or {})
    if payload.get("run"):
        run = payload["run"]
        total = int(payload["total"])
        max_id = int(payload["max_id"])
        manifest = list(payload.get("parts") or [])
        sent = list(payload.get("sent") or [])
        bpr = payload.get("bytes_per_record")
        start_after = int(payload["next_after"])
    else:
        run = f"{datetime.now():%Y-%m-%d_%H%M%S}_JOB-{ctx.id:06d}"
        total, max_id = snapshot(conn)
        manifest, sent, bpr, start_after = [], [], None, 0
    if total == 0 or max_id == 0:
        ctx.log("Master: قاعدة فارغة — لا شيء للدمج")
        return {"run": run, "parts": 0, "records": 0, "files": 0, "sent": 0}

    manifest_records = sum(p["records"] for p in manifest)

    if start_after < max_id:
        part_records = _part_records_for(bpr)
        cp = ctx.checkpoint or {}
        resume = cp if (cp.get("run") == run
                        and cp.get("part_start") == start_after) else None
        if cp and resume is None:
            log.info("master job %s: ignoring stale checkpoint for "
                     "another run/part", ctx.id)
        last_reported = [0.0]
        meta = ProgressETA(total)

        def on_batch(info):
            ctx.conn.execute(
                "UPDATE jobs SET checkpoint=? WHERE id=?",
                (json.dumps({"run": run, "part_start": start_after,
                             "last_id": info["last_id"],
                             "rows": info["rows"],
                             "records_done": info["records_done"],
                             "filled": info["filled"],
                             "tmpdir": info["tmpdir"],
                             "master_tmp": info["master_tmp"]}), ctx.id))
            overall = ((manifest_records + info["records_done"]) / total
                       * 100.0) if total else 100.0
            pct = min(99.0, max(last_reported[0], overall))
            last_reported[0] = pct
            ctx.progress(pct)
            _, eta_dt = meta.update(manifest_records + info["records_done"])
            if eta_dt is not None and meta.should_log(pct):
                ctx.log(f"تقدم {pct:.0f}% — الانتهاء المتوقع {eta_dt:%H:%M}")

        built = build_verified_parts(conn, reports_dir, run, start_after,
                                     max_id, part_records, resume=resume,
                                     on_batch=on_batch,
                                     stopping=ctx.stopping)
        if not built:
            # Range exhausted (records deleted mid-run): nothing more
            # to build; fall through to delivery of what exists.
            start_after = max_id
        else:
            for staging, info in built:
                seq = len(manifest) + 1
                final = reports_dir / f"Master_{run}_part{seq:03d}.xlsx"
                os.replace(staging, final)
                os.chmod(final, 0o600)
                manifest.append({"seq": seq, "path": str(final),
                                 "from_id": info["from_id"],
                                 "to_id": info["to_id"],
                                 "records": info["records"],
                                 "size": final.stat().st_size})
            tot_b = sum(p["size"] for p in manifest)
            tot_r = sum(p["records"] for p in manifest)
            bpr = (tot_b / tot_r) if tot_r else None
            start_after = built[-1][1]["to_id"]
            ctx.log(f"Master: أُنجزت أجزاء {len(built)} "
                    f"(سجلات {manifest_records}.."
                    f"{manifest_records + sum(i['records'] for _, i in built)}) "
                    f"— تقدم الدمج الكلي "
                    f"{(sum(p['records'] for p in manifest) / total * 100.0):.1f}%")
            if start_after < max_id:
                cont = {"run": run, "total": total, "max_id": max_id,
                        "parts": manifest, "sent": [],
                        "bytes_per_record": bpr, "next_after": start_after}
                cont_id = ctx.queue.submit("master_excel", None, cont,
                                           ctx.created_by)
                ctx.log(f"Master: متابعة الدمج JOB-{cont_id:06d} "
                        f"(تعمل بعد المهام المنتظرة — لا تحتكر العامل)")
                return {"run": run, "parts": len(manifest),
                        "records": sum(p["records"] for p in manifest),
                        "continued": cont_id}

    # ---- delivery phase (final part built, or a retried delivery) ----
    if not manifest:
        ctx.log("Master: لا أجزاء للتسليم")
        return {"run": run, "parts": 0, "records": 0, "files": 0, "sent": 0}
    if ctx.created_by is None:
        raise RuntimeError(
            "Master: لا يوجد طالب للتسليم (created_by فارغ) — "
            "لن يُرسل أي جزء")
    ctx.conn.execute(
        "UPDATE jobs SET payload=? WHERE id=?",
        (json.dumps({"run": run, "total": total, "max_id": max_id,
                     "parts": manifest, "sent": sent,
                     "bytes_per_record": bpr, "next_after": max_id}),
         ctx.id))
    n = len(manifest)
    for part in manifest:
        seq = part["seq"]
        if seq in sent:
            continue
        path = Path(part["path"])
        if not path.is_file():
            raise RuntimeError(
                f"Master: ملف الجزء {seq}/{n} مفقود ({path}) — "
                f"أُرسل منها {sent}؛ أعد بناء الدمج أو استعد الملف ثم "
                f"أعد محاولة المهمة")
        try:
            api.send_document(
                ctx.created_by, path,
                f"🗂 Master Excel • جزء {seq}/{n} • "
                f"{part['records']} سجل")
        except Exception as exc:
            ctx.log(f"Master: توقف التسليم عند الجزء {seq}/{n}: {exc}؛ "
                    f"المُرسلة: {sent}، غير المُرسلة لم تُدعَ مُرسلة")
            raise RuntimeError(
                f"master delivery stopped at part {seq}/{n}: {exc}; "
                f"delivered parts: {sent}") from exc
        sent.append(seq)
        ctx.conn.execute(
            "UPDATE jobs SET payload=? WHERE id=?",
            (json.dumps({"run": run, "total": total, "max_id": max_id,
                         "parts": manifest, "sent": sent,
                         "bytes_per_record": bpr, "next_after": max_id}),
             ctx.id))
        audit.log(conn, ctx.created_by, audit.EXPORT, "master_excel",
                  ctx.id, {"part": seq, "parts": n,
                           "records": part["records"],
                           "size_bytes": part["size"]})
        ctx.log(f"Master: أُرسل الجزء {seq}/{n}")
    files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_records = sum(p["records"] for p in manifest)
    try:
        api.send_message(
            ctx.created_by,
            f"🗂 Master Excel كامل: {n} أجزاء • {total_records} سجل • "
            f"{files_count} ملف")
    except Exception as exc:  # summary text only; parts already delivered
        log.warning("master summary text failed (parts delivered): %s", exc)
    audit.log(conn, ctx.created_by, audit.EXPORT, "master_excel", ctx.id,
              {"parts": n, "records": total_records, "files": files_count})
    ctx.log(f"Master: اكتمل التسليم — {n} أجزاء، {total_records} سجل")
    ctx.conn.execute("UPDATE jobs SET checkpoint=NULL WHERE id=?", (ctx.id,))
    ctx.progress(100)
    return {"run": run, "parts": n, "records": total_records,
            "files": files_count, "sent": len(sent)}
