"""Phase 5 — matching + performance (nb:p5:*).

Admin-only (perm "manage") screens. Counts, ids, statuses and column/field
NAMES only — record field VALUES are never read (record_values is never
queried here) and never shown. Secrets are never shown: error text is
scrubbed and truncated to 200 chars.

Deliberate limits (stated loudly on the screens themselves):
- imp: preview only, never merges; approval stays on the existing er:c button.
- und: reversal-record + audit only. The schema has no "reversed" marker
  (match_decisions allows only never_match/confirmed; entity_matches
  decision has no reversed value) and history rows are never deleted, so
  physical unmerge is parent wiring via the existing er.unmerge flow.
- wrk: worker threads/processes are not visible from the DB; only running
  jobs are shown, no invented PIDs.
- cln: deletes DB rows only, never disk files; indexed/partial files untouched.
- dbc: read-only check; no repair is performed here.
- rul/spd: synthetic / measured numbers only, labelled as such.
"""
from __future__ import annotations

import re
import resource
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from ..errors import PermissionDenied
from .nb_store import (
    audit_nb,
    confirm_markup,
    ensure_nb_tables,
    maintenance_active,
    page_slice,
    require,
)
from .ui import LINE, ago, fmt_int, fmt_size, kb, nav, pager

PREFIX = "nb:p5:"
PAGE_SIZE = 10
STUCK_MINUTES = 60
REVIEW_STATES = ("PROBABLE_MATCH", "POSSIBLE_MATCH", "NEEDS_REVIEW")

BUTTONS = [
    {"key": "rev", "label": "📋 قائمة المراجعات المطلوبة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "imp", "label": "🧪 معاينة أثر المطابقة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "und", "label": "↩️ التراجع عن قرار سابق", "perm": "manage", "confirm": True, "audit": "nb_p5_undo"},
    {"key": "rul", "label": "🧩 اختبار قواعد المطابقة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "cnf", "label": "⚠️ التعارضات غير المحسومة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "chk", "label": "🩺 فحص شامل", "perm": "manage", "confirm": False, "audit": None},
    {"key": "spd", "label": "⚡ اختبار السرعة", "perm": "manage", "confirm": False, "audit": None},
    {"key": "dash", "label": "📈 لوحة الأداء", "perm": "manage", "confirm": False, "audit": None},
    {"key": "wrk", "label": "🧵 تفاصيل العمال", "perm": "manage", "confirm": False, "audit": None},
    {"key": "err", "label": "🧯 تشخيص آخر خطأ", "perm": "manage", "confirm": False, "audit": None},
    {"key": "clp", "label": "🧹 معاينة التنظيف", "perm": "manage", "confirm": False, "audit": None},
    {"key": "cln", "label": "♻️ تنظيف آمن", "perm": "manage", "confirm": True, "audit": "nb_p5_cleanup"},
    {"key": "dbc", "label": "🗃 فحص قاعدة البيانات", "perm": "manage", "confirm": False, "audit": None},
    {"key": "feat", "label": "🧰 اختبار الميزات", "perm": "manage", "confirm": False, "audit": None},
    {"key": "mnt", "label": "🛑 وضع الصيانة", "perm": "manage", "confirm": True, "audit": "nb_p5_maintenance_on"},
    {"key": "end", "label": "▶️ إنهاء الصيانة", "perm": "manage", "confirm": True, "audit": "nb_p5_maintenance_off"},
    {"key": "rep", "label": "📄 تقرير صحة النظام", "perm": "manage", "confirm": False, "audit": "nb_p5_health_report"},
]

_KEYS = {b["key"] for b in BUTTONS} | {"hub"}
_LABEL = {b["key"]: b["label"] for b in BUTTONS}


# ---------------------------------------------------------------- helpers
def _tables(db):
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _cols(db, table):
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _count(db, sql, args=()):
    try:
        return int(db.execute(sql, args).fetchone()[0])
    except (sqlite3.Error, TypeError, IndexError):
        return 0


def _rows(db, sql, args=()):
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def _one(db, sql, args=()):
    try:
        r = db.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
    except sqlite3.Error:
        return None


def _commit(db):
    try:
        db.commit()
    except sqlite3.Error:
        pass


def _banner(db):
    try:
        if maintenance_active(db):
            return "🛑 وضع الصيانة نشط\n"
    except sqlite3.Error:
        pass
    return ""


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _page(parts, idx):
    try:
        return max(0, int(parts[idx]))
    except (IndexError, ValueError):
        return 0


def _conflict_fields(raw):
    """Field NAMES only from an entity_matches.conflicts JSON blob."""
    import json
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [str(c.get("field")) for c in data if isinstance(c, dict) and c.get("field")]
    if isinstance(data, dict):
        return [str(k) for k in data.keys()]
    return []


def _scrub(text):
    s = str(text or "")
    try:
        from ..audit import _scrub as _a_scrub
        s = _a_scrub(s)
    except Exception:
        pass
    s = re.sub(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b", "***", s)
    s = re.sub(r"\bsk-[A-Za-z0-9]{10,}\b", "***", s)
    s = re.sub(r"(?i)(token|api_key|apikey|secret|password|passwd)\s*[:=]\s*\S+", r"\1=***", s)
    return s[:200]


def _retention_days(db, kind, default):
    row = _one(db, "SELECT days FROM nb_retention WHERE kind=?", (kind,))
    try:
        return int(row["days"]) if row else default
    except (TypeError, ValueError):
        return default


def _cleanup_preview(db):
    """Preview categories only. Returns dict of (count, size_bytes) + rule text.

    Rule (stated on screen): temp = files with status failed/quarantined
    older than nb_retention(temp) days; reports older than
    nb_retention(reports) days; callback_state older than 7 days.
    """
    now = datetime.now(timezone.utc)
    temp_days = _retention_days(db, "temp", 7)
    rep_days = _retention_days(db, "reports", 90)
    cutoff_temp = now - timedelta(days=temp_days)
    cutoff_rep = now - timedelta(days=rep_days)
    cutoff_cb = now - timedelta(days=7)
    temp_rows = [r for r in _rows(db, "SELECT id, size_bytes, received_at FROM files WHERE status IN ('failed','quarantined')")
                 if (_parse_ts(r.get("received_at")) or now) < cutoff_temp]
    rep_rows = [r for r in _rows(db, "SELECT id, COALESCE(size_bytes,0) AS sz, ts FROM reports")
                if (_parse_ts(r.get("ts")) or now) < cutoff_rep]
    cb_rows = [r for r in _rows(db, "SELECT id, created_at FROM callback_state")
               if (_parse_ts(r.get("created_at")) or now) < cutoff_cb]
    return {
        "temp_days": temp_days, "rep_days": rep_days,
        "temp": temp_rows, "reports": rep_rows, "callbacks": cb_rows,
        "temp_size": sum(int(r.get("size_bytes") or 0) for r in temp_rows),
        "rep_size": sum(int(r.get("sz") or 0) for r in rep_rows),
    }


def _check_summary(db):
    """Shared diagnostics for chk / end / rep. Counts only."""
    tables = _tables(db)
    quick = "غير متحقق"
    try:
        r = db.execute("PRAGMA quick_check").fetchone()
        quick = str(r[0]) if r else "غير متحقق"
    except sqlite3.Error:
        quick = "خطأ في الفحص"
    fts_note = "غير موجود"
    if "record_fts" in tables:
        n = _count(db, "SELECT COUNT(*) FROM record_fts")
        fts_note = (f"موجود ({fmt_int(n)} صفًا) — سلامة FTS الكاملة غير متحقق "
                    "منها بهذا الاستعلام (لم يُجرَ فحص تكامل مخصص)")
    else:
        fts_note = "غير موجود (لم يُنشأ جدول FTS بعد)"
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STUCK_MINUTES)
    stuck = 0
    for j in _rows(db, "SELECT started_at, created_at FROM jobs WHERE status='running'"):
        ts = _parse_ts(j.get("started_at")) or _parse_ts(j.get("created_at"))
        if ts and ts < cutoff:
            stuck += 1
    jobs_total = _count(db, "SELECT COUNT(*) FROM jobs")
    jobs_running = _count(db, "SELECT COUNT(*) FROM jobs WHERE status='running'")
    jobs_queued = _count(db, "SELECT COUNT(*) FROM jobs WHERE status='queued'")
    jobs_failed = _count(db, "SELECT COUNT(*) FROM jobs WHERE status='failed'")
    files_failed = _count(db, "SELECT COUNT(*) FROM files WHERE status IN ('failed','quarantined')")
    reports_pending = 0
    if "reports" in tables and "status" in _cols(db, "reports"):
        reports_pending = _count(db, "SELECT COUNT(*) FROM reports WHERE status NOT IN ('ready')")
    return {
        "quick": quick, "fts": fts_note, "stuck": stuck,
        "jobs_total": jobs_total, "jobs_running": jobs_running,
        "jobs_queued": jobs_queued, "jobs_failed": jobs_failed,
        "files_failed": files_failed, "reports_pending": reports_pending,
    }


def _last_error(db):
    job = _one(db, "SELECT * FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT 1")
    if not job:
        return None, None
    cause = _scrub(job.get("error") or "لا يوجد نص خطأ مسجل")
    low = cause.lower()
    if "locked" in low:
        fix = "أعد المحاولة لاحقًا (قاعدة البيانات مقفلة مؤقتًا)"
    elif "unsupported" in low:
        fix = "صيغة غير مدعومة — تحقق من صيغة الملف قبل إعادة المحاولة"
    elif "timeout" in low or "timed out" in low:
        fix = "أعد المحاولة (انتهت المهلة)"
    elif "no space" in low or "disk full" in low or "no space left" in low:
        fix = "حرّر مساحة تخزين ثم أعد المحاولة"
    else:
        fix = "راجع سجل المهمة (job_events) لتفاصيل إضافية"
    return job, (cause, fix)


def _ai_note(ctx):
    cfg = getattr(ctx, "cfg", None)
    if cfg is None:
        return "غير معروف في هذا السياق (لا إعدادات متاحة)"
    try:
        return "مكوّن" if cfg.ai_enabled else "غير مكوّن"
    except Exception:
        return "غير معروف في هذا السياق"


def _hub_markup():
    rows = []
    keys = [b["key"] for b in BUTTONS]
    for i in range(0, len(keys), 2):
        rows.append([( _LABEL[k], f"{PREFIX}{k}") for k in keys[i:i + 2]])
    rows.append(nav())
    return kb(*rows)


def _deny(ctx, user, chat, mid, what):
    try:
        ctx.deny(user.telegram_id, what)
    except Exception:
        pass
    ctx.show(chat, mid, "⛔ مرفوض: هذه الشاشة للمشرفين فقط (manage).", kb(nav()))
    return True


# ---------------------------------------------------------------- screens
def _screen_hub(ctx, user, chat, mid, parts):
    db = ctx.db
    text = (_banner(db) + "🧩 المطابقة والأداء — لوحة المرحلة 5\n"
            "كل الشاشات أعداد ومعرّفات وحالات فقط؛ لا قيم سجلات ولا أسرار.\n" + LINE)
    ctx.show(chat, mid, text, _hub_markup())
    return True


def _screen_rev(ctx, user, chat, mid, parts):
    db = ctx.db
    page = _page(parts, 1)
    items = []  # (sort_key, kind, row_dict)
    for m in _rows(db, "SELECT id, record_a, record_b, state, confidence FROM entity_matches "
                        "WHERE state='CONFLICT' AND (decision IS NULL OR decision='conflict') "
                        "ORDER BY confidence DESC"):
        items.append((0, "conflict", m))
    for m in _rows(db, "SELECT id, record_a, record_b, state, confidence FROM entity_matches "
                        "WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW') "
                        "ORDER BY confidence DESC"):
        items.append((1, "match", m))
    for r in _rows(db, "SELECT id, user_id, perm FROM nb_perm_requests WHERE status='pending' ORDER BY created_at"):
        items.append((2, "perm_req", r))
    for r in _rows(db, "SELECT id, user_id, target_type FROM nb_del_requests WHERE status='pending' ORDER BY created_at"):
        items.append((2, "del_req", r))
    # items already grouped: conflicts, then review matches, then requests.
    page_rows, pages, page = page_slice(items, page, PAGE_SIZE)
    lines = [_banner(db) + "📋 قائمة المراجعات المطلوبة",
             "الترتيب المعلن: (1) تعارضات CONFLICT أولًا، (2) مطابقات المراجعة "
             "NEEDS_REVIEW / PROBABLE / POSSIBLE بثقة تنازلية، (3) طلبات الإذن "
             "والحذف المعلّقة أخيرًا (الأقدم أولًا).",
             "ملاحظة خطر: التعارضات أولًا لأن دمجها الخاطئ أخطر من تأخير المراجعة.",
             LINE]
    btns = []
    for _s, kind, r in page_rows:
        if kind in ("conflict", "match"):
            lines.append(f"#{r['id']} {r['state']} ثقة {r['confidence']:.2f} سجلان {r['record_a']}↔{r['record_b']}")
            btns.append([(f"مراجعة #{r['id']}", f"er:m:{r['id']}:rv0")])
        elif kind == "perm_req":
            lines.append(f"طلب إذن #{r['id']} مستخدم {r['user_id']} إذن {r['perm']} (تفاصيله في شاشة nb الأصلية)")
        else:
            lines.append(f"طلب حذف #{r['id']} مستخدم {r['user_id']} نوع {r['target_type']} (تفاصيله في شاشة nb الأصلية)")
    if not page_rows:
        lines.append("لا شيء للمراجعة 👍")
    if pages > 1:
        lines.append(f"صفحة {page + 1} من {pages}")
    rows = btns + ([pager(page, pages, f"{PREFIX}rev")] if pages > 1 else []) + [nav(f"{PREFIX}hub")]
    ctx.show(chat, mid, "\n".join(lines), kb(*rows))
    return True


def _screen_imp(ctx, user, chat, mid, parts):
    db = ctx.db
    if len(parts) < 2:
        page = _page(parts, 1) if len(parts) > 1 else 0
        rows_m = _rows(db, "SELECT id, record_a, record_b, state, confidence FROM entity_matches "
                           "WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW','CONFLICT') "
                           "ORDER BY confidence DESC")
        page_rows, pages, page = page_slice(rows_m, 0, PAGE_SIZE)
        lines = [_banner(db) + "🧪 معاينة أثر المطابقة — اختر مطابقة لمعاينة أثرها قبل الموافقة",
                 "الموافقة نفسها تبقى على زر er:c في شاشة المراجعة الأصلية؛ هذه الشاشة لا تدمج أبدًا.",
                 LINE]
        btns = []
        for m in page_rows:
            lines.append(f"#{m['id']} {m['state']} ثقة {m['confidence']:.2f} سجلان {m['record_a']}↔{m['record_b']}")
            btns.append([(f"معاينة #{m['id']}", f"{PREFIX}imp:{m['id']}")])
        if not page_rows:
            lines.append("لا مطابقات معلّقة للمعاينة.")
        rows = btns + [nav(f"{PREFIX}hub")]
        ctx.show(chat, mid, "\n".join(lines), kb(*rows))
        return True
    try:
        mid_match = int(parts[1])
    except ValueError:
        ctx.show(chat, mid, "⚠️ معرّف مطابقة غير صالح.", kb(nav(f"{PREFIX}hub")))
        return True
    m = _one(db, "SELECT id, record_a, record_b, state, confidence FROM entity_matches WHERE id=?", (mid_match,))
    if not m:
        ctx.show(chat, mid, "⚠️ المطابقة غير موجودة.", kb(nav(f"{PREFIX}hub")))
        return True

    def side_counts(rid):
        ent = _one(db, "SELECT entity_id FROM entity_records WHERE record_id=? AND active=1", (rid,))
        if ent:
            recs = [r["record_id"] for r in _rows(db, "SELECT record_id FROM entity_records WHERE entity_id=? AND active=1", (ent["entity_id"],))]
            files = {r["file_id"] for r in _rows(db, f"SELECT file_id FROM records WHERE id IN ({','.join('?' * len(recs))})", tuple(recs))} if recs else set()
            return recs, files, ent["entity_id"]
        rec = _one(db, "SELECT file_id FROM records WHERE id=?", (rid,))
        return [rid], ({rec["file_id"]} if rec else set()), None

    recs_a, files_a, ent_a = side_counts(m["record_a"])
    recs_b, files_b, ent_b = side_counts(m["record_b"])
    merged_recs = set(recs_a) | set(recs_b)
    merged_files = files_a | files_b
    text = (_banner(db) + f"🧪 معاينة أثر المطابقة #{m['id']}\n"
            f"الحالة: {m['state']} — الثقة: {m['confidence']:.2f}\n" + LINE + "\n"
            f"الطرف A: كيان {ent_a if ent_a is not None else '— (سجل منفرد)'} — {fmt_int(len(recs_a))} سجل\n"
            f"الطرف B: كيان {ent_b if ent_b is not None else '— (سجل منفرد)'} — {fmt_int(len(recs_b))} سجل\n"
            f"النتيجة المتوقعة عند الدمج: كيان واحد يحوي {fmt_int(len(merged_recs))} سجلًا من {fmt_int(len(merged_files))} ملفًا (أعداد فقط، بلا قيم).\n"
            "⚠️ معاينة قراءة فقط: لا يتم أي دمج من هذه الشاشة.\n"
            "الموافقة تبقى على زر er:c في شاشة المراجعة الأصلية (er:m) — اضغط زر المراجعة أدناه للانتقال إليها.\n"
            "للإلغاء: ارجع إلى القائمة أو الرئيسية دون أي إجراء.")
    ctx.show(chat, mid, text, kb([("🔍 شاشة المراجعة الأصلية", f"er:m:{m['id']}:rv0")],
                                  [("◀️ قائمة المعاينة", f"{PREFIX}imp")], nav(f"{PREFIX}hub")))
    return True


def _screen_und(ctx, user, chat, mid, parts):
    db = ctx.db
    if len(parts) >= 2 and parts[1] == "page":
        pass  # fall through to the list branch below
    elif len(parts) >= 2 and parts[1] not in ("yes", "no"):
        try:
            hid = int(parts[1])
        except ValueError:
            ctx.show(chat, mid, "⚠️ معرّف سجل غير صالح.", kb(nav(f"{PREFIX}hub")))
            return True
        if len(parts) >= 3 and parts[2] == "yes":
            row = _one(db, "SELECT * FROM entity_merge_history WHERE id=?", (hid,))
            if not row:
                ctx.show(chat, mid, "⚠️ سجل القرار غير موجود.", kb(nav(f"{PREFIX}hub")))
                return True
            prior = _one(db, "SELECT id FROM nb_settings_log WHERE setting='match_undo_reversal' AND old_value=?", (str(hid),))
            if prior:
                ctx.show(chat, mid, "ℹ️ سبق تسجيل إبطال لهذا القرار — لم يُتخذ إجراء جديد ولم يُحذف أي تاريخ.",
                         kb(nav(f"{PREFIX}hub")))
                return True
            db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?,?,?,?)",
                       (user.telegram_id, "match_undo_reversal", str(hid), "reversal-record-only"))
            _commit(db)
            audit_nb(db, user.telegram_id, "nb_p5_undo", "match_history", hid,
                     {"history_id": hid, "action": row["action"]})
            _commit(db)
            text = (_banner(db) + f"↩️ تم تسجيل التراجع عن القرار (سجل تاريخ #{hid})\n"
                    "⚠️ تنبيه صريح: هذا التراجع سجل إبطال + تدقيق فقط.\n"
                    "لم يُحذف أي سجل من entity_merge_history، ولم يُنفذ فصل فيزيائي للكيانات من هذه الشاشة.\n"
                    "الفصل الفيزيائي — إن لزم — يتم عبر مسار unmerge الأصلي (er) كخطوة مشغل منفصلة، "
                    "لأن مخطط match_decisions لا يحمل قيمة «reversed» (يسمح فقط never_match/confirmed) "
                    "ولا يوجد عمود حالة إبطال في entity_matches.")
            ctx.show(chat, mid, text, kb(nav(f"{PREFIX}hub")))
            return True
        if len(parts) >= 3 and parts[2] == "no":
            ctx.show(chat, mid, "أُلغي — لم يُتخذ أي إجراء ولم يُسجل أي إبطال.", kb(nav(f"{PREFIX}hub")))
            return True
        row = _one(db, "SELECT * FROM entity_merge_history WHERE id=?", (hid,))
        if not row:
            ctx.show(chat, mid, "⚠️ سجل القرار غير موجود.", kb(nav(f"{PREFIX}hub")))
            return True
        import json
        try:
            detail = json.loads(row["detail"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        match_id = detail.get("match_id", "—")
        text = (_banner(db) + f"↩️ التراجع عن قرار سابق — سجل تاريخ #{hid}\n"
                f"القرار: {row['action']}\n"
                f"معرّف المطابقة المرتبط: {match_id}\n"
                f"الوقت: {ago(row['ts'])}\n"
                f"بواسطة: {row['actor'] or '—'}\n" + LINE + "\n"
                "ماذا سيفعل التأكيد؟ سجل إبطال في nb_settings_log + إدخال تدقيق فقط.\n"
                "⚠️ لن يُحذف التاريخ ولن يُفصل أي كيان فيزيائيًا من هنا (انظر تنبيه شاشة التأكيد).")
        ctx.show(chat, mid, text, confirm_markup(f"{PREFIX}und:{hid}:yes", f"{PREFIX}und:{hid}:no"))
        return True
    page = _page(parts, 2) if len(parts) >= 2 and parts[1] == "page" else 0
    rows_h = _rows(db, "SELECT * FROM entity_merge_history ORDER BY id DESC")
    page_rows, pages, page = page_slice(rows_h, page, PAGE_SIZE)
    lines = [_banner(db) + "↩️ التراجع عن قرار سابق — قرارات سابقة (من entity_merge_history)",
             "اختر قرارًا لعرضه ثم التراجع عنه (سجل إبطال + تدقيق فقط — بلا حذف تاريخ وبلا فصل فيزيائي).",
             LINE]
    btns = []
    for r in page_rows:
        lines.append(f"#{r['id']} {r['action']} — {ago(r['ts'])} — بواسطة {r['actor'] or '—'}")
        btns.append([(f"تراجع عن #{r['id']}", f"{PREFIX}und:{r['id']}")])
    if not page_rows:
        lines.append("لا قرارات سابقة مسجلة.")
    if pages > 1:
        lines.append(f"صفحة {page + 1} من {pages}")
    rows = btns + ([pager(page, pages, f"{PREFIX}und:page")] if pages > 1 else []) + [nav(f"{PREFIX}hub")]
    ctx.show(chat, mid, "\n".join(lines), kb(*rows))
    return True


def _screen_rul(ctx, user, chat, mid, parts):
    try:
        from ..er import matcher
    except Exception:
        ctx.show(chat, mid, "⚠️ محرك المطابقة غير متاح في هذا السياق — لم يُجرَ أي اختبار.",
                 kb(nav(f"{PREFIX}hub")))
        return True

    def ent(name, mother, dob=None, sex=None, phone=None, nid=None, pid=None):
        return {"name_norm": name, "name_core": name, "mother_name": mother,
                "dob": dob, "sex": sex, "phone": phone, "national_id": nid, "patient_id": pid}
    pairs = [
        ("اسم + أم مطابقان", ent("احمد محمد علي", "فاطمة حسن علي", "1990-05-10", "M"),
         ent("احمد محمد علي", "فاطمة حسن علي", "1990-05-10", "M"), {"EXACT_MATCH", "HIGH_CONFIDENCE_MATCH"}, "merge"),
        ("اسم مطابق + أم مختلفة تمامًا", ent("احمد محمد علي", "فاطمة حسن علي", "1990-05-10", "M"),
         ent("احمد محمد علي", "مريم خالد سعيد", "1990-05-10", "M"), {"CONFLICT", "NO_MATCH"}, "nomerge"),
        ("اسم بخطأ إملائي + نفس الميلاد والهاتف", ent("احمد محمد علي", None, "1990-05-10", "M", "07700000001"),
         ent("احمد محمود علي", None, "1990-05-10", "M", "07700000001"), {"PROBABLE_MATCH", "NEEDS_REVIEW", "POSSIBLE_MATCH"}, "review"),
        ("هويتان وطنيتان متعارضتان", ent("احمد محمد علي", None, "1990-05-10", "M", None, "111"),
         ent("احمد محمد علي", None, "1990-05-10", "M", None, "222"), {"CONFLICT", "NO_MATCH"}, "nomerge"),
    ]
    auto = {"EXACT_MATCH", "HIGH_CONFIDENCE_MATCH"}
    lines = [_banner(ctx.db) + "🧩 اختبار قواعد المطابقة — بيانات اصطناعية فقط",
             "⚠️ هذه الأزواج مختلقة داخل الذاكرة؛ لم تُقرأ أي سجلات إنتاج ولم يُحسب أي معدل دقة إنتاج هنا.",
             LINE]
    correct = false_merges = misses = 0
    for name, a, b, expected, kind in pairs:
        res = matcher.compare(a, b)
        ok = res.state in expected
        correct += 1 if ok else 0
        if kind == "nomerge" and res.state in auto:
            false_merges += 1
        if kind == "merge" and res.state not in auto:
            misses += 1
        verdict = "مطابقة" if res.state in auto else ("مراجعة" if res.state in ("PROBABLE_MATCH", "POSSIBLE_MATCH", "NEEDS_REVIEW") else "لا مطابقة")
        lines.append(f"• {name}: {verdict} — حالة {res.state} — ثقة {res.confidence:.2f} — قاعدة {res.rule} — {'صحيح' if ok else 'خارج المتوقع'}")
    lines += [LINE,
              f"ملخص اصطناعي: صحيح {correct} من {len(pairs)} — دمج خاطئ (اصطناعي): {false_merges} — فائت (اصطناعي): {misses}",
              "هذا الملخص يصف المجموعة الاصطناعية الأربع أعلاه فقط، وليس دقة الإنتاج أبدًا."]
    ctx.show(chat, mid, "\n".join(lines), kb(nav(f"{PREFIX}hub")))
    return True


def _screen_cnf(ctx, user, chat, mid, parts):
    db = ctx.db
    page = _page(parts, 1)
    rows_m = _rows(db, "SELECT id, record_a, record_b, state, confidence, conflicts FROM entity_matches "
                        "WHERE state='CONFLICT' AND (decision IS NULL OR decision='conflict') ORDER BY id")
    page_rows, pages, page = page_slice(rows_m, page, PAGE_SIZE)
    lines = [_banner(db) + "⚠️ التعارضات غير المحسومة",
             "تُترك هذه التعارضات للمراجع؛ لا يوجد زر دمج قسري في هذه الشاشة أبدًا.",
             "تُعرض أسماء الحقول المتعارضة فقط — بلا قيم.",
             LINE]
    btns = []
    for m in page_rows:
        fields = _conflict_fields(m.get("conflicts"))
        lines.append(f"#{m['id']} سجلان {m['record_a']}↔{m['record_b']} ثقة {m['confidence']:.2f} — حقول متعارضة: {', '.join(fields) if fields else '—'}")
        btns.append([(f"مراجعة #{m['id']}", f"er:m:{m['id']}:rv0")])
    if not page_rows:
        lines.append("لا تعارضات غير محسومة 👍")
    if pages > 1:
        lines.append(f"صفحة {page + 1} من {pages}")
    rows = btns + ([pager(page, pages, f"{PREFIX}cnf")] if pages > 1 else []) + [nav(f"{PREFIX}hub")]
    ctx.show(chat, mid, "\n".join(lines), kb(*rows))
    return True


def _screen_chk(ctx, user, chat, mid, parts):
    db = ctx.db
    s = _check_summary(db)
    ai = _ai_note(ctx)
    summary = (f"quick={s['quick']}; stuck={s['stuck']}; jobs={s['jobs_total']}; "
               f"failed_jobs={s['jobs_failed']}; failed_files={s['files_failed']}; "
               f"pending_reports={s['reports_pending']}")
    db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?,?,?,?)",
               (user.telegram_id, "full_check", "", summary))
    _commit(db)
    text = (_banner(db) + "🩺 فحص شامل\n" + LINE + "\n"
            f"فحص سريع لقاعدة البيانات (PRAGMA quick_check): {s['quick']}\n"
            f"FTS: {s['fts']}\n"
            f"مهام عالقة (جارية أقدم من {STUCK_MINUTES} دقيقة): {fmt_int(s['stuck'])}\n"
            f"مهام: إجمالي {fmt_int(s['jobs_total'])} — جارية {fmt_int(s['jobs_running'])} — منتظرة {fmt_int(s['jobs_queued'])} — فاشلة {fmt_int(s['jobs_failed'])}\n"
            f"ملفات فاشلة/محجوزة: {fmt_int(s['files_failed'])}\n"
            f"تقارير معلّقة: {fmt_int(s['reports_pending'])}\n"
            f"خدمة الذكاء الاصطناعي: {ai}\n"
            "تم تسجيل هذا الفحص في nb_settings_log (full_check) ليُعرض عند إنهاء الصيانة.")
    ctx.show(chat, mid, text, kb(nav(f"{PREFIX}hub")))
    return True


def _screen_spd(ctx, user, chat, mid, parts):
    db = ctx.db
    t0 = time.perf_counter()
    n_files = _count(db, "SELECT COUNT(*) FROM files")
    n_records = _count(db, "SELECT COUNT(*) FROM records")
    t_count = (time.perf_counter() - t0) * 1000
    # FTS on synthetic data in a separate in-memory DB (never production rows).
    t1 = time.perf_counter()
    fts_n = None
    try:
        mem = sqlite3.connect(":memory:")
        mem.execute("CREATE VIRTUAL TABLE record_fts USING fts5(body, tokenize='unicode61 remove_diacritics 0')")
        for i in range(20):
            mem.execute("INSERT INTO record_fts(rowid, body) VALUES (?,?)", (i + 1, f"synthetic test token {i}"))
        fts_n = mem.execute("SELECT COUNT(*) FROM record_fts WHERE record_fts MATCH 'synthetic'").fetchone()[0]
        mem.close()
    except sqlite3.Error:
        fts_n = None
    t_fts = (time.perf_counter() - t1) * 1000
    t2 = time.perf_counter()
    real_n = None
    if "record_fts" in _tables(db):
        try:
            real_n = db.execute("SELECT COUNT(*) FROM record_fts WHERE record_fts MATCH 'zzsyntheticzz'").fetchone()[0]
        except sqlite3.Error:
            real_n = None
    t_real = (time.perf_counter() - t2) * 1000
    maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    text = (_banner(db) + "⚡ اختبار السرعة — أرقام مقاسة فعلًا (ليست تقديرات)\n" + LINE + "\n"
            f"استعلام عدّ مفهرس (COUNT ملفات + سجلات): {t_count:.2f} ms — ملفات {fmt_int(n_files)} — سجلات {fmt_int(n_records)}\n"
            f"استعلام FTS على بيانات اصطناعية (قاعدة ذاكرة منفصلة، 20 صفًا): {t_fts:.2f} ms — نتائج {fts_n if fts_n is not None else 'غير متاح'}\n"
            f"استعلام FTS على قاعدة البيانات الحالية (مصطلح اصطناعي لا يطابق شيئًا): {t_real:.2f} ms — نتائج {real_n if real_n is not None else 'غير متاح (لا جدول FTS)'}\n"
            "زمن فهرسة ملف تجريبي عبر Indexer: غير مقاس هنا (يتطلب مسار إدخال كاملًا بآثار جانبية)؛ "
            "المقاس بدلًا منه هو العدّ + البحث أعلاه، ومسمّى كذلك صراحة.\n"
            f"الذاكرة (أقصى استخدام للعملية، ru_maxrss): {maxrss_kb / 1024:.2f} MB ({fmt_int(maxrss_kb)} KB)")
    ctx.show(chat, mid, text, kb(nav(f"{PREFIX}hub")))
    return True


def _screen_dash(ctx, user, chat, mid, parts):
    db = ctx.db
    statuses = {r["status"]: r["c"] for r in _rows(db, "SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    total = sum(statuses.values())
    failed = statuses.get("failed", 0)
    rate = (failed / total * 100) if total else 0.0
    durations = []
    for j in _rows(db, "SELECT started_at, finished_at FROM jobs WHERE started_at IS NOT NULL AND finished_at IS NOT NULL AND status IN ('done','failed')"):
        a, b = _parse_ts(j["started_at"]), _parse_ts(j["finished_at"])
        if a and b and b >= a:
            durations.append((b - a).total_seconds())
    avg_line = (f"متوسط زمن المعالجة: {sum(durations) / len(durations):.2f} ثانية (من {len(durations)} مهمة مكتملة بطوابع زمنية)"
                if durations else "زمن المعالجة: لا توجد مهام مكتملة بطوابع زمنية لحساب المتوسط (started_at/finished_at مخزّنة بالمخطط لكن بلا صفوف مكتملة هنا)")
    files_size = _count(db, "SELECT COALESCE(SUM(size_bytes),0) FROM files")
    files_n = _count(db, "SELECT COUNT(*) FROM files")
    db_size_line = "حجم ملف قاعدة البيانات: غير متاح (لا مسار عبر ctx.cfg — أعداد فقط)"
    cfg = getattr(ctx, "cfg", None)
    if cfg is not None:
        try:
            import os
            p = cfg.db_path
            if p and os.path.exists(p):
                db_size_line = f"حجم ملف قاعدة البيانات: {fmt_size(os.path.getsize(p))}"
        except Exception:
            pass
    lines = [_banner(db) + "📈 لوحة الأداء — نافذة القياس: جميع المهام المسجلة",
             f"مهام جارية: {fmt_int(statuses.get('running', 0))} — منتظرة: {fmt_int(statuses.get('queued', 0))} — متوقفة مؤقتًا: {fmt_int(statuses.get('paused', 0))}",
             f"معدل الفشل: {rate:.2f}% (فاشلة {fmt_int(failed)} من إجمالي {fmt_int(total)})",
             avg_line,
             f"التخزين: {fmt_int(files_n)} ملفًا — إجمالي أحجام الملفات {fmt_size(files_size)}",
             db_size_line,
             "أعداد فقط — بلا قيم سجلات."]
    ctx.show(chat, mid, "\n".join(lines), kb(nav(f"{PREFIX}hub")))
    return True


def _screen_wrk(ctx, user, chat, mid, parts):
    db = ctx.db
    page = _page(parts, 1)
    rows_j = _rows(db, "SELECT id, kind, status, progress, created_by, started_at, file_id FROM jobs "
                        "WHERE status IN ('running','paused') ORDER BY id")
    page_rows, pages, page = page_slice(rows_j, page, PAGE_SIZE)
    lines = [_banner(db) + "🧵 تفاصيل العمال — المهام الجارية فعلًا",
             "خيوط/عمليات العامل غير مرئية من قاعدة البيانات؛ المعروض أدناه هو المهام الجارية فقط — لا أرقام PID مختلقة.",
             LINE]
    for j in page_rows:
        lines.append(f"JOB-{j['id']:06d} {j['kind']} {j['status']} تقدم {j['progress']:.0f}% — أنشأها {j['created_by']} — بدأت {ago(j['started_at'])}")
    if not page_rows:
        lines.append("لا مهام جارية أو متوقفة مؤقتًا حاليًا.")
    if pages > 1:
        lines.append(f"صفحة {page + 1} من {pages}")
    rows = ([pager(page, pages, f"{PREFIX}wrk")] if pages > 1 else []) + [nav(f"{PREFIX}hub")]
    ctx.show(chat, mid, "\n".join(lines), kb(*rows))
    return True


def _screen_err(ctx, user, chat, mid, parts):
    db = ctx.db
    job, pair = _last_error(db)
    if not job:
        ctx.show(chat, mid, _banner(db) + "🧯 تشخيص آخر خطأ\nلا توجد مهام فاشلة مسجلة.", kb(nav(f"{PREFIX}hub")))
        return True
    cause, fix = pair
    text = (_banner(db) + "🧯 تشخيص آخر خطأ\n" + LINE + "\n"
            f"رقم الخطأ (معرّف المهمة): JOB-{job['id']:06d}\n"
            f"نوع المهمة: {job['kind']} — الحالة: {job['status']}\n"
            f"السبب المختصر (مقتصّ إلى 200 حرف ومطهّر من الأسرار): {cause}\n"
            f"الإصلاح المقترح: {fix}\n"
            "لا أسرار ولا قيم سجلات في هذا التشخيص.")
    ctx.show(chat, mid, text, kb(nav(f"{PREFIX}hub")))
    return True


def _screen_clp(ctx, user, chat, mid, parts):
    db = ctx.db
    p = _cleanup_preview(db)
    total_size = p["temp_size"] + p["rep_size"]
    text = (_banner(db) + "🧹 معاينة التنظيف — معاينة فقط، لا يُحذف شيء من هنا\n" + LINE + "\n"
            "القاعدة المعلنة:\n"
            f"• ملفات مؤقتة: حالة failed أو quarantined وأقدم من {p['temp_days']} يومًا (nb_retention temp)\n"
            f"• تقارير قديمة: أقدم من {p['rep_days']} يومًا (nb_retention reports)\n"
            "• حالات أزرار قديمة (callback_state): أقدم من 7 أيام\n"
            f"ملفات مؤقتة: {fmt_int(len(p['temp']))} صفًا — {fmt_size(p['temp_size'])}\n"
            f"تقارير قديمة: {fmt_int(len(p['reports']))} صفًا — {fmt_size(p['rep_size'])}\n"
            f"حالات أزرار قديمة: {fmt_int(len(p['callbacks']))} صفًا\n"
            f"الحجم الإجمالي القابل للتحرير (صفوف قاعدة بيانات): {fmt_size(total_size)}\n"
            "⚠️ التنظيف الفعلي يحذف صفوف قاعدة بيانات فقط؛ ملفات القرص الأصلية لا تُمس أبدًا، "
            "والملفات بحالة indexed/partial لا تُمس أبدًا.")
    ctx.show(chat, mid, text, kb([("♻️ تنفيذ التنظيف الآمن", f"{PREFIX}cln")], nav(f"{PREFIX}hub")))
    return True


def _screen_cln(ctx, user, chat, mid, parts):
    db = ctx.db
    if len(parts) >= 2 and parts[1] == "yes":
        p = _cleanup_preview(db)
        tables = _tables(db)
        skipped = []
        n_temp = n_rep = n_cb = 0
        if "callback_state" in tables:
            cur = db.execute("DELETE FROM callback_state WHERE id IN (%s)" % ",".join("?" * len(p["callbacks"])) if p["callbacks"] else "SELECT 0 WHERE 0",
                             tuple(r["id"] for r in p["callbacks"]))
            n_cb = cur.rowcount if p["callbacks"] else 0
        else:
            skipped.append("callback_state (الجدول غير موجود)")
        if "reports" in tables:
            cur = db.execute("DELETE FROM reports WHERE id IN (%s)" % ",".join("?" * len(p["reports"])) if p["reports"] else "SELECT 0 WHERE 0",
                             tuple(r["id"] for r in p["reports"]))
            n_rep = cur.rowcount if p["reports"] else 0
        else:
            skipped.append("reports (الجدول غير موجود)")
        if "files" in tables:
            cur = db.execute("DELETE FROM files WHERE id IN (%s)" % ",".join("?" * len(p["temp"])) if p["temp"] else "SELECT 0 WHERE 0",
                             tuple(r["id"] for r in p["temp"]))
            n_temp = cur.rowcount if p["temp"] else 0
        else:
            skipped.append("files (الجدول غير موجود)")
        _commit(db)
        audit_nb(db, user.telegram_id, "nb_p5_cleanup", "cleanup", None,
                 {"temp_files": n_temp, "reports": n_rep, "callbacks": n_cb})
        _commit(db)
        text = (_banner(db) + "♻️ اكتمل التنظيف الآمن\n"
                f"ملفات مؤقتة محذوفة (صفوف): {fmt_int(n_temp)}\n"
                f"تقارير قديمة محذوفة (صفوف): {fmt_int(n_rep)}\n"
                f"حالات أزرار قديمة محذوفة (صفوف): {fmt_int(n_cb)}\n"
                "ملفات القرص الأصلية لم تُمس أبدًا (حذف صفوف قاعدة بيانات فقط).\n"
                "الملفات بحالة indexed/partial لم تُمس.\n"
                + (f"فئات متخطاة: {', '.join(skipped)}" if skipped else "لا فئات متخطاة."))
        ctx.show(chat, mid, text, kb(nav(f"{PREFIX}hub")))
        return True
    if len(parts) >= 2 and parts[1] == "no":
        ctx.show(chat, mid, "أُلغي — لم يُحذف شيء.", kb(nav(f"{PREFIX}hub")))
        return True
    p = _cleanup_preview(db)
    text = (_banner(db) + "♻️ تنظيف آمن — تأكيد\n"
            f"سيُحذف فقط ما عُرض في معاينة التنظيف: {fmt_int(len(p['temp']))} ملفًا مؤقتًا، "
            f"{fmt_int(len(p['reports']))} تقريرًا قديمًا، {fmt_int(len(p['callbacks']))} حالة زر قديمة (صفوف قاعدة بيانات فقط).\n"
            "لن تُحذف ملفات القرص الأصلية أبدًا، ولن تُمس الملفات بحالة indexed/partial.")
    ctx.show(chat, mid, text, confirm_markup(f"{PREFIX}cln:yes", f"{PREFIX}cln:no"))
    return True


def _screen_dbc(ctx, user, chat, mid, parts):
    db = ctx.db
    try:
        integ = [str(r[0]) for r in db.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.Error:
        integ = ["خطأ في الفحص"]
    try:
        quick = [str(r[0]) for r in db.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.Error:
        quick = ["خطأ في الفحص"]
    idxs = _rows(db, "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name")
    lines = [_banner(db) + "🗃 فحص قاعدة البيانات — قراءة فقط، لا إصلاح هنا",
             f"integrity_check: {integ[0] if len(integ) == 1 else fmt_int(len(integ)) + ' سطرًا (أولها: ' + (integ[0] if integ else '—') + ')'}",
             f"quick_check: {quick[0] if len(quick) == 1 else fmt_int(len(quick)) + ' سطرًا'}",
             f"الفهارس ({fmt_int(len(idxs))}): " + (", ".join(f"{i['name']}({i['tbl_name']})" for i in idxs[:30]) if idxs else "—"),
             "⚠️ تحذير قبل أي إصلاح: هذه الشاشة لا تنفذ أي إصلاح ولا تعدّل أي بيانات. "
             "أي إصلاح هو خطوة مشغل صريحة منفصلة، خارج هذه الشاشة، وبعد نسخة احتياطية موثقة."]
    ctx.show(chat, mid, "\n".join(lines), kb(nav(f"{PREFIX}hub")))
    return True


def _screen_feat(ctx, user, chat, mid, parts):
    db = ctx.db
    header_files = _count(db, "SELECT COUNT(*) FROM files")
    header_jobs = _count(db, "SELECT COUNT(*) FROM jobs")
    checks = []
    try:
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        from .nb_store import TABLES, ensure_nb_tables as _ensure
        _ensure(mem)
        have = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks.append(("وجود جداول nb_store", all(t in have for t in TABLES), ""))
        mem.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, ts TEXT DEFAULT '', user_id INTEGER, action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT)")
        from .. import audit as _audit
        _audit.log(mem, user.telegram_id, "nb_p5_feat_test", "test", None, {"check": "feat"})
        n = mem.execute("SELECT COUNT(*) FROM audit_log WHERE action='nb_p5_feat_test'").fetchone()[0]
        checks.append(("كتابة التدقيق تعمل", n == 1, ""))
        from ..auth import User as _User
        admin_u = _User(1, "admin")
        plain_u = _User(2, "user", perms=frozenset())
        ok_req = False
        try:
            require(admin_u, "manage")
            try:
                require(plain_u, "manage")
                ok_req = False
            except PermissionDenied:
                ok_req = True
        except PermissionDenied:
            ok_req = False
        checks.append(("فحص الإذن require يعمل", ok_req, ""))
        sliced, _pages, clamped = page_slice(list(range(25)), 99, 10)
        checks.append(("تقييد الصفحات (pagination clamps)", clamped == 2 and len(sliced) == 5, ""))
        mem.close()
    except Exception as exc:  # noqa: BLE001 — reported as failed checks, never raised
        checks.append(("مجموعة الاختبارات", False, str(exc)[:100]))
    lines = [_banner(db) + "🧰 اختبار الميزات — على نسخة اختبار في الذاكرة فقط",
             "⚠️ الاختبارات جرت على قاعدة :memory: منفصلة؛ قاعدة البيانات الحقيقية لم تُمس بها "
             f"(رأس الشاشة فقط يقرأ أعدادها: ملفات {fmt_int(header_files)} — مهام {fmt_int(header_jobs)}).",
             LINE]
    for name, ok, _d in checks:
        lines.append(f"{'✅' if ok else '❌'} {name}: {'نجح' if ok else 'فشل'}")
    ctx.show(chat, mid, "\n".join(lines), kb(nav(f"{PREFIX}hub")))
    return True


def _screen_mnt(ctx, user, chat, mid, parts):
    db = ctx.db
    if len(parts) >= 2 and parts[1] == "yes":
        old = "active" if maintenance_active(db) else "inactive"
        db.execute("INSERT OR REPLACE INTO nb_maintenance(id, active, by_user, reason) VALUES (1, 1, ?, ?)",
                   (user.telegram_id, "صيانة يدوية من لوحة المرحلة 5"))
        db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?,?,?,?)",
                   (user.telegram_id, "maintenance", old, "active"))
        _commit(db)
        audit_nb(db, user.telegram_id, "nb_p5_maintenance_on", "maintenance", 1, {"state": "active"})
        _commit(db)
        ctx.show(chat, mid, "🛑 وضع الصيانة نشط الآن — سيظهر التنبيه أعلى كل شاشات لوحة المرحلة 5.",
                 kb(nav(f"{PREFIX}hub")))
        return True
    if len(parts) >= 2 and parts[1] == "no":
        ctx.show(chat, mid, "أُلغي — لم يتغير وضع الصيانة.", kb(nav(f"{PREFIX}hub")))
        return True
    state = "نشط" if maintenance_active(db) else "غير نشط"
    ctx.show(chat, mid, _banner(db) + f"🛑 وضع الصيانة — الحالة الحالية: {state}\nتأكيد تفعيل وضع الصيانة؟ سيظهر التنبيه أعلى كل شاشات المرحلة 5.",
             confirm_markup(f"{PREFIX}mnt:yes", f"{PREFIX}mnt:no"))
    return True


def _screen_end(ctx, user, chat, mid, parts):
    db = ctx.db
    last = _one(db, "SELECT * FROM nb_settings_log WHERE setting='full_check' ORDER BY id DESC LIMIT 1")
    if len(parts) >= 2 and parts[1] == "yes":
        if not last:
            ctx.show(chat, mid, "⛔ مرفوض: لا يوجد فحص شامل مسجل (nb_settings_log full_check). "
                     "شغّل «فحص شامل» أولًا ثم أكّد الإنهاء — لم يتغير وضع الصيانة.",
                     kb(nav(f"{PREFIX}hub")))
            return True
        old = "active" if maintenance_active(db) else "inactive"
        db.execute("INSERT OR REPLACE INTO nb_maintenance(id, active, by_user, reason) VALUES (1, 0, NULL, '')")
        db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?,?,?,?)",
                   (user.telegram_id, "maintenance", old, "inactive"))
        _commit(db)
        audit_nb(db, user.telegram_id, "nb_p5_maintenance_off", "maintenance", 1, {"state": "inactive"})
        _commit(db)
        ctx.show(chat, mid, "▶️ انتهت الصيانة — وضع الصيانة غير نشط الآن.", kb(nav(f"{PREFIX}hub")))
        return True
    if len(parts) >= 2 and parts[1] == "no":
        ctx.show(chat, mid, "أُلغي — بقي وضع الصيانة كما هو.", kb(nav(f"{PREFIX}hub")))
        return True
    rule = ("قاعدة الإنهاء: يُعرض آخر فحص شامل مسجل (nb_settings_log full_check) ويُطلب التأكيد؛ "
            "والإنهاء مرفوض برمجيًا إن لم يوجد فحص مسجل — شغّل «فحص شامل» أولًا في تلك الحالة.")
    summary = last["new_value"] if last else "لا يوجد فحص شامل مسجل بعد."
    state = "نشط" if maintenance_active(db) else "غير نشط"
    ctx.show(chat, mid, _banner(db) + f"▶️ إنهاء الصيانة — الحالة الحالية: {state}\n{rule}\nآخر فحص شامل مسجل: {summary}",
             confirm_markup(f"{PREFIX}end:yes", f"{PREFIX}end:no"))
    return True


def _screen_rep(ctx, user, chat, mid, parts):
    db = ctx.db
    s = _check_summary(db)
    job, pair = _last_error(db)
    files_n = _count(db, "SELECT COUNT(*) FROM files")
    ai = _ai_note(ctx)
    audit_nb(db, user.telegram_id, "nb_p5_health_report", "health_report", None,
             {"jobs": s["jobs_total"], "failed_jobs": s["jobs_failed"], "files": files_n})
    _commit(db)
    lines = [_banner(db) + "📄 تقرير صحة النظام", LINE,
             "— التشخيص —",
             f"quick_check: {s['quick']} — FTS: {s['fts']}",
             f"مهام عالقة (أقدم من {STUCK_MINUTES} دقيقة): {fmt_int(s['stuck'])}",
             f"خدمة الذكاء الاصطناعي: {ai}",
             "— آخر خطأ —"]
    if job:
        cause, fix = pair
        lines += [f"JOB-{job['id']:06d} {job['kind']}: {cause}", f"الإصلاح المقترح: {fix}"]
    else:
        lines.append("لا مهام فاشلة مسجلة.")
    lines += ["— الإحصاءات —",
              f"ملفات: {fmt_int(files_n)} — مهام: إجمالي {fmt_int(s['jobs_total'])}، جارية {fmt_int(s['jobs_running'])}، "
              f"منتظرة {fmt_int(s['jobs_queued'])}، فاشلة {fmt_int(s['jobs_failed'])}",
              f"ملفات فاشلة/محجوزة: {fmt_int(s['files_failed'])} — تقارير معلّقة: {fmt_int(s['reports_pending'])}",
              "أعداد فقط — بلا قيم سجلات ولا أسرار. تم تسجيل توليد هذا التقرير في التدقيق."]
    ctx.show(chat, mid, "\n".join(lines), kb(nav(f"{PREFIX}hub")))
    return True


_SCREENS = {"hub": _screen_hub, "rev": _screen_rev, "imp": _screen_imp, "und": _screen_und,
            "rul": _screen_rul, "cnf": _screen_cnf, "chk": _screen_chk, "spd": _screen_spd,
            "dash": _screen_dash, "wrk": _screen_wrk, "err": _screen_err, "clp": _screen_clp,
            "cln": _screen_cln, "dbc": _screen_dbc, "feat": _screen_feat, "mnt": _screen_mnt,
            "end": _screen_end, "rep": _screen_rep}


def handle(ctx, user, chat, mid, parts) -> bool:
    if not parts or parts[0] not in _KEYS:
        return False
    key = parts[0]
    try:
        ensure_nb_tables(ctx.db)
    except sqlite3.Error:
        pass
    try:
        require(user, "manage")
    except PermissionDenied:
        return _deny(ctx, user, chat, mid, "manage")
    try:
        return _SCREENS[key](ctx, user, chat, mid, parts)
    except PermissionDenied:
        return _deny(ctx, user, chat, mid, "manage")
    except Exception as exc:  # noqa: BLE001 — every handled path must show exactly once
        ctx.show(chat, mid, f"⚠️ خطأ في الشاشة: {_scrub(str(exc))[:120]}", kb(nav(f"{PREFIX}hub")))
        return True


def handle_text(ctx, user, chat, text) -> bool:
    return False
