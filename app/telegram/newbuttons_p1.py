"""Phase 1 (UX) new buttons — prefix nb:p1:.

Privacy: screens show counts, ids, statuses, file names, column names and
job numbers ONLY. record_values is never read here, search-result field
values are never shown, and record content is never requested or stored.

Honest limits (wired by the parent router later, NOT here):
- tpl "use" shows the stored conditions and records an audit entry; it
  does NOT execute a search (no search engine in this module).
- ntf toggles store the real preference in nb_notif_prefs; actual
  sending of notifications is the parent's notify wiring.
- set order/match/language values are stored per-user; the search engine
  honouring them is the parent's wiring. Page size reuses the existing
  settings key "pagesize:{uid}" that bot.page_size() already reads.
- perm export note: limits are enforced by the parent's limits work;
  this screen shows only the user's real export (report) count and says
  limits are not enforced here. No limit number is invented.
- Own uploads: files has no created_by column. Ownership is derived
  exactly as bot._creator does it: a file is the user's when an ingest
  job for that file was created by the user. Admins see any file.
- This phase submits NO jobs, so the no-duplicate-jobs rule is vacuous
  here (stated, not silently assumed).
"""
from __future__ import annotations

import json
import sqlite3

from ..auth import PERM_AR, PERMISSIONS
from ..errors import AppError, PermissionDenied
from .nb_store import (
    audit_nb,
    confirm_markup,
    ensure_nb_tables,
    page_slice,
    require,
    set_pending,
)
from .ui import LINE, ago, fmt_int, fmt_size, kb, nav, pager

PREFIX = "nb:p1:"

BUTTONS = [
    {"key": "ops", "label": "🕘 سجل عملياتي", "perm": "any", "confirm": False, "audit": None},
    {"key": "tpl", "label": "⭐ قوالب البحث المحفوظة", "perm": "search", "confirm": True, "audit": "nb_tpl_delete"},
    {"key": "set", "label": "🎛 إعدادات البحث", "perm": "any", "confirm": False, "audit": "nb_settings_change"},
    {"key": "fdet", "label": "📄 تفاصيل الملف ونتيجة معالجته", "perm": "sources", "confirm": False, "audit": None},
    {"key": "ntf", "label": "🔔 إشعاراتي", "perm": "any", "confirm": False, "audit": "nb_notif_change"},
    {"key": "perm", "label": "🔐 صلاحياتي واستخدامي", "perm": "any", "confirm": True, "audit": "nb_perm_request"},
    {"key": "help", "label": "🆘 المساعدة والإبلاغ عن مشكلة", "perm": "any", "confirm": False, "audit": "nb_issue_report"},
]

_KEYS = {b["key"]: b for b in BUTTONS}

# ---- settings -----------------------------------------------------------
PAGE_SIZES = (10, 25, 50, 100)
MATCHES = {"exact": "مطابقة تامة", "partial": "مطابقة جزئية", "fuzzy": "مطابقة تقريبية"}
ORDERS = {"newest": "الأحدث أولاً", "relevance": "الأكثر صلة أولاً"}
LANGS = {"ar": "العربية", "en": "English"}
_SET_DEFAULTS = {"page_size": "10", "match": "partial", "order": "newest", "lang": "ar"}


def _set_key(uid: int, name: str) -> str:
    if name == "page_size":
        return f"pagesize:{uid}"
    return f"nb_{name}:{uid}"


def _get_setting(conn, uid: int, name: str) -> str:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (_set_key(uid, name),)).fetchone()
    except sqlite3.Error:
        return _SET_DEFAULTS[name]
    if not row or row[0] is None:
        return _SET_DEFAULTS[name]
    val = str(row[0])
    if name == "page_size":
        return val if val in {str(n) for n in PAGE_SIZES} else _SET_DEFAULTS[name]
    table = {"match": MATCHES, "order": ORDERS, "lang": LANGS}[name]
    return val if val in table else _SET_DEFAULTS[name]


def _write_setting(conn, uid: int, name: str, value: str) -> str:
    old = _get_setting(conn, uid, name)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_set_key(uid, name), value),
    )
    conn.execute(
        "INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) VALUES (?,?,?,?)",
        (uid, name, old, value),
    )
    audit_nb(conn, uid, "nb_settings_change", "setting", name, {"old": old, "new": value})
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return old


# ---- small helpers ------------------------------------------------------
def _deny_show(ctx, user, chat, mid, what: str):
    ctx.deny(user.telegram_id, what)
    ctx.show(chat, mid, "⛔ غير مصرح لك بهذا الإجراء.", kb(nav()))


def _own_file_ids(conn, uid: int):
    rows = conn.execute(
        "SELECT DISTINCT file_id FROM jobs WHERE kind='ingest' AND created_by=? AND file_id IS NOT NULL",
        (uid,),
    ).fetchall()
    return {r[0] for r in rows}


def _can_see_file(conn, user, fid: int) -> bool:
    if user.is_admin:
        return True
    return fid in _own_file_ids(conn, user.telegram_id)


def _tpl_of(conn, user, tid: int):
    row = conn.execute("SELECT * FROM nb_templates WHERE id=?", (tid,)).fetchone()
    if row is None or (row["user_id"] != user.telegram_id and not user.is_admin):
        return None
    return row


def _cond_summary(conditions: str) -> str:
    try:
        data = json.loads(conditions or "{}")
    except ValueError:
        return (conditions or "")[:500]
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)[:500]
    return str(data)[:500]


def _page(parts, idx=1, default=0) -> int:
    try:
        return max(0, int(parts[idx]))
    except (ValueError, IndexError):
        return default


# ---- ops ----------------------------------------------------------------
def _ops(ctx, user, chat, mid, parts):
    require(user, "any")
    conn = ctx.db
    uid = user.telegram_id
    page = _page(parts, 1) if len(parts) > 1 else 0
    searches = conn.execute(
        "SELECT id, mode, ts, result_count FROM search_history WHERE user_id=? ORDER BY id DESC",
        (uid,),
    ).fetchall()
    s_page, s_pages, page = page_slice(searches, page, 10)
    own_ids = _own_file_ids(conn, uid)
    files = []
    if own_ids:
        q = ",".join("?" * len(own_ids))
        files = conn.execute(
            f"SELECT id, original_name, status, size_bytes FROM files WHERE id IN ({q}) ORDER BY id DESC LIMIT 5",
            tuple(sorted(own_ids)),
        ).fetchall()
    files_total = len(own_ids)
    jobs = conn.execute(
        "SELECT id, kind, status, progress FROM jobs WHERE created_by=? ORDER BY id DESC LIMIT 5",
        (uid,),
    ).fetchall()
    jobs_total = conn.execute("SELECT COUNT(*) FROM jobs WHERE created_by=?", (uid,)).fetchone()[0]
    lines = [f"🕘 سجل عملياتي — صفحة {page + 1} من {s_pages}", LINE,
             f"عمليات بحث: {fmt_int(len(searches))} • ملفاتي: {fmt_int(files_total)} • مهامي: {fmt_int(jobs_total)}",
             "", "🔎 آخر عمليات البحث (رقم البحث، النوع، عدد النتائج فقط):"]
    if s_page:
        for r in s_page:
            lines.append(f"• بحث #{r['id']} — {r['mode']} — {fmt_int(r['result_count'])} نتيجة — {ago(r['ts'])}")
    else:
        lines.append("لا توجد عمليات بحث بعد.")
    lines += ["", "📁 ملفاتي (من مهام الرفع الخاصة بك):"]
    if files:
        for f in files:
            lines.append(f"• ملف #{f['id']} {f['original_name']} — {f['status']} — {fmt_size(f['size_bytes'] or 0)}")
    else:
        lines.append("لا توجد ملفات مرفوعة من قبلك بعد.")
    lines += ["", "🧑‍💼 مهامي الأخيرة:"]
    if jobs:
        for j in jobs:
            lines.append(f"• JOB-{j['id']:06d} {j['kind']} — {j['status']} — {j['progress']:.0f}%")
    else:
        lines.append("لا توجد مهام بعد.")
    rows = []
    prow = pager(page, s_pages, f"{PREFIX}ops")
    if prow:
        rows.append(prow)
    ctx.show(chat, mid, "\n".join(lines), kb(*rows, nav()))


# ---- templates ----------------------------------------------------------
def _tpl_list(ctx, user, chat, mid, page):
    require(user, "search")
    conn = ctx.db
    rows = conn.execute(
        "SELECT id, name, conditions, created_at FROM nb_templates WHERE user_id=? ORDER BY id DESC",
        (user.telegram_id,),
    ).fetchall()
    t_page, pages, page = page_slice(rows, page, 10)
    lines = [f"⭐ قوالب البحث المحفوظة — صفحة {page + 1} من {pages}",
             "القوالب تحفظ الشروط فقط، لا نسخاً من النتائج.", LINE]
    kb_rows = []
    if t_page:
        for t in t_page:
            lines.append(f"• قالب #{t['id']} {t['name']} — {ago(t['created_at'])}")
            kb_rows.append([("▶️ استخدام", f"{PREFIX}tpl:use:{t['id']}"),
                            ("✏️ تعديل", f"{PREFIX}tpl:edit:{t['id']}"),
                            ("🗑 حذف", f"{PREFIX}tpl:del:{t['id']}")])
    else:
        lines.append("لا توجد قوالب محفوظة بعد. اضغط «حفظ قالب جديد» ثم أرسل اسم القالب.")
    kb_rows.append([("➕ حفظ قالب جديد", f"{PREFIX}tpl:save")])
    prow = pager(page, pages, f"{PREFIX}tpl")
    if prow:
        kb_rows.append(prow)
    ctx.show(chat, mid, "\n".join(lines), kb(*kb_rows, nav()))


def _tpl_save_prompt(ctx, user, chat, mid):
    require(user, "search")
    set_pending(ctx.db, user.telegram_id, "nb_p1_tpl_save_name", {})
    ctx.show(chat, mid,
             "⭐ حفظ قالب بحث جديد\nأرسل اسم القالب الآن.\n"
             "ستُحفظ شروط آخر بحث لك إن وُجد، وإلا سيُطلب منك كتابة شرط يدوي بسيط.\n"
             "لا تُرسل محتوى سجلات — الاسم والشروط فقط.",
             kb(nav(f"{PREFIX}tpl")))


def _tpl_use(ctx, user, chat, mid, tid):
    require(user, "search")
    row = _tpl_of(ctx.db, user, tid)
    if row is None:
        raise PermissionDenied("tpl")
    audit_nb(ctx.db, user.telegram_id, "nb_tpl_use", "template", tid, {"name": row["name"]})
    try:
        ctx.db.commit()
    except sqlite3.Error:
        pass
    ctx.show(chat, mid,
             f"⭐ قالب #{row['id']} {row['name']}\n{LINE}\n"
             f"الشروط المحفوظة:\n{_cond_summary(row['conditions'])}\n{LINE}\n"
             "ملاحظة: زر «تشغيل» الكامل لم يُوصَل بعد — تنفيذ البحث من القالب "
             "يُوصله الموجّه الرئيسي لاحقاً، وهذا العرض يسجّل استخدام القالب فقط ولا ينفّذ بحثاً.",
             kb([("⭐ القوالب", f"{PREFIX}tpl")], nav()))


def _tpl_edit_prompt(ctx, user, chat, mid, tid):
    require(user, "search")
    row = _tpl_of(ctx.db, user, tid)
    if row is None:
        raise PermissionDenied("tpl")
    set_pending(ctx.db, user.telegram_id, "nb_p1_tpl_edit_name", {"tpl_id": tid})
    ctx.show(chat, mid,
             f"✏️ تعديل قالب #{row['id']} {row['name']}\n"
             "أرسل الاسم الجديد الآن، أو أرسل - للاحتفاظ بالاسم الحالي.\n"
             "بعدها سيُطلب منك إرسال الشروط الجديدة أو - للاحتفاظ بها.",
             kb(nav(f"{PREFIX}tpl")))


def _tpl_del_confirm(ctx, user, chat, mid, tid):
    require(user, "search")
    row = _tpl_of(ctx.db, user, tid)
    if row is None:
        raise PermissionDenied("tpl")
    ctx.show(chat, mid, f"🗑 حذف قالب #{row['id']} {row['name']}؟\nلا يمكن التراجع عن الحذف.",
             confirm_markup(f"{PREFIX}tpl:del:{tid}:yes", f"{PREFIX}tpl:del:{tid}:no"))


def _tpl_del_yes(ctx, user, chat, mid, tid):
    require(user, "search")
    row = _tpl_of(ctx.db, user, tid)
    if row is None:
        raise PermissionDenied("tpl")
    ctx.db.execute("DELETE FROM nb_templates WHERE id=?", (tid,))
    audit_nb(ctx.db, user.telegram_id, "nb_tpl_delete", "template", tid, {"name": row["name"]})
    try:
        ctx.db.commit()
    except sqlite3.Error:
        pass
    ctx.show(chat, mid, f"🗑 تم حذف القالب #{tid} {row['name']}.",
             kb([("⭐ القوالب", f"{PREFIX}tpl")], nav()))


def _tpl(ctx, user, chat, mid, parts):
    if len(parts) >= 2 and parts[1] == "save":
        _tpl_save_prompt(ctx, user, chat, mid)
    elif len(parts) >= 3 and parts[1] == "use":
        _tpl_use(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else -1)
    elif len(parts) >= 3 and parts[1] == "edit":
        _tpl_edit_prompt(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else -1)
    elif len(parts) >= 4 and parts[1] == "del" and parts[3] == "yes":
        _tpl_del_yes(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else -1)
    elif len(parts) >= 4 and parts[1] == "del" and parts[3] == "no":
        require(user, "search")
        ctx.show(chat, mid, "أُلغي حذف القالب — لم يُحذف شيء.",
                 kb([("⭐ القوالب", f"{PREFIX}tpl")], nav()))
    elif len(parts) >= 3 and parts[1] == "del":
        _tpl_del_confirm(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else -1)
    elif len(parts) >= 2 and parts[1].isdigit():
        _tpl_list(ctx, user, chat, mid, int(parts[1]))
    elif len(parts) == 1:
        _tpl_list(ctx, user, chat, mid, 0)
    else:
        _tpl_list(ctx, user, chat, mid, 0)


# ---- settings -----------------------------------------------------------
def _set_screen(ctx, user, chat, mid, note=""):
    require(user, "any")
    conn = ctx.db
    uid = user.telegram_id
    ps = _get_setting(conn, uid, "page_size")
    mt = _get_setting(conn, uid, "match")
    od = _get_setting(conn, uid, "order")
    lg = _get_setting(conn, uid, "lang")
    lines = ["🎛 إعدادات البحث", LINE,
             f"حجم الصفحة: {ps}",
             f"نوع المطابقة: {MATCHES[mt]}",
             f"ترتيب النتائج: {ORDERS[od]}",
             f"لغة العرض: {LANGS[lg]}",
             "ملاحظة: نوع المطابقة والترتيب قيم محفوظة هنا؛ تطبيقها داخل محرك البحث من عمل الموجّه الرئيسي."]
    if note:
        lines.append(note)
    rows = [[((("✓ " if ps == str(n) else "") + str(n)), f"{PREFIX}set:ps:{n}") for n in PAGE_SIZES],
            [(("✓ " if mt == k else "") + v, f"{PREFIX}set:mt:{k}") for k, v in MATCHES.items()],
            [(("✓ " if od == k else "") + v, f"{PREFIX}set:ord:{k}") for k, v in ORDERS.items()],
            [(("✓ " if lg == k else "") + v, f"{PREFIX}set:lang:{k}") for k, v in LANGS.items()]]
    ctx.show(chat, mid, "\n".join(lines), kb(*rows, nav()))


def _set(ctx, user, chat, mid, parts):
    if len(parts) >= 3 and parts[1] == "ps" and parts[2] in {str(n) for n in PAGE_SIZES}:
        require(user, "any")
        _write_setting(ctx.db, user.telegram_id, "page_size", parts[2])
        _set_screen(ctx, user, chat, mid, f"✅ تم حفظ حجم الصفحة: {parts[2]}")
    elif len(parts) >= 3 and parts[1] == "mt" and parts[2] in MATCHES:
        require(user, "any")
        _write_setting(ctx.db, user.telegram_id, "match", parts[2])
        _set_screen(ctx, user, chat, mid, f"✅ تم حفظ نوع المطابقة: {MATCHES[parts[2]]}")
    elif len(parts) >= 3 and parts[1] == "ord" and parts[2] in ORDERS:
        require(user, "any")
        _write_setting(ctx.db, user.telegram_id, "order", parts[2])
        _set_screen(ctx, user, chat, mid, f"✅ تم حفظ الترتيب: {ORDERS[parts[2]]}")
    elif len(parts) >= 3 and parts[1] == "lang" and parts[2] in LANGS:
        require(user, "any")
        _write_setting(ctx.db, user.telegram_id, "lang", parts[2])
        _set_screen(ctx, user, chat, mid, f"✅ تم حفظ لغة العرض: {LANGS[parts[2]]}")
    elif len(parts) == 1:
        _set_screen(ctx, user, chat, mid)
    else:
        require(user, "any")
        ctx.show(chat, mid, "⚠️ قيمة إعداد غير صالحة — لم يُحفظ شيء.",
                 kb([("🎛 إعدادات البحث", f"{PREFIX}set")], nav()))


# ---- file details -------------------------------------------------------
def _fdet_list(ctx, user, chat, mid, page):
    require(user, "sources")
    conn = ctx.db
    if user.is_admin:
        rows = conn.execute("SELECT id, original_name, status FROM files ORDER BY id DESC").fetchall()
    else:
        ids = _own_file_ids(conn, user.telegram_id)
        if not ids:
            rows = []
        else:
            q = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, original_name, status FROM files WHERE id IN ({q}) ORDER BY id DESC",
                tuple(sorted(ids)),
            ).fetchall()
    f_page, pages, page = page_slice(rows, page, 10)
    lines = [f"📄 تفاصيل الملف — صفحة {page + 1} من {pages}",
             "اختر ملفاً لعرض حالته وعدد صفوفه وأعمدته غير المعروفة وأخطائه. لا تُعرض قيم سجلات أبداً.", LINE]
    kb_rows = []
    if f_page:
        for f in f_page:
            lines.append(f"• ملف #{f['id']} {f['original_name']} — {f['status']}")
            kb_rows.append([("📄 التفاصيل", f"{PREFIX}fdet:file:{f['id']}")])
    else:
        lines.append("لا توجد ملفات متاحة لك بعد.")
    prow = pager(page, pages, f"{PREFIX}fdet:pg")
    if prow:
        kb_rows.append(prow)
    ctx.show(chat, mid, "\n".join(lines), kb(*kb_rows, nav()))


def _fdet_detail(ctx, user, chat, mid, fid):
    require(user, "sources")
    conn = ctx.db
    f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if f is None or not _can_see_file(conn, user, fid):
        raise PermissionDenied("sources")
    cols = conn.execute(
        "SELECT f.name FROM fields f LEFT JOIN field_mappings fm ON fm.field_id = f.id "
        "WHERE f.file_id=? AND (fm.field_id IS NULL OR fm.standard_field IS NULL OR fm.standard_field='') "
        "ORDER BY f.position",
        (fid,),
    ).fetchall()
    has_cols = conn.execute("SELECT COUNT(*) FROM fields WHERE file_id=?", (fid,)).fetchone()[0]
    jobs = conn.execute(
        "SELECT id, kind, status, error FROM jobs WHERE file_id=? ORDER BY id DESC LIMIT 10",
        (fid,),
    ).fetchall()
    lines = [f"📄 تفاصيل الملف #{f['id']}", LINE,
             f"الاسم: {f['original_name']}",
             f"الحالة: {f['status']}",
             f"الحجم: {fmt_size(f['size_bytes'] or 0)}",
             f"الصفوف المقروءة: {fmt_int(f['record_count'] or 0)}"]
    if f["status"] == "partial":
        lines.append("الصفوف المتخطاة: عدد الصفوف المُحاوَلة غير محفوظ بقاعدة البيانات، "
                     "لذلك لا يُعرض رقم متخطى مختلق — المعروض هو الصفوف المقروءة فعلاً فقط.")
    lines.append("")
    if has_cols == 0:
        lines.append("الأعمدة غير المعروفة: لا توجد بيانات أعمدة لهذا الملف.")
    elif cols:
        lines.append("الأعمدة غير المعروفة (غير مربوطة بحقل معياري): " + "، ".join(c[0] for c in cols))
    else:
        lines.append("الأعمدة غير المعروفة: لا يوجد — كل الأعمدة مربوطة.")
    lines.append("")
    lines.append("أخطاء المعالجة (من مهام الملف):")
    errs = [j for j in jobs if j["error"]]
    if errs:
        for j in errs:
            lines.append(f"• JOB-{j['id']:06d} {j['kind']} ({j['status']}): {(j['error'] or '')[:300]}")
    elif f["error"]:
        lines.append(f"• ملف: {(f['error'] or '')[:300]}")
    else:
        lines.append("لا توجد أخطاء مسجلة.")
    ctx.show(chat, mid, "\n".join(lines),
             kb([("📄 قائمة الملفات", f"{PREFIX}fdet")], nav()))


def _fdet(ctx, user, chat, mid, parts):
    if len(parts) >= 3 and parts[1] == "file":
        _fdet_detail(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else -1)
    elif len(parts) >= 3 and parts[1] == "pg":
        _fdet_list(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else 0)
    elif len(parts) == 1:
        _fdet_list(ctx, user, chat, mid, 0)
    else:
        _fdet_list(ctx, user, chat, mid, 0)


# ---- notifications ------------------------------------------------------
def _ntf_prefs(conn, uid):
    row = conn.execute("SELECT on_finish, on_fail FROM nb_notif_prefs WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return 1, 1
    return int(row[0]), int(row[1])


def _ntf_screen(ctx, user, chat, mid, note=""):
    require(user, "any")
    fin, fail = _ntf_prefs(ctx.db, user.telegram_id)
    lines = ["🔔 إشعاراتي", LINE,
             f"عند انتهاء مهمة: {'🟢 مفعّل' if fin else '🔴 معطّل'}",
             f"عند فشل مهمة: {'🟢 مفعّل' if fail else '🔴 معطّل'}",
             "ملاحظة: هذه التفضيلات محفوظة فعلاً هنا؛ إرسال الإشعارات نفسه من توصيل الموجّه الرئيسي."]
    if note:
        lines.append(note)
    ctx.show(chat, mid, "\n".join(lines),
             kb([("🔔 انتهاء المهمة: تبديل", f"{PREFIX}ntf:finish"),
                 ("🔔 فشل المهمة: تبديل", f"{PREFIX}ntf:fail")], nav()))


def _ntf(ctx, user, chat, mid, parts):
    if len(parts) >= 2 and parts[1] in ("finish", "fail"):
        require(user, "any")
        conn = ctx.db
        col = "on_finish" if parts[1] == "finish" else "on_fail"
        fin, fail = _ntf_prefs(conn, user.telegram_id)
        new = 1 - (fin if parts[1] == "finish" else fail)
        conn.execute(
            "INSERT INTO nb_notif_prefs(user_id, on_finish, on_fail) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET on_finish=excluded.on_finish, on_fail=excluded.on_fail",
            (user.telegram_id, new if parts[1] == "finish" else fin, new if parts[1] == "fail" else fail),
        )
        audit_nb(ctx.db, user.telegram_id, "nb_notif_change", "notif_pref", col, {"new": new})
        try:
            conn.commit()
        except sqlite3.Error:
            pass
        _ntf_screen(ctx, user, chat, mid, f"✅ تم تحديث تفضيل «{col}» إلى {'مفعّل' if new else 'معطّل'}.")
    else:
        _ntf_screen(ctx, user, chat, mid)


# ---- permissions / usage ------------------------------------------------
def _perm_screen(ctx, user, chat, mid, page, note=""):
    require(user, "any")
    conn = ctx.db
    uid = user.telegram_id
    if user.is_admin:
        mine = list(PERMISSIONS)
    else:
        mine = [p for p in PERMISSIONS if p in user.perms]
    exp_count = conn.execute("SELECT COUNT(*) FROM reports WHERE user_id=?", (uid,)).fetchone()[0]
    acts = conn.execute(
        "SELECT id, action, target_type, ts FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (uid,),
    ).fetchall()
    reqs = conn.execute(
        "SELECT id, perm, status, created_at FROM nb_perm_requests WHERE user_id=? ORDER BY id DESC",
        (uid,),
    ).fetchall()
    r_page, pages, page = page_slice(reqs, page, 10)
    lines = ["🔐 صلاحياتي واستخدامي", LINE, "صلاحياتك:"]
    if mine:
        for p in mine:
            lines.append(f"• {PERM_AR.get(p, p)}")
    else:
        lines.append("• لا توجد صلاحيات ممنوحة بعد.")
    lines += ["", f"تقاريرك المصدّرة: {fmt_int(exp_count)}",
              "ملاحظة: حدود التصدير تُفرض من عمل حدود الموجّه الرئيسي، وليست مفعّلة هنا — "
              "لا يوجد رقم حدّ معروض لأن لا حدّ مفروضاً من هذه الشاشة.",
              "", "آخر نشاطك (إجراءاتك فقط):"]
    if acts:
        for a in acts:
            lines.append(f"• #{a['id']} {a['action']} {a['target_type'] or ''} — {ago(a['ts'])}")
    else:
        lines.append("لا يوجد نشاط مسجل بعد.")
    lines += ["", f"طلبات مراجعة الصلاحية — صفحة {page + 1} من {pages}:"]
    kb_rows = []
    if r_page:
        for r in r_page:
            lines.append(f"• طلب #{r['id']} {PERM_AR.get(r['perm'], r['perm'])} — {r['status']}")
            if r["status"] == "pending":
                kb_rows.append([("🗑 حذف الطلب", f"{PREFIX}perm:del:{r['id']}")])
    else:
        lines.append("لا توجد طلبات بعد.")
    missing = [p for p in PERMISSIONS if p not in mine]
    if missing:
        lines.append("")
        lines.append("اطلب صلاحية (أرسل السبب بعد الاختيار):")
        for p in missing:
            kb_rows.append([(f"➕ طلب {PERM_AR.get(p, p)}", f"{PREFIX}perm:req:{p}")])
    if note:
        lines.append(note)
    prow = pager(page, pages, f"{PREFIX}perm:pg")
    if prow:
        kb_rows.append(prow)
    ctx.show(chat, mid, "\n".join(lines), kb(*kb_rows, nav()))


def _perm(ctx, user, chat, mid, parts):
    if len(parts) >= 3 and parts[1] == "req":
        require(user, "any")
        perm = parts[2]
        if perm not in PERMISSIONS:
            ctx.show(chat, mid, "⚠️ صلاحية غير معروفة.", kb([("🔐 صلاحياتي", f"{PREFIX}perm")], nav()))
            return
        if user.is_admin or perm in user.perms:
            ctx.show(chat, mid, "لديك هذه الصلاحية أصلاً — لا حاجة لطلبها.",
                     kb([("🔐 صلاحياتي", f"{PREFIX}perm")], nav()))
            return
        existing = ctx.db.execute(
            "SELECT id FROM nb_perm_requests WHERE user_id=? AND perm=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user.telegram_id, perm),
        ).fetchone()
        if existing:
            ctx.show(chat, mid, f"لديك طلب معلّق أصلاً لهذه الصلاحية: طلب #{existing[0]} — لم يُنشأ طلب مكرر.",
                     kb([("🔐 صلاحياتي", f"{PREFIX}perm")], nav()))
            return
        set_pending(ctx.db, user.telegram_id, "nb_p1_perm_req", {"perm": perm})
        ctx.show(chat, mid, f"🔐 طلب صلاحية {PERM_AR.get(perm, perm)}\nأرسل سبب الطلب الآن (نص مختصر، بلا محتوى سجلات).",
                 kb(nav(f"{PREFIX}perm")))
        return
    if len(parts) >= 4 and parts[1] == "del" and parts[3] == "yes":
        require(user, "any")
        rid = int(parts[2]) if parts[2].isdigit() else -1
        row = ctx.db.execute("SELECT * FROM nb_perm_requests WHERE id=?", (rid,)).fetchone()
        if row is None or row["user_id"] != user.telegram_id:
            raise PermissionDenied("perm_request")
        ctx.db.execute("DELETE FROM nb_perm_requests WHERE id=?", (rid,))
        audit_nb(ctx.db, user.telegram_id, "nb_perm_request_delete", "perm_request", rid, {"perm": row["perm"]})
        try:
            ctx.db.commit()
        except sqlite3.Error:
            pass
        ctx.show(chat, mid, f"🗑 تم حذف طلب الصلاحية #{rid}.",
                 kb([("🔐 صلاحياتي", f"{PREFIX}perm")], nav()))
        return
    if len(parts) >= 4 and parts[1] == "del" and parts[3] == "no":
        require(user, "any")
        ctx.show(chat, mid, "أُلغي حذف طلب الصلاحية — لم يُحذف شيء.",
                 kb([("🔐 صلاحياتي", f"{PREFIX}perm")], nav()))
        return
    if len(parts) >= 3 and parts[1] == "del":
        require(user, "any")
        rid = int(parts[2]) if parts[2].isdigit() else -1
        row = ctx.db.execute("SELECT * FROM nb_perm_requests WHERE id=?", (rid,)).fetchone()
        if row is None or row["user_id"] != user.telegram_id:
            raise PermissionDenied("perm_request")
        ctx.show(chat, mid, f"🗑 حذف طلب الصلاحية #{rid} ({PERM_AR.get(row['perm'], row['perm'])})؟",
                 confirm_markup(f"{PREFIX}perm:del:{rid}:yes", f"{PREFIX}perm:del:{rid}:no"))
        return
    if len(parts) >= 3 and parts[1] == "pg":
        _perm_screen(ctx, user, chat, mid, int(parts[2]) if parts[2].isdigit() else 0)
        return
    _perm_screen(ctx, user, chat, mid, 0)


# ---- help / issues ------------------------------------------------------
_KINDS = {"search": "🔎 البحث", "file": "📁 الملفات", "report": "📤 التقارير"}


def _help_screen(ctx, user, chat, mid):
    require(user, "any")
    ctx.show(chat, mid,
             "🆘 المساعدة والإبلاغ عن مشكلة\nاختر نوع المشكلة، ثم أرسل رقم المهمة إن وُجد أو - إن لم يكن لها رقم.\n"
             "لا تُرسل محتوى سجلات أبداً — وصف المشكلة يكفي دون قيم.",
             kb([("🔎 مشكلة بالبحث", f"{PREFIX}help:kind:search"),
                 ("📁 مشكلة بملف", f"{PREFIX}help:kind:file")],
                [("📤 مشكلة بتقرير", f"{PREFIX}help:kind:report"),
                 ("📋 بلاغاتي", f"{PREFIX}help:list")], nav()))


def _help_list(ctx, user, chat, mid, page):
    require(user, "any")
    rows = ctx.db.execute(
        "SELECT id, kind, job_id, status, created_at FROM nb_issue_reports WHERE user_id=? ORDER BY id DESC",
        (user.telegram_id,),
    ).fetchall()
    r_page, pages, page = page_slice(rows, page, 10)
    lines = [f"🆘 بلاغاتي — صفحة {page + 1} من {pages}", LINE]
    if r_page:
        for r in r_page:
            job = f"JOB-{r['job_id']:06d}" if r["job_id"] is not None else "بلا رقم مهمة"
            lines.append(f"• بلاغ #{r['id']} {_KINDS.get(r['kind'], r['kind'])} — {job} — {r['status']} — {ago(r['created_at'])}")
    else:
        lines.append("لا توجد بلاغات بعد.")
    kb_rows = []
    prow = pager(page, pages, f"{PREFIX}help:list")
    if prow:
        kb_rows.append(prow)
    ctx.show(chat, mid, "\n".join(lines), kb(*kb_rows, nav(f"{PREFIX}help")))


def _help(ctx, user, chat, mid, parts):
    if len(parts) >= 3 and parts[1] == "kind" and parts[2] in _KINDS:
        require(user, "any")
        set_pending(ctx.db, user.telegram_id, "nb_p1_issue", {"kind": parts[2]})
        ctx.show(chat, mid,
                 f"🆘 بلاغ جديد: {_KINDS[parts[2]]}\n"
                 "أرسل رقم المهمة (أرقام فقط) إن وُجد، أو أرسل - إن لم يكن للمشكلة رقم مهمة.\n"
                 "لا تُرسل محتوى سجلات.",
                 kb(nav(f"{PREFIX}help")))
        return
    if len(parts) >= 3 and parts[1] == "list" and parts[2].isdigit():
        _help_list(ctx, user, chat, mid, int(parts[2]))
        return
    if len(parts) >= 2 and parts[1] == "list":
        _help_list(ctx, user, chat, mid, 0)
        return
    _help_screen(ctx, user, chat, mid)


# ---- router -------------------------------------------------------------
def handle(ctx, user, chat, mid, parts) -> bool:
    ensure_nb_tables(ctx.db)
    if not parts or parts[0] not in _KEYS:
        return False
    key = parts[0]
    try:
        if key == "ops":
            _ops(ctx, user, chat, mid, parts)
        elif key == "tpl":
            _tpl(ctx, user, chat, mid, parts)
        elif key == "set":
            _set(ctx, user, chat, mid, parts)
        elif key == "fdet":
            _fdet(ctx, user, chat, mid, parts)
        elif key == "ntf":
            _ntf(ctx, user, chat, mid, parts)
        elif key == "perm":
            _perm(ctx, user, chat, mid, parts)
        elif key == "help":
            _help(ctx, user, chat, mid, parts)
    except PermissionDenied:
        _deny_show(ctx, user, chat, mid, key)
    except (AppError, ValueError, sqlite3.Error, IndexError, KeyError) as exc:
        ctx.show(chat, mid, f"⚠️ حدث خطأ: {exc}"[:500], kb(nav()))
    return True


# ---- text input ---------------------------------------------------------
def _peek_pending(conn, uid):
    try:
        return conn.execute("SELECT action, payload FROM nb_pending_text WHERE user_id=?", (uid,)).fetchone()
    except sqlite3.Error:
        return None


def _send_deny(ctx, user, chat, what):
    ctx.deny(user.telegram_id, what)
    ctx.send(chat, "⛔ غير مصرح لك بهذا الإجراء.")


def handle_text(ctx, user, chat, text) -> bool:
    ensure_nb_tables(ctx.db)
    row = _peek_pending(ctx.db, user.telegram_id)
    if row is None:
        return False
    action = row["action"]
    if not action.startswith("nb_p1_"):
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    # consume now; re-set below only on the retry path (bad job number)
    ctx.db.execute("DELETE FROM nb_pending_text WHERE user_id=?", (user.telegram_id,))
    try:
        ctx.db.commit()
    except sqlite3.Error:
        pass
    txt = (text or "").strip()
    conn = ctx.db
    try:
        if action == "nb_p1_tpl_save_name":
            require(user, "search")
            if not txt:
                ctx.send(chat, "⚠️ الاسم فارغ — اضغط «حفظ قالب جديد» وأعد المحاولة.")
                return True
            name = txt[:100]
            last = conn.execute(
                "SELECT query_json FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user.telegram_id,),
            ).fetchone()
            if last is None:
                set_pending(conn, user.telegram_id, "nb_p1_tpl_save_cond", {"name": name})
                ctx.send(chat, "لا يوجد بحث سابق لك. أرسل شرطاً يدوياً بسيطاً للقالب الآن (نص مختصر، بلا محتوى سجلات).")
                return True
            try:
                cond = json.loads(last["query_json"] or "{}")
                cond_text = json.dumps(cond, ensure_ascii=False) if isinstance(cond, dict) else json.dumps({"condition": cond}, ensure_ascii=False)
            except ValueError:
                cond_text = json.dumps({"condition_text": last["query_json"]}, ensure_ascii=False)
            existing = conn.execute(
                "SELECT id FROM nb_templates WHERE user_id=? AND name=?", (user.telegram_id, name)).fetchone()
            if existing:
                conn.execute("UPDATE nb_templates SET conditions=? WHERE id=?", (cond_text, existing[0]))
                tid, note = existing[0], "تم تحديث قالب موجود بنفس الاسم."
            else:
                tid = conn.execute(
                    "INSERT INTO nb_templates(user_id, name, conditions) VALUES (?,?,?)",
                    (user.telegram_id, name, cond_text)).lastrowid
                note = "تم حفظ القالب من شروط آخر بحث لك."
            audit_nb(conn, user.telegram_id, "nb_tpl_save", "template", tid, {"name": name})
            conn.commit()
            ctx.send(chat, f"⭐ تم حفظ القالب #{tid} {name}. {note}")
            return True

        if action == "nb_p1_tpl_save_cond":
            require(user, "search")
            name = payload.get("name", "قالب")[:100]
            cond_text = json.dumps({"manual_condition": txt}, ensure_ascii=False)
            tid = conn.execute(
                "INSERT INTO nb_templates(user_id, name, conditions) VALUES (?,?,?)",
                (user.telegram_id, name, cond_text)).lastrowid
            audit_nb(conn, user.telegram_id, "nb_tpl_save", "template", tid, {"name": name})
            conn.commit()
            ctx.send(chat, f"⭐ تم حفظ القالب #{tid} {name} بالشرط اليدوي.")
            return True

        if action == "nb_p1_tpl_edit_name":
            require(user, "search")
            tid = payload.get("tpl_id")
            tpl = _tpl_of(conn, user, tid) if isinstance(tid, int) else None
            if tpl is None:
                raise PermissionDenied("tpl")
            name = tpl["name"] if txt == "-" or not txt else txt[:100]
            set_pending(conn, user.telegram_id, "nb_p1_tpl_edit_cond", {"tpl_id": tid, "name": name})
            ctx.send(chat, "أرسل الشروط الجديدة الآن (نص مختصر أو JSON)، أو أرسل - للاحتفاظ بالشروط الحالية.")
            return True

        if action == "nb_p1_tpl_edit_cond":
            require(user, "search")
            tid = payload.get("tpl_id")
            tpl = _tpl_of(conn, user, tid) if isinstance(tid, int) else None
            if tpl is None:
                raise PermissionDenied("tpl")
            name = payload.get("name", tpl["name"])[:100]
            if txt != "-":
                try:
                    parsed = json.loads(txt)
                    cond_text = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else json.dumps({"manual_condition": txt}, ensure_ascii=False)
                except ValueError:
                    cond_text = json.dumps({"manual_condition": txt}, ensure_ascii=False)
                conn.execute("UPDATE nb_templates SET name=?, conditions=? WHERE id=?", (name, cond_text, tid))
            else:
                conn.execute("UPDATE nb_templates SET name=? WHERE id=?", (name, tid))
            audit_nb(conn, user.telegram_id, "nb_tpl_edit", "template", tid, {"name": name})
            conn.commit()
            ctx.send(chat, f"✏️ تم تعديل القالب #{tid} {name}.")
            return True

        if action == "nb_p1_perm_req":
            require(user, "any")
            perm = payload.get("perm")
            if perm not in PERMISSIONS or user.is_admin or perm in user.perms:
                ctx.send(chat, "⚠️ طلب غير صالح أو صلاحية لديك أصلاً — لم يُنشأ طلب.")
                return True
            existing = conn.execute(
                "SELECT id FROM nb_perm_requests WHERE user_id=? AND perm=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (user.telegram_id, perm)).fetchone()
            if existing:
                ctx.send(chat, f"لديك طلب معلّق أصلاً: طلب #{existing[0]} — لم يُنشأ طلب مكرر.")
                return True
            rid = conn.execute(
                "INSERT INTO nb_perm_requests(user_id, perm, reason) VALUES (?,?,?)",
                (user.telegram_id, perm, txt[:500])).lastrowid
            audit_nb(conn, user.telegram_id, "nb_perm_request", "perm_request", rid, {"perm": perm})
            conn.commit()
            ctx.send(chat, f"🔐 تم إنشاء طلب مراجعة الصلاحية #{rid} ({PERM_AR.get(perm, perm)}) — الحالة: pending. "
                           "موافقة الأدمن تتم من شاشة المرحلة 3، وليست هنا.")
            return True

        if action == "nb_p1_issue":
            require(user, "any")
            kind = payload.get("kind")
            if kind not in _KINDS:
                ctx.send(chat, "⚠️ نوع بلاغ غير صالح.")
                return True
            if txt in ("-", "", "لا يوجد", "لايوجد"):
                job_id = None
            elif txt.isdigit():
                job_id = int(txt)
                jrow = conn.execute("SELECT created_by FROM jobs WHERE id=?", (job_id,)).fetchone()
                if jrow is not None and jrow[0] != user.telegram_id and not user.is_admin:
                    set_pending(conn, user.telegram_id, "nb_p1_issue", {"kind": kind})
                    ctx.send(chat, "⚠️ رقم المهمة هذا غير متاح لك — أرسل رقم مهمة خاصة بك أو -.")
                    return True
            else:
                set_pending(conn, user.telegram_id, "nb_p1_issue", {"kind": kind})
                ctx.send(chat, "⚠️ أرسل رقم مهمة (أرقام فقط) أو - إن لم يكن لها رقم.")
                return True
            rid = conn.execute(
                "INSERT INTO nb_issue_reports(user_id, kind, job_id) VALUES (?,?,?)",
                (user.telegram_id, kind, job_id)).lastrowid
            audit_nb(conn, user.telegram_id, "nb_issue_report", "issue", rid, {"kind": kind})
            conn.commit()
            ctx.send(chat, f"🆘 تم إنشاء البلاغ #{rid} ({_KINDS[kind]}) — الحالة: open."
                           + (f" مرتبط بـ JOB-{job_id:06d}." if job_id is not None else " بلا رقم مهمة."))
            return True
    except PermissionDenied:
        _send_deny(ctx, user, chat, action)
        return True
    except (AppError, ValueError, sqlite3.Error, IndexError, KeyError) as exc:
        ctx.send(chat, f"⚠️ حدث خطأ: {exc}"[:500])
        return True
    return False
