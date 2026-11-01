"""Phase 2 — file processing (nb:p2:*).

Admin-only (perm "manage") screens. Privacy: screens show counts, ids,
statuses, file names, column names and job numbers ONLY. Record values are
never selected into output: quality checks use COUNT aggregates over
record_values (never SELECT original/normalized value text), and no screen
reads record_values value columns at all.

Honest limits (stated on the screens themselves, not hidden):
- Queue control is a direct jobs-table status transition mirroring
  JobQueue.control semantics. JobQueue itself cannot be driven from this
  module: it opens its own connection from cfg.db_path, while the router
  hands us ctx.db; a live worker therefore will not notice a pause/cancel
  request until the parent wires live JobQueue control. Queued/paused jobs
  are fully controlled by the table transition; running-job pause/cancel
  is recorded as a status request.
- Isolation / suspension records (nb_isolated_files) are real and
  queryable, but blocking an isolated file from search at intake is
  parent wiring, not done here.
- Files table has no uploader column: the auth screen shows the latest
  job created_by for the file when one exists, else "غير مسجّل".
- Jobs table has no priority column: queue order is FIFO by id.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3

from ..errors import AppError, PermissionDenied
from .nb_store import (
    audit_nb,
    confirm_markup,
    ensure_nb_tables,
    page_slice,
    set_pending,
)
from .ui import LINE, ago, fmt_int, fmt_size, kb, nav, pager

P = "nb:p2:"
PAGE_SIZE = 10
TERMINAL_JOBS = ("done", "failed", "cancelled")
NONTERMINAL_JOBS = ("queued", "running", "paused", "interrupted")

BUTTONS = [
    {"key": "imp", "label": "📥 مركز استيراد الملفات", "perm": "manage", "confirm": False, "audit": "nb_p2_imp_approve"},
    {"key": "bat", "label": "🗃 إدارة الدفعات", "perm": "manage", "confirm": True, "audit": "nb_p2_bat_stop"},
    {"key": "chk", "label": "🧪 فحص الملفات قبل المعالجة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "cmp", "label": "🔎 مقارنة نسخ الملفات", "perm": "manage", "confirm": False, "audit": "nb_p2_cmp_decision"},
    {"key": "que", "label": "⚙️ طابور المعالجة", "perm": "manage", "confirm": True, "audit": "nb_p2_que_cancel"},
    {"key": "idx", "label": "🗂 فهرس الملفات والحقول", "perm": "manage", "confirm": True, "audit": "nb_p2_idx_approve"},
    {"key": "auth", "label": "🛡 مراجعة الاستخدام المصرح", "perm": "manage", "confirm": True, "audit": "nb_p2_auth_isolate"},
]
_KEYS = {b["key"] for b in BUTTONS}
_HOME = "nb:p2:imp"


# ---------------------------------------------------------------- helpers
def _commit(db):
    try:
        db.commit()
    except sqlite3.Error:
        pass


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _row(db, sql, params=()):
    return db.execute(sql, params).fetchone()


def _file(db, fid):
    return _row(db, "SELECT * FROM files WHERE id=?", (fid,))


def _fline(f):
    return (f"#{f['id']} {f['original_name']} — {fmt_size(f['size_bytes'] or 0)} — "
            f"نوع: {f['detected_type'] or '-'} — حالة: {f['status']} — "
            f"سجلات: {fmt_int(f['record_count'] or 0)} — استلام: {ago(f['received_at'])}")


def _nonterminal_job(db, file_id, kind=None):
    sql = ("SELECT * FROM jobs WHERE file_id=? AND status IN "
           "('queued','running','paused','interrupted') ORDER BY id DESC LIMIT 1")
    params = [file_id]
    if kind:
        sql = sql.replace("WHERE file_id=?", "WHERE file_id=? AND kind=?")
        params = [file_id, kind]
    return _row(db, sql, params)


def _job_cols(db):
    try:
        return {r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()}
    except sqlite3.Error:
        return set()


def _submit_ingest(db, user, file_id):
    """Insert a queued ingest job, or return the existing non-terminal one."""
    existing = _nonterminal_job(db, file_id, kind="ingest")
    if existing:
        return existing, False
    cols = _job_cols(db)
    payload = json.dumps({"source": "nb_p2", "file_id": file_id}, ensure_ascii=False)
    if "payload" in cols:
        cur = db.execute(
            "INSERT INTO jobs(kind, file_id, status, progress, created_by, payload) "
            "VALUES ('ingest', ?, 'queued', 0, ?, ?)", (file_id, user.telegram_id, payload))
    else:
        cur = db.execute(
            "INSERT INTO jobs(kind, file_id, status, progress, created_by) "
            "VALUES ('ingest', ?, 'queued', 0, ?)", (file_id, user.telegram_id))
    _commit(db)
    return _row(db, "SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)), True


def _nav_markup(back=None):
    return kb(nav(back))


def _screen(db, key, text, rows, back=None):
    """rows: list of button rows (each a list of (label, cb) tuples)."""
    all_rows = list(rows)
    if back:
        all_rows.append([("◀️ رجوع", back)])
    all_rows.append(nav())
    return text, kb(*all_rows)


# ---------------------------------------------------------------- quality check
def _norm_name(s):
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def _check_file(db, fid):
    """Return (issues:list[str], notes:list[str]). Counts only — no values."""
    f = _file(db, fid)
    if f is None:
        return ["الملف غير موجود"], []
    issues, notes = [], []
    if f["status"] in ("failed", "quarantined"):
        issues.append(f"تلف/حجر: حالة {f['status']}" + (f" — خطأ: {(f['error'] or '')[:200]}" if f["error"] else ""))
    if f["status"] == "unsupported":
        issues.append(f"غير مدعوم: نوع مكتشف {f['detected_type'] or '-'}")
    fields = db.execute(
        "SELECT * FROM fields WHERE file_id=? ORDER BY position, id", (fid,)).fetchall()
    nrec = db.execute("SELECT COUNT(*) FROM records WHERE file_id=?", (fid,)).fetchone()[0]
    if nrec == 0:
        notes.append("لا توجد سجلات بعد لهذا الملف — فراغ الأعمدة غير قابل للقياس")
    else:
        empty = []
        for fld in fields:
            try:
                cnt = db.execute(
                    "SELECT COUNT(*) FROM record_values WHERE field_id=? "
                    "AND normalized_value IS NOT NULL AND TRIM(normalized_value) <> ''",
                    (fld["id"],)).fetchone()[0]
            except sqlite3.Error:
                cnt = None
            if cnt == 0:
                empty.append(fld["name"])
        if empty:
            issues.append("أعمدة فارغة (صفر قيم): " + "، ".join(empty))
    # schema change vs previous version of the same name
    prev = _row(db, "SELECT * FROM files WHERE original_name=? AND id<? ORDER BY id DESC LIMIT 1",
                (f["original_name"], fid))
    if prev is not None:
        if (prev["detected_type"] or "") != (f["detected_type"] or ""):
            issues.append(f"تغيّر النوع المكتشف: {prev['detected_type'] or '-'} ← {f['detected_type'] or '-'}")
        old_cols = [r[0] for r in db.execute(
            "SELECT name FROM fields WHERE file_id=? ORDER BY position, id", (prev["id"],)).fetchall()]
        new_cols = [fld["name"] for fld in fields]
        added = [c for c in new_cols if c not in old_cols]
        removed = [c for c in old_cols if c not in new_cols]
        if added:
            issues.append("أعمدة مضافة: " + "، ".join(added))
        if removed:
            issues.append("أعمدة محذوفة: " + "، ".join(removed))
    else:
        notes.append("لا توجد نسخة سابقة بنفس الاسم للمقارنة")
    # invalid dates — only if a date column exists; count only
    date_cols = [fld for fld in fields
                 if "date" in (fld["detected_type"] or "").lower()
                 or "date" in fld["name"].lower() or "dob" in fld["name"].lower()
                 or "تاريخ" in fld["name"] or "ميلاد" in fld["name"]]
    if not date_cols:
        notes.append("تواريخ غير صالحة: غير قابلة للقياس — لا يوجد عمود تاريخ بالمخطط")
    else:
        for fld in date_cols:
            try:
                bad = db.execute(
                    "SELECT COUNT(*) FROM record_values WHERE field_id=? "
                    "AND normalized_value IS NOT NULL AND TRIM(normalized_value) <> '' "
                    "AND normalized_value NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
                    "AND normalized_value NOT GLOB '[0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]*' "
                    "AND normalized_value NOT GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]*'",
                    (fld["id"],)).fetchone()[0]
            except sqlite3.Error:
                bad = None
            if bad:
                issues.append(f"تواريخ غير صالحة: {fmt_int(bad)} قيمة بالعمود {fld['name']} (عدد فقط)")
    return issues, notes


def _check_text(db, fid):
    f = _file(db, fid)
    if f is None:
        return "🧪 فحص الملف\nالملف غير موجود.", [], 0
    issues, notes = _check_file(db, fid)
    lines = ["🧪 تقرير فحص الملف", _fline(f), LINE]
    if issues:
        lines.append(f"المشاكل: {len(issues)}")
        lines += [f"• {i}" for i in issues]
    else:
        lines.append("لا مشاكل مكتشفة.")
    if notes:
        lines.append(LINE)
        lines += [f"• {n}" for n in notes]
    return "\n".join(lines), issues, len(issues)


# ---------------------------------------------------------------- imp
def _imp_list(db, page):
    rows = db.execute(
        "SELECT * FROM files WHERE status IN ('queued','received','processing') "
        "ORDER BY received_at DESC, id DESC").fetchall()
    page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
    lines = [f"📥 مركز استيراد الملفات — صفحة {page + 1} من {pages} — {fmt_int(len(rows))} ملفًا بانتظار المعالجة", LINE]
    btns = []
    for f in page_rows:
        lines.append(_fline(f))
        btns.append([(f"📄 #{f['id']} {f['original_name']}", f"{P}imp:f:{f['id']}")])
    if not page_rows:
        lines.append("لا ملفات بانتظار المعالجة.")
    pg = pager(page, pages, f"{P}imp")
    if pg:
        btns.append(pg)
    return "\n".join(lines), btns


def _imp_detail(db, fid):
    f = _file(db, fid)
    if f is None:
        return "📥 مركز استيراد الملفات\nالملف غير موجود.", []
    lines = ["📥 تفاصيل الملف", _fline(f), LINE]
    dups = db.execute(
        "SELECT id, original_name FROM files WHERE sha256=? AND id<>? ORDER BY id",
        (f["sha256"], fid)).fetchall()
    if dups:
        lines.append("⚠️ تحذير تكرار: نفس sha256 موجود بملفات أخرى (لم يُحذف شيء تلقائيًا):")
        lines += [f"• #{d['id']} {d['original_name']}" for d in dups]
    else:
        lines.append("لا تكرارات sha256 لهذا الملف.")
    job = _nonterminal_job(db, fid, kind="ingest")
    if job:
        lines.append(f"مهمة معالجة غير منتهية موجودة: JOB-{job['id']:06d} ({job['status']})")
    btns = [[("🧪 فحص الملف", f"{P}imp:chk:{fid}"), ("✅ اعتماد المعالجة", f"{P}imp:ap:{fid}")]]
    return "\n".join(lines), btns


# ---------------------------------------------------------------- bat
def _bat_list(db, page):
    rows = db.execute("SELECT * FROM nb_batches ORDER BY id DESC").fetchall()
    page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
    lines = [f"🗃 إدارة الدفعات — صفحة {page + 1} من {pages} — {fmt_int(len(rows))} دفعة", LINE]
    btns = [[("➕ دفعة جديدة", f"{P}bat:new")]]
    for b in page_rows:
        n = db.execute("SELECT COUNT(*) FROM nb_batch_files WHERE batch_id=?", (b["id"],)).fetchone()[0]
        lines.append(f"#{b['id']} {b['name']} — حالة: {b['status']} — ملفات: {fmt_int(n)} — أنشأها: {b['created_by']} — {ago(b['created_at'])}")
        btns.append([(f"📦 #{b['id']} {b['name']}", f"{P}bat:d:{b['id']}")])
    if not page_rows:
        lines.append("لا دفعات بعد.")
    pg = pager(page, pages, f"{P}bat")
    if pg:
        btns.append(pg)
    return "\n".join(lines), btns


def _bat_files(db, bid):
    return db.execute(
        "SELECT f.* FROM files f JOIN nb_batch_files bf ON bf.file_id=f.id "
        "WHERE bf.batch_id=? ORDER BY f.id", (bid,)).fetchall()


def _bat_detail(db, bid):
    b = _row(db, "SELECT * FROM nb_batches WHERE id=?", (bid,))
    if b is None:
        return "🗃 إدارة الدفعات\nالدفعة غير موجودة.", []
    files = _bat_files(db, bid)
    counts = {}
    for f in files:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    lines = [f"🗃 الدفعة #{b['id']} {b['name']} — حالة: {b['status']} — أنشأها: {b['created_by']} — {ago(b['created_at'])}",
             f"عدد الملفات: {fmt_int(len(files))} — " + ("، ".join(f"{s}: {n}" for s, n in sorted(counts.items())) if counts else "لا ملفات"), LINE]
    btns = []
    for f in files:
        lines.append(_fline(f))
    btns.append([("🔁 إعادة ملفات الفاشلة", f"{P}bat:retry:{bid}"),
                 ("📊 تقرير الدفعة", f"{P}bat:rep:{bid}"),
                 ("🧪 فحص جماعي", f"{P}bat:chk:{bid}")])
    btns.append([("⏹ إيقاف الدفعة", f"{P}bat:stop:{bid}")])
    return "\n".join(lines), btns


# ---------------------------------------------------------------- cmp
def _prev_file(db, f):
    return _row(db, "SELECT * FROM files WHERE original_name=? AND id<? ORDER BY id DESC LIMIT 1",
                (f["original_name"], f["id"]))


def _cmp_text(db, fid):
    f = _file(db, fid)
    if f is None:
        return "🔎 مقارنة نسخ الملفات\nالملف غير موجود.", []
    prev = _prev_file(db, f)
    if prev is None:
        return (f"🔎 مقارنة نسخ الملفات\n{_fline(f)}\n{LINE}\nلا توجد نسخة سابقة بنفس الاسم للمقارنة.", [])
    old_cols = [r[0] for r in db.execute(
        "SELECT name FROM fields WHERE file_id=? ORDER BY position, id", (prev["id"],)).fetchall()]
    new_cols = [r[0] for r in db.execute(
        "SELECT name FROM fields WHERE file_id=? ORDER BY position, id", (fid,)).fetchall()]
    old_n, new_n = prev["record_count"] or 0, f["record_count"] or 0
    delta, pct = new_n - old_n, (100.0 * (new_n - old_n) / old_n) if old_n else 0.0
    lines = ["🔎 مقارنة نسخ الملفات",
             f"السابقة: #{prev['id']} — {prev['original_name']} — {ago(prev['received_at'])}",
             f"الجديدة: #{f['id']} — {f['original_name']} — {ago(f['received_at'])}", LINE,
             f"النوع المكتشف: {prev['detected_type'] or '-'} ← {f['detected_type'] or '-'}"
             + (" (تغيّر)" if (prev["detected_type"] or "") != (f["detected_type"] or "") else " (دون تغيّر)"),
             f"عدد السجلات: {fmt_int(old_n)} ← {fmt_int(new_n)} (فرق {delta:+d}، {pct:+.1f}%)",
             f"الحالة: {prev['status']} ← {f['status']}"]
    added = [c for c in new_cols if c not in old_cols]
    removed = [c for c in old_cols if c not in new_cols]
    lines.append("الأعمدة المضافة: " + ("، ".join(added) if added else "لا شيء"))
    lines.append("الأعمدة المحذوفة: " + ("، ".join(removed) if removed else "لا شيء"))
    lines.append("ملخص أعداد ومخطط فقط — لا بيانات سجلات.")
    btns = [[("✅ اعتماد النسخة الجديدة", f"{P}cmp:adopt:{fid}"),
             ("⏮ الاحتفاظ بالسابقة", f"{P}cmp:keep:{fid}")]]
    return "\n".join(lines), btns


# ---------------------------------------------------------------- que
def _que_list(db, page):
    has_priority = "priority" in _job_cols(db)
    rows = db.execute(
        "SELECT * FROM jobs WHERE status IN ('queued','running','paused') ORDER BY id").fetchall()
    page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
    lines = [f"⚙️ طابور المعالجة — صفحة {page + 1} من {pages} — {fmt_int(len(rows))} مهمة بانتظار/جارية", LINE]
    if has_priority:
        lines.append("الأولوية: عمود priority موجود.")
    else:
        lines.append("لا يوجد عمود أولوية — الترتيب FIFO (حسب id).")
    lines.append("ملاحظة: إيقاف/إلغاء مهمة جارية يُسجَّل كطلب حالة بالجدول؛ إيقاف العامل الحي فعليًا يتطلب ربط JobQueue الحي (parent wiring).")
    btns = []
    for j in page_rows:
        lines.append(f"JOB-{j['id']:06d} {j['kind']} — {j['status']} — {j['progress']:.0f}% — أنشأها: {j['created_by']}")
        btns.append([(f"JOB-{j['id']:06d} {j['kind']}", f"{P}que:j:{j['id']}")])
    if not page_rows:
        lines.append("لا مهام بانتظار/جارية.")
    pg = pager(page, pages, f"{P}que")
    if pg:
        btns.append(pg)
    return "\n".join(lines), btns


def _que_control(db, user, jid, action):
    """Direct table transition mirroring JobQueue.control; returns (text, ok)."""
    j = _row(db, "SELECT * FROM jobs WHERE id=?", (jid,))
    if j is None:
        return "⚙️ طابور المعالجة\nالمهمة غير موجودة.", False
    st = j["status"]
    new, note = None, ""
    if action == "pause":
        if st == "queued":
            new = "paused"
        elif st == "running":
            new = "paused"
            note = " (طلب حالة للعامل الحي — الربط الحي parent wiring)"
        else:
            return f"⚙️ طابور المعالجة\nلا تغيير: حالة JOB-{jid:06d} الحالية ({st}) لا تقبل الإيقاف المؤقت.", False
    elif action == "resume":
        if st in ("paused", "interrupted"):
            new = "queued"
        else:
            return f"⚙️ طابور المعالجة\nلا تغيير: حالة JOB-{jid:06d} الحالية ({st}) لا تقبل الاستئناف.", False
    elif action == "retry":
        if st in ("failed", "interrupted", "cancelled"):
            new = "queued"
        else:
            return f"⚙️ طابور المعالجة\nلا تغيير: حالة JOB-{jid:06d} الحالية ({st}) لا تقبل إعادة المحاولة.", False
    elif action == "cancel":
        if st in ("queued", "paused", "interrupted"):
            new = "cancelled"
        elif st == "running":
            new = "cancelled"
            note = " (طلب حالة للعامل الحي — الربط الحي parent wiring)"
        else:
            return f"⚙️ طابور المعالجة\nلا تغيير: حالة JOB-{jid:06d} الحالية ({st}) لا تقبل الإلغاء.", False
    if action == "retry":
        db.execute("UPDATE jobs SET status=?, error=NULL, finished_at=NULL WHERE id=?", (new, jid))
    elif new == "cancelled":
        db.execute("UPDATE jobs SET status=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (new, jid))
    else:
        db.execute("UPDATE jobs SET status=? WHERE id=?", (new, jid))
    _commit(db)
    audit_nb(db, user.telegram_id, f"nb_p2_que_{action}", "job", jid, {"from": st, "to": new})
    _commit(db)
    return f"⚙️ طابور المعالجة\nJOB-{jid:06d}: {st} ← {new}{note}", True


# ---------------------------------------------------------------- idx
def _standard_pool(db):
    pool = set()
    for r in db.execute("SELECT DISTINCT standard_field FROM field_mappings WHERE standard_field IS NOT NULL AND standard_field<>''").fetchall():
        pool.add(r[0])
    try:
        for r in db.execute("SELECT DISTINCT standard_field FROM schema_synonyms").fetchall():
            pool.add(r[0])
    except sqlite3.Error:
        pass
    return sorted(pool)


def _suggest(column, pool):
    n = _norm_name(column)
    best, best_score = None, 0.0
    for cand in pool:
        c = _norm_name(cand)
        if not c:
            continue
        score = difflib.SequenceMatcher(None, n, c).ratio()
        if n == c:
            score = 1.0
        elif n.startswith(c) or c.startswith(n):
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = cand, score
    return (best, best_score) if best_score >= 0.5 else (None, best_score)


def _idx_list(db, page):
    rows = db.execute(
        "SELECT fl.id AS field_id, fl.name AS col_name, f.original_name AS file_name, f.id AS file_id, "
        "fm.standard_field AS std FROM fields fl JOIN files f ON f.id=fl.file_id "
        "LEFT JOIN field_mappings fm ON fm.field_id=fl.id "
        "WHERE fm.field_id IS NULL OR fm.standard_field IS NULL OR fm.standard_field='' "
        "ORDER BY fl.id").fetchall()
    page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
    lines = [f"🗂 فهرس الملفات والحقول — أعمدة غير معروفة: {fmt_int(len(rows))} — صفحة {page + 1} من {pages}", LINE]
    btns = []
    for r in page_rows:
        lines.append(f"عمود #{r['field_id']}: {r['col_name']} — ملف: {r['file_name']} (#{r['file_id']})")
        btns.append([(f"🔍 #{r['field_id']} {r['col_name']}", f"{P}idx:d:{r['field_id']}")])
    if not page_rows:
        lines.append("لا أعمدة غير معروفة.")
    btns.append([("↩️ تراجع عن آخر تغيير", f"{P}idx:undo")])
    pg = pager(page, pages, f"{P}idx")
    if pg:
        btns.append(pg)
    return "\n".join(lines), btns


def _idx_detail(db, field_id):
    r = _row(db, "SELECT fl.*, f.original_name AS file_name FROM fields fl JOIN files f ON f.id=fl.file_id WHERE fl.id=?", (field_id,))
    if r is None:
        return "🗂 فهرس الملفات والحقول\nالعمود غير موجود.", []
    sug, score = _suggest(r["name"], _standard_pool(db))
    nrec = db.execute("SELECT COUNT(*) FROM records WHERE file_id=?", (r["file_id"],)).fetchone()[0]
    try:
        nval = db.execute(
            "SELECT COUNT(*) FROM record_values WHERE field_id=? "
            "AND normalized_value IS NOT NULL AND TRIM(normalized_value) <> ''", (field_id,)).fetchone()[0]
    except sqlite3.Error:
        nval = 0
    lines = [f"🗂 العمود #{field_id}: {r['name']} — ملف: {r['file_name']} (#{r['file_id']})",
             f"معاينة الربط المقترح: ستؤثر على {fmt_int(nrec)} سجلًا بالملف، منها {fmt_int(nval)} قيمة غير فارغة لهذا العمود (أعداد فقط).",
             f"الاقتراح: {sug or 'لا يوجد اقتراح قريب'}" + (f" (تشابه {score:.2f})" if sug else ""),
             "الاقتراح فقط — لا يُطبَّق تلقائيًا."]
    btns = []
    if sug:
        btns.append([("✅ اعتماد الربط", f"{P}idx:ap:{field_id}")])
    return "\n".join(lines), btns


# ---------------------------------------------------------------- auth
def _auth_list(db, page):
    rows = db.execute("SELECT * FROM files ORDER BY id DESC").fetchall()
    page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
    lines = [f"🛡 مراجعة الاستخدام المصرح — صفحة {page + 1} من {pages} — {fmt_int(len(rows))} ملفًا", LINE,
             "ملاحظة: حظر الملف المعزول من البحث يُربط عند الإدخال (parent wiring)؛ سجلات العزل هنا حقيقية وقابلة للاستعلام."]
    btns = []
    for f in page_rows:
        iso = _row(db, "SELECT reason FROM nb_isolated_files WHERE file_id=?", (f["id"],))
        flag = f" — معزول ({iso['reason']})" if iso else ""
        lines.append(f"#{f['id']} {f['original_name']} — حالة: {f['status']}{flag} — استلام: {ago(f['received_at'])}")
        btns.append([(f"🛡 #{f['id']} {f['original_name']}", f"{P}auth:f:{f['id']}")])
    pg = pager(page, pages, f"{P}auth")
    if pg:
        btns.append(pg)
    return "\n".join(lines), btns


def _auth_detail(db, fid):
    f = _file(db, fid)
    if f is None:
        return "🛡 مراجعة الاستخدام المصرح\nالملف غير موجود.", []
    job = _row(db, "SELECT created_by FROM jobs WHERE file_id=? ORDER BY id DESC LIMIT 1", (fid,))
    uploader = job["created_by"] if job else "غير مسجّل (لا يوجد عمود رافع بجدول الملفات)"
    iso = _row(db, "SELECT * FROM nb_isolated_files WHERE file_id=?", (fid,))
    rev = _row(db, "SELECT * FROM nb_settings_log WHERE setting=? ORDER BY id DESC LIMIT 1", (f"file_review:{fid}",))
    lines = ["🛡 مراجعة ملف", _fline(f),
             f"الرافع (من آخر مهمة للملف إن وجدت): {uploader}",
             f"العزل: {'معزول — سبب: ' + iso['reason'] if iso else 'غير معزول'}",
             f"آخر قرار مراجعة: {rev['new_value'] if rev else 'لا يوجد'}"]
    btns = [[("⏸ تعليق المعالجة", f"{P}auth:sus:{fid}"),
             ("🔒 عزل الملف", f"{P}auth:iso:{fid}")],
            [("📝 تسجيل قرار مراجعة", f"{P}auth:dec:{fid}")]]
    return "\n".join(lines), btns


# ---------------------------------------------------------------- dispatch
def _dispatch(db, user, parts):
    key = parts[0]
    rest = parts[1:]
    # ---- imp
    if key == "imp":
        if not rest:
            _t, _b = _imp_list(db, 0)
            return _screen(db, key, _t, _b)
        if rest[0] == "f" and len(rest) == 2:
            text, btns = _imp_detail(db, _int(rest[1]))
            return _screen(db, key, text, btns, back=f"{P}imp")
        if rest[0] == "chk" and len(rest) == 2:
            text, _i, _n = _check_text(db, _int(rest[1]))
            return _screen(db, key, text, [], back=f"{P}imp:f:{rest[1]}")
        if rest[0] == "ap" and len(rest) == 2:
            fid = _int(rest[1])
            f = _file(db, fid)
            if f is None:
                return _screen(db, key, "📥 مركز استيراد الملفات\nالملف غير موجود.", [], back=f"{P}imp")
            job, created = _submit_ingest(db, user, fid)
            if created:
                audit_nb(db, user.telegram_id, "nb_p2_imp_approve", "file", fid, {"job_id": job["id"]})
                _commit(db)
                text = f"📥 مركز استيراد الملفات\nتم اعتماد المعالجة للملف #{fid} — مهمة جديدة JOB-{job['id']:06d} (queued)."
            else:
                text = (f"📥 مركز استيراد الملفات\nتوجد مهمة معالجة غير منتهية للملف #{fid}: "
                        f"JOB-{job['id']:06d} ({job['status']}) — لم تُقدَّم مهمة جديدة.")
            return _screen(db, key, text, [], back=f"{P}imp:f:{fid}")
        if rest[0].isdigit():
            _t, _b = _imp_list(db, _int(rest[0]))
            return _screen(db, key, _t, _b)
        return _screen(db, key, "📥 مركز استيراد الملفات\nإجراء غير معروف.", [], back=f"{P}imp")
    # ---- bat
    if key == "bat":
        if not rest:
            _t, _b = _bat_list(db, 0)
            return _screen(db, key, _t, _b)
        if rest[0] == "new":
            set_pending(db, user.telegram_id, "nb_p2_bat_new")
            return _screen(db, key, "🗃 دفعة جديدة\nأرسل اسم الدفعة كنص وسيتم إنشاؤها.", [], back=f"{P}bat")
        if rest[0] == "d" and len(rest) == 2:
            text, btns = _bat_detail(db, _int(rest[1]))
            return _screen(db, key, text, btns, back=f"{P}bat")
        if rest[0] == "rep" and len(rest) == 2:
            bid = _int(rest[1])
            b = _row(db, "SELECT * FROM nb_batches WHERE id=?", (bid,))
            if b is None:
                return _screen(db, key, "🗃 إدارة الدفعات\nالدفعة غير موجودة.", [], back=f"{P}bat")
            files = _bat_files(db, bid)
            counts = {}
            for f in files:
                counts[f["status"]] = counts.get(f["status"], 0) + 1
            lines = [f"📊 تقرير الدفعة #{bid} {b['name']} — حالة: {b['status']}",
                     "أعداد حسب الحالة: " + ("، ".join(f"{s}: {n}" for s, n in sorted(counts.items())) if counts else "لا ملفات"), LINE]
            lines += [_fline(f) for f in files]
            lines.append("تقرير أعداد وأسماء ملفات فقط — لا قيم سجلات.")
            return _screen(db, key, "\n".join(lines), [], back=f"{P}bat:d:{bid}")
        if rest[0] == "chk" and len(rest) == 2:
            bid = _int(rest[1])
            files = _bat_files(db, bid)
            total_issues, per_file = 0, []
            for f in files:
                _issues, _notes = _check_file(db, f["id"])
                total_issues += len(_issues)
                per_file.append(f"#{f['id']} {f['original_name']}: {len(_issues)} مشكلة")
            lines = [f"🧪 فحص جماعي للدفعة #{bid} — {fmt_int(len(files))} ملفًا — إجمالي المشاكل: {total_issues}", LINE] + per_file
            return _screen(db, key, "\n".join(lines), [], back=f"{P}bat:d:{bid}")
        if rest[0] == "retry" and len(rest) == 2:
            bid = _int(rest[1])
            b = _row(db, "SELECT * FROM nb_batches WHERE id=?", (bid,))
            if b is None:
                return _screen(db, key, "🗃 إدارة الدفعات\nالدفعة غير موجودة.", [], back=f"{P}bat")
            requeued, skipped = 0, 0
            for f in _bat_files(db, bid):
                if f["status"] != "failed":
                    continue
                _job, created = _submit_ingest(db, user, f["id"])
                if created:
                    db.execute("UPDATE files SET status='queued' WHERE id=?", (f["id"],))
                    requeued += 1
                else:
                    skipped += 1
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_bat_retry", "batch", bid,
                     {"requeued": requeued, "skipped": skipped})
            _commit(db)
            return _screen(db, key, f"🗃 إدارة الدفعات\nإعادة الدفعة #{bid}: أُعيد {requeued} ملفًا، وتُخطي {skipped} ملفًا لوجود مهمة غير منتهية.",
                           [], back=f"{P}bat:d:{bid}")
        if rest[0] == "stop" and len(rest) == 2:
            bid = _int(rest[1])
            return ("🗃 إيقاف الدفعة\n"
                    f"إيقاف الدفعة #{bid}؟ سيُعلَّم وضعها «stopped» فقط — لا يوقف المهام الجارية.",
                    confirm_markup(f"{P}bat:stop:yes:{bid}", f"{P}bat:stop:no:{bid}"))
        if rest[0] == "stop" and len(rest) == 3 and rest[1] == "yes":
            bid = _int(rest[2])
            b = _row(db, "SELECT * FROM nb_batches WHERE id=?", (bid,))
            if b is None:
                return _screen(db, key, "🗃 إدارة الدفعات\nالدفعة غير موجودة.", [], back=f"{P}bat")
            db.execute("UPDATE nb_batches SET status='stopped' WHERE id=?", (bid,))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_bat_stop", "batch", bid, {})
            _commit(db)
            return _screen(db, key, f"🗃 إدارة الدفعات\nتم إيقاف الدفعة #{bid} (stopped). المهام الجارية لم تُوقف — هذا الإيقاف يعلّم الدفعة فقط.",
                           [], back=f"{P}bat:d:{bid}")
        if rest[0] == "stop" and len(rest) == 3 and rest[1] == "no":
            return _screen(db, key, "🗃 إدارة الدفعات\nأُلغي إيقاف الدفعة — لم يتغير شيء.", [], back=f"{P}bat")
        if rest[0].isdigit():
            _t, _b = _bat_list(db, _int(rest[0]))
            return _screen(db, key, _t, _b)
        return _screen(db, key, "🗃 إدارة الدفعات\nإجراء غير معروف.", [], back=f"{P}bat")
    # ---- chk
    if key == "chk":
        if not rest:
            rows = db.execute("SELECT * FROM files ORDER BY id DESC").fetchall()
            page_rows, pages, page = page_slice(rows, 0, PAGE_SIZE)
            lines = [f"🧪 فحص الملفات قبل المعالجة — {fmt_int(len(rows))} ملفًا — صفحة 1 من {pages}", LINE]
            btns = []
            for f in page_rows:
                lines.append(_fline(f))
                btns.append([(f"🧪 #{f['id']} {f['original_name']}", f"{P}chk:f:{f['id']}")])
            pg = pager(0, pages, f"{P}chk")
            if pg:
                btns.append(pg)
            return _screen(db, key, "\n".join(lines), btns)
        if rest[0] == "f" and len(rest) == 2:
            text, _i, _n = _check_text(db, _int(rest[1]))
            return _screen(db, key, text, [], back=f"{P}chk")
        if rest[0] == "b" and len(rest) == 2:
            bid = _int(rest[1])
            files = _bat_files(db, bid)
            total, per_file = 0, []
            for f in files:
                _issues, _notes = _check_file(db, f["id"])
                total += len(_issues)
                per_file.append(f"#{f['id']} {f['original_name']}: {len(_issues)} مشكلة")
            lines = [f"🧪 فحص جماعي للدفعة #{bid} — {fmt_int(len(files))} ملفًا — إجمالي المشاكل: {total}", LINE] + per_file
            return _screen(db, key, "\n".join(lines), [], back=f"{P}chk")
        if rest[0].isdigit():
            page = _int(rest[0])
            rows = db.execute("SELECT * FROM files ORDER BY id DESC").fetchall()
            page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
            lines = [f"🧪 فحص الملفات قبل المعالجة — {fmt_int(len(rows))} ملفًا — صفحة {page + 1} من {pages}", LINE]
            btns = []
            for f in page_rows:
                lines.append(_fline(f))
                btns.append([(f"🧪 #{f['id']} {f['original_name']}", f"{P}chk:f:{f['id']}")])
            pg = pager(page, pages, f"{P}chk")
            if pg:
                btns.append(pg)
            return _screen(db, key, "\n".join(lines), btns)
        return _screen(db, key, "🧪 فحص الملفات قبل المعالجة\nإجراء غير معروف.", [], back=f"{P}chk")
    # ---- cmp
    if key == "cmp":
        if not rest:
            rows = db.execute(
                "SELECT * FROM files WHERE EXISTS (SELECT 1 FROM files p WHERE p.original_name=files.original_name AND p.id<files.id) "
                "ORDER BY id DESC").fetchall()
            page_rows, pages, page = page_slice(rows, 0, PAGE_SIZE)
            lines = [f"🔎 مقارنة نسخ الملفات — {fmt_int(len(rows))} ملفًا له نسخة سابقة — صفحة 1 من {pages}", LINE]
            btns = []
            for f in page_rows:
                lines.append(_fline(f))
                btns.append([(f"🔎 #{f['id']} {f['original_name']}", f"{P}cmp:f:{f['id']}")])
            pg = pager(0, pages, f"{P}cmp")
            if pg:
                btns.append(pg)
            return _screen(db, key, "\n".join(lines), btns)
        if rest[0] == "f" and len(rest) == 2:
            text, btns = _cmp_text(db, _int(rest[1]))
            return _screen(db, key, text, btns, back=f"{P}cmp")
        if rest[0] in ("adopt", "keep") and len(rest) == 2:
            fid = _int(rest[1])
            f = _file(db, fid)
            if f is None:
                return _screen(db, key, "🔎 مقارنة نسخ الملفات\nالملف غير موجود.", [], back=f"{P}cmp")
            decision = "adopt_new" if rest[0] == "adopt" else "keep_previous"
            audit_nb(db, user.telegram_id, "nb_p2_cmp_decision", "file", fid, {"decision": decision})
            _commit(db)
            verb = "اعتماد النسخة الجديدة" if rest[0] == "adopt" else "الاحتفاظ بالنسخة السابقة"
            return _screen(db, key, f"🔎 مقارنة نسخ الملفات\nتم تسجيل قرار: {verb} للملف #{fid} (ملاحظة تدقيق فقط — لا يستبدل أي بيانات).",
                           [], back=f"{P}cmp:f:{fid}")
        if rest[0].isdigit():
            page = _int(rest[0])
            rows = db.execute(
                "SELECT * FROM files WHERE EXISTS (SELECT 1 FROM files p WHERE p.original_name=files.original_name AND p.id<files.id) "
                "ORDER BY id DESC").fetchall()
            page_rows, pages, page = page_slice(rows, page, PAGE_SIZE)
            lines = [f"🔎 مقارنة نسخ الملفات — {fmt_int(len(rows))} ملفًا له نسخة سابقة — صفحة {page + 1} من {pages}", LINE]
            btns = []
            for f in page_rows:
                lines.append(_fline(f))
                btns.append([(f"🔎 #{f['id']} {f['original_name']}", f"{P}cmp:f:{f['id']}")])
            pg = pager(page, pages, f"{P}cmp")
            if pg:
                btns.append(pg)
            return _screen(db, key, "\n".join(lines), btns)
        return _screen(db, key, "🔎 مقارنة نسخ الملفات\nإجراء غير معروف.", [], back=f"{P}cmp")
    # ---- que
    if key == "que":
        if not rest:
            _t, _b = _que_list(db, 0)
            return _screen(db, key, _t, _b)
        if rest[0] == "j" and len(rest) == 2:
            jid = _int(rest[1])
            j = _row(db, "SELECT * FROM jobs WHERE id=?", (jid,))
            if j is None:
                return _screen(db, key, "⚙️ طابور المعالجة\nالمهمة غير موجودة.", [], back=f"{P}que")
            lines = [f"⚙️ JOB-{jid:06d} {j['kind']} — حالة: {j['status']} — تقدم: {j['progress']:.0f}% — أنشأها: {j['created_by']} — ملف: {j['file_id']}",
                     f"خطأ: {(j['error'] or '-')[:200]}"]
            btns = [[("⏸ إيقاف مؤقت", f"{P}que:pause:{jid}"),
                     ("▶️ استئناف", f"{P}que:resume:{jid}"),
                     ("🔁 إعادة محاولة", f"{P}que:retry:{jid}")],
                    [("🚫 إلغاء المهمة", f"{P}que:cancel:{jid}")]]
            return _screen(db, key, "\n".join(lines), btns, back=f"{P}que")
        if rest[0] in ("pause", "resume", "retry") and len(rest) == 2:
            text, _ok = _que_control(db, user, _int(rest[1]), rest[0])
            return _screen(db, key, text, [], back=f"{P}que:j:{rest[1]}")
        if rest[0] == "cancel" and len(rest) == 2:
            jid = _int(rest[1])
            return ("⚙️ إلغاء المهمة\n" f"إلغاء JOB-{jid:06d}؟ لا يمكن التراجع عن الإلغاء من هنا إلا بإعادة المحاولة لاحقًا.",
                    confirm_markup(f"{P}que:cancel:yes:{jid}", f"{P}que:cancel:no:{jid}"))
        if rest[0] == "cancel" and len(rest) == 3 and rest[1] == "yes":
            text, _ok = _que_control(db, user, _int(rest[2]), "cancel")
            return _screen(db, key, text, [], back=f"{P}que:j:{rest[2]}")
        if rest[0] == "cancel" and len(rest) == 3 and rest[1] == "no":
            return _screen(db, key, "⚙️ طابور المعالجة\nأُلغي إلغاء المهمة — لم يتغير شيء.", [], back=f"{P}que")
        if rest[0].isdigit():
            _t, _b = _que_list(db, _int(rest[0]))
            return _screen(db, key, _t, _b)
        return _screen(db, key, "⚙️ طابور المعالجة\nإجراء غير معروف.", [], back=f"{P}que")
    # ---- idx
    if key == "idx":
        if not rest:
            _t, _b = _idx_list(db, 0)
            return _screen(db, key, _t, _b)
        if rest[0] == "d" and len(rest) == 2:
            text, btns = _idx_detail(db, _int(rest[1]))
            return _screen(db, key, text, btns, back=f"{P}idx")
        if rest[0] == "ap" and len(rest) == 2:
            field_id = _int(rest[1])
            frow = _row(db, "SELECT name FROM fields WHERE id=?", (field_id,))
            sug, _score = _suggest(frow["name"], _standard_pool(db)) if frow else (None, 0)
            if not sug:
                return _screen(db, key, "🗂 فهرس الملفات والحقول\nلا يوجد اقتراح لاعتماده لهذا العمود.", [], back=f"{P}idx:d:{field_id}")
            return ("🗂 اعتماد الربط\n" f"اعتماد ربط العمود #{field_id} بالحقل القياسي «{sug}»؟",
                    confirm_markup(f"{P}idx:ap:yes:{field_id}", f"{P}idx:ap:no:{field_id}"))
        if rest[0] == "ap" and len(rest) == 3 and rest[1] == "yes":
            field_id = _int(rest[2])
            row = _row(db, "SELECT name FROM fields WHERE id=?", (field_id,))
            if row is None:
                return _screen(db, key, "🗂 فهرس الملفات والحقول\nالعمود غير موجود.", [], back=f"{P}idx")
            sug, _score = _suggest(row["name"], _standard_pool(db))
            if not sug:
                return _screen(db, key, "🗂 فهرس الملفات والحقول\nلا يوجد اقتراح لاعتماده.", [], back=f"{P}idx:d:{field_id}")
            cur = _row(db, "SELECT standard_field FROM field_mappings WHERE field_id=?", (field_id,))
            old = cur["standard_field"] if cur else ""
            db.execute(
                "INSERT INTO field_mappings(field_id, standard_field, confidence, reason, status) VALUES (?, ?, 'MEDIUM', 'nb_p2 suggestion approved', 'confirmed') "
                "ON CONFLICT(field_id) DO UPDATE SET standard_field=excluded.standard_field, confidence='MEDIUM', reason='nb_p2 suggestion approved', status='confirmed'",
                (field_id, sug))
            db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?, ?, ?, ?)",
                       (user.telegram_id, f"field_mapping:{field_id}", old or "", sug))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_idx_approve", "field", field_id, {"standard_field": sug})
            _commit(db)
            return _screen(db, key, f"🗂 فهرس الملفات والحقول\nتم اعتماد ربط العمود #{field_id} بالحقل «{sug}» وسُجّل بسجل الإعدادات والتدقيق.",
                           [], back=f"{P}idx:d:{field_id}")
        if rest[0] == "ap" and len(rest) == 3 and rest[1] == "no":
            return _screen(db, key, "🗂 فهرس الملفات والحقول\nأُلغي اعتماد الربط — لم يتغير شيء.", [], back=f"{P}idx")
        if rest[0] == "undo" and len(rest) == 1:
            last = _row(db, "SELECT * FROM nb_settings_log WHERE setting LIKE 'field_mapping:%' ORDER BY id DESC LIMIT 1")
            if last is None:
                return _screen(db, key, "🗂 فهرس الملفات والحقول\nلا يوجد تغيير ربط للتراجع عنه.", [], back=f"{P}idx")
            return ("🗂 تراجع عن آخر تغيير\n"
                    f"التراجع عن {last['setting']}: «{last['new_value']}» ← «{last['old_value'] or '(فارغ)'}»؟",
                    confirm_markup(f"{P}idx:undo:yes", f"{P}idx:undo:no"))
        if rest[0] == "undo" and len(rest) == 2 and rest[1] == "yes":
            last = _row(db, "SELECT * FROM nb_settings_log WHERE setting LIKE 'field_mapping:%' ORDER BY id DESC LIMIT 1")
            if last is None:
                return _screen(db, key, "🗂 فهرس الملفات والحقول\nلا يوجد تغيير ربط للتراجع عنه.", [], back=f"{P}idx")
            field_id = _int(last["setting"].split(":", 1)[1])
            cur = _row(db, "SELECT standard_field FROM field_mappings WHERE field_id=?", (field_id,))
            cur_val = cur["standard_field"] if cur else ""
            old = last["old_value"] or None
            db.execute("UPDATE field_mappings SET standard_field=?, status='auto' WHERE field_id=?", (old, field_id))
            db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?, ?, ?, ?)",
                       (user.telegram_id, last["setting"], cur_val or "", last["old_value"] or ""))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_idx_undo", "field", field_id, {"reverted_to": last["old_value"] or ""})
            _commit(db)
            return _screen(db, key, f"🗂 فهرس الملفات والحقول\nتم التراجع عن ربط العمود #{field_id} إلى «{last['old_value'] or '(فارغ)'}».",
                           [], back=f"{P}idx")
        if rest[0] == "undo" and len(rest) == 2 and rest[1] == "no":
            return _screen(db, key, "🗂 فهرس الملفات والحقول\nأُلغي التراجع — لم يتغير شيء.", [], back=f"{P}idx")
        if rest[0].isdigit():
            _t, _b = _idx_list(db, _int(rest[0]))
            return _screen(db, key, _t, _b)
        return _screen(db, key, "🗂 فهرس الملفات والحقول\nإجراء غير معروف.", [], back=f"{P}idx")
    # ---- auth
    if key == "auth":
        if not rest:
            return _auth_list(db, 0)
        if rest[0] == "f" and len(rest) == 2:
            text, btns = _auth_detail(db, _int(rest[1]))
            return _screen(db, key, text, btns, back=f"{P}auth")
        if rest[0] == "sus" and len(rest) == 2:
            fid = _int(rest[1])
            if _file(db, fid) is None:
                return _screen(db, key, "🛡 مراجعة الاستخدام المصرح\nالملف غير موجود.", [], back=f"{P}auth")
            iso = _row(db, "SELECT * FROM nb_isolated_files WHERE file_id=?", (fid,))
            if iso and iso["reason"] == "review":
                return _screen(db, key, f"🛡 مراجعة الاستخدام المصرح\nالملف #{fid} معلّق للمراجعة أصلًا.", [], back=f"{P}auth:f:{fid}")
            db.execute("INSERT INTO nb_isolated_files(file_id, reason, by_user) VALUES (?, 'review', ?) "
                       "ON CONFLICT(file_id) DO UPDATE SET reason='review', by_user=excluded.by_user", (fid, user.telegram_id))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_auth_suspend", "file", fid, {"reason": "review"})
            _commit(db)
            return _screen(db, key, f"🛡 مراجعة الاستخدام المصرح\nتم تعليق معالجة الملف #{fid} (review-pending). الحظر من البحث يُربط عند الإدخال (parent wiring).",
                           [], back=f"{P}auth:f:{fid}")
        if rest[0] == "iso" and len(rest) == 2:
            fid = _int(rest[1])
            return ("🛡 عزل الملف\n" f"عزل الملف #{fid}؟ سيُسجَّل بجدول الملفات المعزولة.",
                    confirm_markup(f"{P}auth:iso:yes:{fid}", f"{P}auth:iso:no:{fid}"))
        if rest[0] == "iso" and len(rest) == 3 and rest[1] == "yes":
            fid = _int(rest[2])
            if _file(db, fid) is None:
                return _screen(db, key, "🛡 مراجعة الاستخدام المصرح\nالملف غير موجود.", [], back=f"{P}auth")
            db.execute("INSERT INTO nb_isolated_files(file_id, reason, by_user) VALUES (?, 'isolated', ?) "
                       "ON CONFLICT(file_id) DO UPDATE SET reason='isolated', by_user=excluded.by_user", (fid, user.telegram_id))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p2_auth_isolate", "file", fid, {"reason": "isolated"})
            _commit(db)
            return _screen(db, key, f"🛡 مراجعة الاستخدام المصرح\nتم عزل الملف #{fid} وسُجّل بالتدقيق. الحظر من البحث يُربط عند الإدخال (parent wiring).",
                           [], back=f"{P}auth:f:{fid}")
        if rest[0] == "iso" and len(rest) == 3 and rest[1] == "no":
            return _screen(db, key, "🛡 مراجعة الاستخدام المصرح\nأُلغي عزل الملف — لم يتغير شيء.", [], back=f"{P}auth")
        if rest[0] == "dec" and len(rest) == 2:
            fid = _int(rest[1])
            set_pending(db, user.telegram_id, "nb_p2_auth_dec", {"file_id": fid})
            return _screen(db, key, f"🛡 تسجيل قرار مراجعة\nأرسل نص القرار للملف #{fid} وسيُسجَّل بسجل الإعدادات والتدقيق.", [], back=f"{P}auth:f:{fid}")
        if rest[0].isdigit():
            return _auth_list(db, _int(rest[0]))
        return _screen(db, key, "🛡 مراجعة الاستخدام المصرح\nإجراء غير معروف.", [], back=f"{P}auth")
    return None


def handle(ctx, user, chat, mid, parts) -> bool:
    ensure_nb_tables(ctx.db)
    if not parts or parts[0] not in _KEYS:
        return False
    try:
        from .nb_store import require
        require(user, "manage")
        result = _dispatch(ctx.db, user, parts)
        if result is None:
            return False
        text, markup = result
        ctx.show(chat, mid, text, markup)
        return True
    except PermissionDenied as exc:
        try:
            ctx.deny(user.telegram_id, str(exc) or "manage")
        except Exception:
            pass
        ctx.show(chat, mid, "⛔ مرفوض: هذه الشاشة للأدمن فقط.", kb(nav()))
        return True
    except AppError as exc:
        ctx.show(chat, mid, f"⚠️ خطأ: {exc}", kb(nav()))
        return True


def handle_text(ctx, user, chat, text) -> bool:
    ensure_nb_tables(ctx.db)
    try:
        from .nb_store import require
        require(user, "manage")
    except PermissionDenied as exc:
        try:
            ctx.deny(user.telegram_id, str(exc) or "manage")
        except Exception:
            pass
        ctx.send(chat, "⛔ مرفوض: هذه الشاشة للأدمن فقط.", kb(nav()))
        return True
    row = ctx.db.execute("SELECT action, payload FROM nb_pending_text WHERE user_id=?", (user.telegram_id,)).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    action = row["action"]
    if action == "nb_p2_bat_new":
        name = (text or "").strip()[:200]
        if not name:
            return False
        ctx.db.execute("DELETE FROM nb_pending_text WHERE user_id=?", (user.telegram_id,))
        cur = ctx.db.execute("INSERT INTO nb_batches(name, created_by, status) VALUES (?, ?, 'open')",
                             (name, user.telegram_id))
        _commit(ctx.db)
        audit_nb(ctx.db, user.telegram_id, "nb_p2_bat_new", "batch", cur.lastrowid, {"name": name})
        _commit(ctx.db)
        ctx.send(chat, f"🗃 إدارة الدفعات\nتم إنشاء الدفعة #{cur.lastrowid} «{name}».",
                 kb([("📦 فتح الدفعة", f"{P}bat:d:{cur.lastrowid}")], nav()))
        return True
    if action == "nb_p2_auth_dec":
        fid = _int(payload.get("file_id"))
        ctx.db.execute("DELETE FROM nb_pending_text WHERE user_id=?", (user.telegram_id,))
        ctx.db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?, ?, '', ?)",
                       (user.telegram_id, f"file_review:{fid}", (text or "").strip()[:500]))
        _commit(ctx.db)
        audit_nb(ctx.db, user.telegram_id, "nb_p2_auth_decision", "file", fid, {})
        _commit(ctx.db)
        ctx.send(chat, f"🛡 مراجعة الاستخدام المصرح\nتم تسجيل قرار المراجعة للملف #{fid}.",
                 kb([("🛡 فتح الملف", f"{P}auth:f:{fid}")], nav()))
        return True
    return False
