"""Phase 4 — protection / audit / privacy (nb:p4:*).

All buttons admin-only (perm "manage"). Screens show counts, ids, statuses,
file ids and scrubbed audit details ONLY. Record field values are never
read here (record_values is never queried), and backup file CONTENTS are
never read (directory listing + sizes only, never opened/decrypted).

Honest limitations, stated on the screens themselves:
- Audit export: NOT stored as a reports row — the reports schema has no
  kind column and requires a file path + a format in ('html','txt','pdf'),
  so inventing one would be dishonest. The export is paginated screen
  text, and the export action itself is audited (action 'audit_export').
- Retention policy: this phase only SHOWS and SETS the policy
  (nb_retention). Actual deletion per policy is NOT performed here; a
  separate safe-cleanup action (Phase 5) applies it.
- Deletion-request approval: marks the request 'approved' ONLY. It does
  NOT delete production data by itself; actual deletion is a separate,
  explicit operator step defined by the parent.
- Export lock / file isolation: the ROWS written here are real and
  queryable, but enforcement at export-submit / search / intake is parent
  wiring, not this module.
- Backup check: directory listing + last backup audit entries only; no
  backup content is read, decrypted or printed. If no backup directory is
  configured in ctx.cfg, that is reported honestly.
- Restore test: DRY-RUN ONLY. No restore is executed, no production data
  is touched; only an nb_settings_log entry + an audit row are written.
  A real restore test needs the parent's terminal-only restore path
  (app.cli restore), which this module never invokes.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ..errors import PermissionDenied
from .nb_store import (
    audit_nb,
    confirm_markup,
    ensure_nb_tables,
    export_locked,
    page_slice,
    pop_pending,
    require,
    set_pending,
)
from .ui import LINE, ago, fmt_int, kb, nav, pager

PREFIX = "nb:p4:"

BUTTONS = [
    {"key": "sec", "label": "🛡 مركز الأمان", "perm": "manage", "confirm": False, "audit": None},
    {"key": "alt", "label": "🚨 التنبيهات الأمنية", "perm": "manage", "confirm": False, "audit": None},
    {"key": "det", "label": "🧾 تفاصيل عملية التدقيق", "perm": "manage", "confirm": False, "audit": None},
    {"key": "sch", "label": "🔎 البحث في سجل التدقيق", "perm": "manage", "confirm": False, "audit": None},
    {"key": "exp", "label": "📦 تصدير سجل التدقيق", "perm": "manage", "confirm": True, "audit": "audit_export"},
    {"key": "ret", "label": "🧹 سياسة الاحتفاظ بالبيانات", "perm": "manage", "confirm": True, "audit": "settings"},
    {"key": "del", "label": "🗑 طلبات حذف البيانات", "perm": "manage", "confirm": True, "audit": "del_request"},
    {"key": "bkc", "label": "🔐 فحص النسخ الاحتياطية", "perm": "manage", "confirm": False, "audit": None},
    {"key": "rst", "label": "🧪 اختبار استعادة احتياطي", "perm": "manage", "confirm": True, "audit": "restore_test"},
    {"key": "iso", "label": "🚫 عزل ملف", "perm": "manage", "confirm": True, "audit": "isolate"},
    {"key": "chg", "label": "📝 سجل تغييرات الإعدادات", "perm": "manage", "confirm": False, "audit": None},
    {"key": "lck", "label": "🔒 قفل التصدير مؤقتاً", "perm": "manage", "confirm": True, "audit": "export_lock"},
]

_KEYS = {b["key"] for b in BUTTONS}
_PERMS = ("search", "smart", "sources", "stats", "reports", "export", "upload")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAGE = 10


# ---------------------------------------------------------------- helpers

def _show(ctx, chat, mid, text, rows):
    rows = [r for r in rows if r]
    rows.append(nav())
    ctx.show(chat, mid, text, kb(*rows))


def _deny(ctx, user, chat, mid, perm):
    ctx.deny(user.telegram_id, perm)
    _show(ctx, chat, mid, "⛔ غير مصرح: هذا الإجراء للأدمن فقط.", [])


def _guard(ctx, user):
    """True when allowed; shows the denial screen and returns False otherwise."""
    try:
        require(user, "manage")
    except PermissionDenied:
        return False
    return True


def _audit_row_text(r):
    target = ((r["target_type"] or "") + ":" + (r["target_id"] or "")).strip(":") or "-"
    return f"#{r['id']} | مستخدم {r['user_id']} | {r['action']} | {target} | {ago(r['ts'])}"


def _pending_payload(ctx, uid):
    row = ctx.db.execute(
        "SELECT action, payload FROM nb_pending_text WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return None, {}
    try:
        return row["action"], json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return row["action"], {}


def _cancelled(ctx, chat, mid):
    _show(ctx, chat, mid, "أُلغي — لم يُنفَّذ شيء.", [[("◀️ رجوع", PREFIX + "sec")]])


# ---------------------------------------------------------------- security center

def _sec(ctx, chat, mid):
    db = ctx.db
    admins = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
    enabled = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='user' AND enabled=1").fetchone()[0]
    perms = {}
    for r in db.execute("SELECT permissions FROM users WHERE role='user' AND enabled=1"):
        try:
            have = set(json.loads(r["permissions"] or "[]"))
        except ValueError:
            have = set()
        for p in _PERMS:
            if p in have:
                perms[p] = perms.get(p, 0) + 1
    iso = db.execute("SELECT file_id, reason FROM nb_isolated_files ORDER BY file_id").fetchall()
    denied_n = db.execute("SELECT COUNT(*) FROM audit_log WHERE action='denied'").fetchone()[0]
    denied = db.execute(
        "SELECT id, user_id, target_type, target_id, ts FROM audit_log "
        "WHERE action='denied' ORDER BY id DESC LIMIT 10").fetchall()
    lines = ["🛡 مركز الأمان", LINE,
             f"الأدمن: {fmt_int(admins)} | المستخدمون المفعّلون: {fmt_int(enabled)}",
             "المستخدمون لكل صلاحية: " + ("، ".join(f"{p}={perms.get(p, 0)}" for p in _PERMS)),
             f"قفل التصدير: {'🔒 مفعّل' if export_locked(db) else '🔓 غير مفعّل'}",
             f"الملفات المعزولة: {fmt_int(len(iso))}"]
    for r in iso:
        lines.append(f"  • ملف {r['file_id']} — السبب: {r['reason'] or '-'}")
    lines += [f"محاولات وصول مرفوضة (audit action='denied'): {fmt_int(denied_n)}"]
    for r in denied:
        target = ((r["target_type"] or "") + ":" + (r["target_id"] or "")).strip(":") or "-"
        lines.append(f"  • #{r['id']} مستخدم {r['user_id']} هدف {target} {ago(r['ts'])}")
    lines.append("ملاحظة: القفل والعزل صفوف حقيقية قابلة للاستعلام؛ التنفيذ عند "
                 "التصدير/البحث/الإدخال توصيل من الأب (parent wiring) وليس هنا.")
    rows = [[("🚨 التنبيهات الأمنية", PREFIX + "alt"),
             ("🧾 تفاصيل عملية", PREFIX + "det")],
            [("🔎 البحث بالسجل", PREFIX + "sch"),
             ("📦 تصدير السجل", PREFIX + "exp")],
            [("🧹 سياسة الاحتفاظ", PREFIX + "ret"),
             ("🗑 طلبات الحذف", PREFIX + "del")],
            [("🔐 فحص النسخ", PREFIX + "bkc"),
             ("🧪 اختبار استعادة", PREFIX + "rst")],
            [("🚫 عزل ملف", PREFIX + "iso"),
             ("📝 سجل الإعدادات", PREFIX + "chg")],
            [("🔒 قفل التصدير", PREFIX + "lck")]]
    _show(ctx, chat, mid, "\n".join(lines), rows)


# ---------------------------------------------------------------- alerts

def _alt(ctx, chat, mid):
    db = ctx.db
    by_user = db.execute(
        "SELECT user_id, COUNT(*) AS n FROM audit_log WHERE action='denied' "
        "AND ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day') "
        "GROUP BY user_id ORDER BY n DESC").fetchall()
    failed_today = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='failed' "
        "AND substr(created_at,1,10) = substr(strftime('%Y-%m-%dT%H:%M:%fZ','now'),1,10)").fetchone()[0]
    failed_30 = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='failed' "
        "AND substr(created_at,1,10) >= substr(strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days'),1,10)").fetchone()[0]
    avg = failed_30 / 30.0
    denied_total = db.execute("SELECT COUNT(*) FROM audit_log WHERE action='denied'").fetchone()[0]
    failed_total = db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
    lines = ["🚨 التنبيهات الأمنية (قراءة فقط — أعداد ومعرّفات وأوقات فقط، بلا قيم سجلات وبلا أسرار)",
             LINE, "محاولات مرفوضة آخر 24 ساعة (حسب المستخدم):"]
    if not by_user:
        lines.append("  • لا شيء")
    for r in by_user:
        lines.append(f"  • مستخدم {r['user_id']}: {fmt_int(r['n'])} محاولة مرفوضة")
    spike = " ⚠️ ارتفاع عن المتوسط" if failed_today > avg and failed_today > 0 else ""
    lines += [f"وظائف فاشلة اليوم: {fmt_int(failed_today)} مقابل متوسط يومي "
              f"(آخر 30 يوماً، نافذة معلنة): {avg:.2f}{spike}",
              "إخفاقات الفحص الأمني (إجمالي): "
              f"وصول مرفوض {fmt_int(denied_total)} + وظائف فاشلة {fmt_int(failed_total)}"]
    _show(ctx, chat, mid, "\n".join(lines), [[("◀️ رجوع", PREFIX + "sec")]])


# ---------------------------------------------------------------- audit detail

def _det_list(ctx, chat, mid):
    rows = ctx.db.execute(
        "SELECT id, user_id, action, target_type, target_id, ts FROM audit_log "
        "ORDER BY id DESC LIMIT 10").fetchall()
    lines = ["🧾 تفاصيل عملية التدقيق — أحدث 10 عمليات (اضغط رقماً للتفاصيل):", LINE]
    btns = []
    for r in rows:
        lines.append(_audit_row_text(r))
        btns.append((f"#{r['id']}", f"{PREFIX}det:{r['id']}"))
    grid = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    grid.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), grid)


def _det_one(ctx, chat, mid, raw_id):
    try:
        aid = int(raw_id)
    except (TypeError, ValueError):
        aid = -1
    r = ctx.db.execute("SELECT * FROM audit_log WHERE id=?", (aid,)).fetchone()
    if r is None:
        _show(ctx, chat, mid, "غير موجود — لا توجد عملية تدقيق بهذا الرقم.",
              [[("◀️ رجوع", PREFIX + "det")]])
        return
    details = r["details"] or "-"
    ok = r["action"] != "denied" and "error" not in (details or "").lower()
    lines = ["🧾 تفاصيل عملية التدقيق", LINE,
             f"الرقم: #{r['id']}", f"المستخدم: {r['user_id']}", f"الوقت: {ago(r['ts'])}",
             f"الإجراء: {r['action']}",
             f"الهدف: {(r['target_type'] or '-')}/{(r['target_id'] or '-')}",
             f"النتيجة: {'✅ نجاح' if ok else '❌ فشل/مرفوض'}",
             "اشتقاق النتيجة (معلن بصراحة): النجاح = الإجراء ليس 'denied' "
             "ولا توجد علامة خطأ (error) بالتفاصيل المخزنة.",
             f"التفاصيل المخزنة (مطهّرة أصلاً من وحدة التدقيق — تُعرض كما هي، بلا فك تطهير): {details}"]
    _show(ctx, chat, mid, "\n".join(lines), [[("◀️ رجوع", PREFIX + "det")]])


# ---------------------------------------------------------------- audit search

def _sch_screen(ctx, chat, mid, page=0):
    _action, payload = _pending_payload(ctx, ctx_user_id[0])
    kind, value = payload.get("kind"), payload.get("value")
    lines = ["🔎 البحث في سجل التدقيق", LINE,
             "اكتب أحد الصيغ التالية (رسالة نصية):",
             "  • user:<رقم المستخدم>  • action:<اسم الإجراء>  • date:<YYYY-MM-DD>",
             "عمود الوقت في audit_log هو ts، والبحث بالتاريخ بادئة على ts."]
    rows = []
    if kind:
        q, params = _sch_query(kind, value)
        found = ctx.db.execute(q, params).fetchall()
        sliced, pages, page = page_slice(found, page, _PAGE)
        lines.append(f"المرشح الحالي: {kind}={value} — النتائج: {fmt_int(len(found))} (صفحة {page + 1}/{pages})")
        for r in sliced:
            lines.append(_audit_row_text(r))
        pg = pager(page, pages, PREFIX + "sch:pg")
        if pg:
            rows.append(pg)
        rows.append([("🗑 مسح المرشح", PREFIX + "sch:clear")])
    rows.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), rows)


def _sch_query(kind, value):
    base = "SELECT id, user_id, action, target_type, target_id, ts FROM audit_log "
    if kind == "user":
        return base + "WHERE user_id=? ORDER BY id DESC", (value,)
    if kind == "action":
        return base + "WHERE action=? ORDER BY id DESC", (value,)
    return base + "WHERE substr(ts,1,10)=? ORDER BY id DESC", (value,)


# ---------------------------------------------------------------- audit export

def _exp_screen(ctx, chat, mid):
    total = ctx.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    by_action = ctx.db.execute(
        "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY n DESC LIMIT 10").fetchall()
    set_pending(ctx.db, ctx_user_id[0], "p4:exp", {})
    lines = ["📦 تصدير سجل التدقيق", LINE,
             f"إجمالي صفوف التدقيق: {fmt_int(total)}",
             "العدد حسب الإجراء: " + ("، ".join(f"{r['action']}={r['n']}" for r in by_action) or "-"),
             "التصدير الافتراضي: آخر 100 صف خلال آخر 30 يوماً (نافذة معلنة).",
             "أو اكتب نطاقاً بصيغة YYYY-MM-DD..YYYY-MM-DD ثم أكّد.",
             "التخزين (معلن بصراحة): التصدير نص مقسّم على شاشات، وليس صفاً "
             "بجدول reports — مخطط reports بلا عمود kind ويتطلب مسار ملف "
             "وصيغة من (html,txt,pdf)، فاختلاق صف فيه تزوير. فعل التصدير نفسه "
             "يُسجَّل بالتدقيق (audit_export). التفاصيل المعروضة مطهّرة فقط."]
    _show(ctx, chat, mid, "\n".join(lines),
          [[("✅ تأكيد التصدير", PREFIX + "exp:yes")],
           [("❌ إلغاء", PREFIX + "exp:no")],
           [("◀️ رجوع", PREFIX + "sec")]])


def _exp_rows(ctx):
    _action, payload = _pending_payload(ctx, ctx_user_id[0])
    frm, to = payload.get("from"), payload.get("to")
    if frm and to:
        return ctx.db.execute(
            "SELECT * FROM audit_log WHERE substr(ts,1,10) BETWEEN ? AND ? "
            "ORDER BY id DESC LIMIT 100", (frm, to)).fetchall()
    return ctx.db.execute(
        "SELECT * FROM audit_log WHERE substr(ts,1,10) >= "
        "substr(strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days'),1,10) "
        "ORDER BY id DESC LIMIT 100").fetchall()


def _exp_page(ctx, chat, mid, page=0):
    rows = _exp_rows(ctx)
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = [f"📦 تصدير سجل التدقيق — {fmt_int(len(rows))} صفاً (صفحة {page + 1}/{pages})",
             "التفاصيل مطهّرة فقط؛ هذا التصدير نفسه مسجّل بالتدقيق (audit_export).", LINE]
    for r in sliced:
        lines.append(_audit_row_text(r))
        lines.append(f"   التفاصيل: {(r['details'] or '-')[:200]}")
    pg = pager(page, pages, PREFIX + "exp:pg")
    out = [pg] if pg else []
    out.append([("◀️ رجوع", PREFIX + "exp")])
    _show(ctx, chat, mid, "\n".join(lines), out)


# ---------------------------------------------------------------- retention

def _ret_screen(ctx, chat, mid):
    rows = ctx.db.execute("SELECT kind, days FROM nb_retention ORDER BY kind").fetchall()
    lines = ["🧹 سياسة الاحتفاظ بالبيانات", LINE,
             "التنفيذ الفعلي للحذف حسب السياسة ليس من مهام هذه المرحلة — "
             "إجراء تنظيف آمن منفصل (المرحلة 5) هو من يطبّقها. هنا عرض وتعيين فقط."]
    btns = []
    for r in rows:
        lines.append(f"  • {r['kind']}: {r['days']} يوماً")
        btns.append((f"تعيين {r['kind']}", f"{PREFIX}ret:set:{r['kind']}"))
    grid = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    grid.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), grid)


# ---------------------------------------------------------------- deletion requests

def _del_screen(ctx, chat, mid, page=0):
    rows = ctx.db.execute(
        "SELECT * FROM nb_del_requests ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, "
        "id DESC").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = [f"🗑 طلبات حذف البيانات — المعلقة أولاً (صفحة {page + 1}/{pages})", LINE,
             "⚠️ تنبيه صريح: الموافقة على طلب تعني تعليم الطلب 'approved' فقط — "
             "هي لا تحذف بيانات الإنتاج بنفسها أبداً. الحذف الفعلي خطوة مشغّل "
             "منفصلة وصريحة يحددها الأب. الحذف الصامت ممنوع."]
    grid = []
    for r in sliced:
        lines.append(f"#{r['id']} | طالب {r['user_id']} | {r['target_type']}/{r['target_id']} | "
                     f"{r['status']} | {ago(r['created_at'])}")
        if r["status"] == "pending":
            grid.append([(f"✅ موافقة #{r['id']}", f"{PREFIX}del:ap:{r['id']}"),
                         (f"❌ رفض #{r['id']}", f"{PREFIX}del:rj:{r['id']}")])
    pg = pager(page, pages, PREFIX + "del:pg")
    if pg:
        grid.append(pg)
    grid.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), grid)


# ---------------------------------------------------------------- backup check

def _backup_dir(ctx):
    cfg = getattr(ctx, "cfg", None)
    if cfg is None:
        return None
    base = cfg.get("base_dir") if isinstance(cfg, dict) else getattr(cfg, "base_dir", None)
    if base is None:
        return None
    return Path(str(base)) / "backups"


def _bkc(ctx, chat, mid):
    lines = ["🔐 فحص النسخ الاحتياطية", LINE,
             "هذا الفحص لا يقرأ محتوى أي نسخة ولا يفك تشفيرها ولا يطبعها أبداً — "
             "قائمة أسماء وأحجام فقط + آخر إدخالات تدقيق النسخ."]
    bdir = _backup_dir(ctx)
    if bdir is None:
        lines.append("مجلد النسخ غير مضبوط بهذا السياق (ctx.cfg بلا base_dir) — "
                     "لا يمكن إجراء فحص الملفات من قاعدة البيانات وحدها.")
    elif not bdir.is_dir():
        lines.append(f"مجلد النسخ غير موجود: {bdir}")
    else:
        entries = sorted(bdir.iterdir(), key=lambda p: p.name)
        if not entries:
            lines.append("مجلد النسخ فارغ.")
        for p in entries:
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            lines.append(f"  • {p.name} — {size} بايت (اسم وحجم فقط، المحتوى لم يُقرأ)")
    lines.append("ما يحتاجه فحص حقيقي كامل: وجود الملف + حجمه + بصمة الرأس (magic) "
                 "دون فك تشفير + إدخال تدقيق backup حديث.")
    last = ctx.db.execute(
        "SELECT id, user_id, target_type, target_id, ts FROM audit_log WHERE action='backup' "
        "ORDER BY id DESC LIMIT 5").fetchall()
    lines.append("آخر إدخالات تدقيق النسخ (action='backup'):")
    if not last:
        lines.append("  • لا إدخالات — لا يوجد سجل تدقيق نسخ بقاعدة البيانات.")
    for r in last:
        target = ((r["target_type"] or "") + ":" + (r["target_id"] or "")).strip(":") or "-"
        lines.append(f"  • #{r['id']} مستخدم {r['user_id']} هدف {target} {ago(r['ts'])}")
    _show(ctx, chat, mid, "\n".join(lines), [[("◀️ رجوع", PREFIX + "sec")]])


# ---------------------------------------------------------------- restore test (dry-run)

def _rst_screen(ctx, chat, mid):
    set_pending(ctx.db, ctx_user_id[0], "p4:rst", {})
    lines = ["🧪 اختبار استعادة احتياطي — تجربة جافة فقط (DRY-RUN)", LINE,
             "⚠️ صريح: لا يُنفَّذ أي استعادة هنا، ولا تُمس بيانات الإنتاج أبداً. "
             "ما يُكتب هو سجل إعدادات (restore_test) + صف تدقيق فقط.",
             "قائمة التحقق لاختبار حقيقي (خارج هذه الشاشة):",
             "  1. بيئة منفصلة تماماً عن الإنتاج.",
             "  2. نسخة احتياطية معروفة بالاسم/المعرّف.",
             "  3. مسار الاستعادة الطرفي للأب (app.cli restore) — لا يُستدعى هنا.",
             "  4. التحقق من سلامة البيانات المستعادة بالبيئة المنفصلة فقط.",
             "اكتب اسم ملف النسخة الاحتياطية (رسالة نصية) ثم أكّد."]
    _show(ctx, chat, mid, "\n".join(lines), [[("◀️ رجوع", PREFIX + "sec")]])


# ---------------------------------------------------------------- isolation

def _iso_screen(ctx, chat, mid):
    set_pending(ctx.db, ctx_user_id[0], "p4:iso", {})
    rows = ctx.db.execute("SELECT * FROM nb_isolated_files ORDER BY file_id").fetchall()
    lines = ["🚫 عزل ملف", LINE,
             f"الملفات المعزولة حالياً: {fmt_int(len(rows))}"]
    grid = []
    for r in rows:
        lines.append(f"  • ملف {r['file_id']} — السبب: {r['reason'] or '-'}")
        grid.append([(f"🔓 إلغاء عزل {r['file_id']}", f"{PREFIX}iso:un:{r['file_id']}")])
    lines.append("لعزل ملف: اكتب رقمه (رسالة نصية) ثم اكتب السبب ثم أكّد.")
    lines.append("ملاحظة: صفوف العزل حقيقية وقابلة للاستعلام؛ التنفيذ عند "
                 "البحث/الإدخال توصيل من الأب وليس هنا.")
    grid.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), grid)


# ---------------------------------------------------------------- settings log

def _chg_screen(ctx, chat, mid, page=0):
    _action, payload = _pending_payload(ctx, ctx_user_id[0])
    name = payload.get("setting")
    lines = ["📝 سجل تغييرات الإعدادات (قراءة فقط)", LINE,
             "للتصفية: اكتب اسم الإعداد (رسالة نصية) أو setting:<الاسم>."]
    if name:
        rows = ctx.db.execute(
            "SELECT * FROM nb_settings_log WHERE setting LIKE ? ORDER BY id DESC",
            (f"%{name}%",)).fetchall()
        lines.append(f"المرشح: {name}")
    else:
        rows = ctx.db.execute("SELECT * FROM nb_settings_log ORDER BY id DESC").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines.append(f"الإجمالي: {fmt_int(len(rows))} (صفحة {page + 1}/{pages})")
    for r in sliced:
        lines.append(f"#{r['id']} | {r['setting']} | {r['old_value']} → {r['new_value']} | "
                     f"مستخدم {r['user_id']} | {ago(r['created_at'])}")
    out = []
    pg = pager(page, pages, PREFIX + "chg:pg")
    if pg:
        out.append(pg)
    if name:
        out.append([("🗑 مسح المرشح", PREFIX + "chg:clear")])
    out.append([("◀️ رجوع", PREFIX + "sec")])
    _show(ctx, chat, mid, "\n".join(lines), out)


# ---------------------------------------------------------------- export lock

def _lck_screen(ctx, chat, mid):
    set_pending(ctx.db, ctx_user_id[0], "p4:lckreason", {})
    row = ctx.db.execute("SELECT locked, by_user, reason FROM nb_export_lock WHERE id=1").fetchone()
    locked = bool(row and row["locked"])
    lines = ["🔒 قفل التصدير مؤقتاً", LINE,
             f"الحالة الحالية: {'🔒 مقفول' if locked else '🔓 مفتوح'}"]
    if locked:
        lines.append(f"  أقفله المستخدم {row['by_user']} — السبب: {row['reason'] or '-'}")
    lines.append("اكتب سبباً (رسالة نصية، اختياري) ثم اضغط زر القفل/الفتح ثم أكّد.")
    lines.append("ملاحظة: صف القفل حقيقي؛ التنفيذ عند تقديم التصدير توصيل من الأب وليس هنا.")
    btn = [("🔓 فتح القفل", PREFIX + "lck:unlock")] if locked else [("🔒 قفل التصدير", PREFIX + "lck:lock")]
    _show(ctx, chat, mid, "\n".join(lines), [btn, [("◀️ رجوع", PREFIX + "sec")]])


# ---------------------------------------------------------------- handle
# ctx_user_id is a one-element box so the shared screen helpers (which do
# not receive `user`) can read the pending-text owner for this call.

ctx_user_id = [0]


def handle(ctx, user, chat, mid, parts) -> bool:
    if not parts or parts[0] not in _KEYS:
        return False
    ctx_user_id[0] = user.telegram_id
    ensure_nb_tables(ctx.db)
    key = parts[0]
    try:
        require(user, "manage")
    except PermissionDenied:
        _deny(ctx, user, chat, mid, "manage")
        return True

    db = ctx.db
    try:
        if key == "sec":
            _sec(ctx, chat, mid)
        elif key == "alt":
            _alt(ctx, chat, mid)
        elif key == "det":
            if len(parts) == 1:
                _det_list(ctx, chat, mid)
            else:
                _det_one(ctx, chat, mid, parts[1])
        elif key == "sch":
            if len(parts) >= 3 and parts[1] == "pg":
                _sch_screen(ctx, chat, mid, int(parts[2]) if parts[2].lstrip("-").isdigit() else 0)
            elif len(parts) >= 2 and parts[1] == "clear":
                set_pending(db, user.telegram_id, "p4:sch", {})
                _sch_screen(ctx, chat, mid, 0)
            else:
                if _pending_payload(ctx, user.telegram_id)[0] != "p4:sch":
                    set_pending(db, user.telegram_id, "p4:sch", {})
                _sch_screen(ctx, chat, mid, 0)
        elif key == "exp":
            if len(parts) >= 2 and parts[1] == "yes":
                audit_nb(db, user.telegram_id, "audit_export", "audit_log", None,
                         {"range": _pending_payload(ctx, user.telegram_id)[1]})
                _exp_page(ctx, chat, mid, 0)
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            elif len(parts) >= 3 and parts[1] == "pg":
                _exp_page(ctx, chat, mid, int(parts[2]) if parts[2].lstrip("-").isdigit() else 0)
            else:
                _exp_screen(ctx, chat, mid)
        elif key == "ret":
            if len(parts) >= 4 and parts[1] == "yes":
                _ret_apply(ctx, chat, mid, user, parts[2], parts[3])
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            elif len(parts) >= 3 and parts[1] == "set":
                set_pending(db, user.telegram_id, "p4:ret", {"kind": parts[2]})
                _show(ctx, chat, mid,
                      f"تعيين سياسة الاحتفاظ لنوع {parts[2]} — اكتب عدد الأيام "
                      f"(عدد صحيح 1..3650) برسالة نصية.",
                      [[("◀️ رجوع", PREFIX + "ret")]])
            else:
                _ret_screen(ctx, chat, mid)
        elif key == "del":
            if len(parts) >= 4 and parts[1] == "yes":
                _del_decide(ctx, chat, mid, user, parts[2], parts[3])
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            elif len(parts) >= 3 and parts[1] == "pg":
                _del_screen(ctx, chat, mid, int(parts[2]) if parts[2].lstrip("-").isdigit() else 0)
            elif len(parts) >= 3 and parts[1] == "ap":
                ctx.show(chat, mid,
                         f"تأكيد الموافقة على طلب الحذف #{parts[2]}؟\n"
                         f"⚠️ الموافقة تعلّم الطلب 'approved' فقط ولا تحذف أي بيانات إنتاج.",
                         confirm_markup(f"{PREFIX}del:yes:ap:{parts[2]}", f"{PREFIX}del:no"))
            elif len(parts) >= 3 and parts[1] == "rj":
                set_pending(db, user.telegram_id, "p4:delrej", {"id": parts[2]})
                _show(ctx, chat, mid,
                      f"رفض طلب الحذف #{parts[2]} — اكتب سبب الرفض (رسالة نصية) ثم أكّد.",
                      [[("◀️ رجوع", PREFIX + "del")]])
            else:
                _del_screen(ctx, chat, mid, 0)
        elif key == "bkc":
            _bkc(ctx, chat, mid)
        elif key == "rst":
            if len(parts) >= 2 and parts[1] == "yes":
                _rst_apply(ctx, chat, mid, user)
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            else:
                _rst_screen(ctx, chat, mid)
        elif key == "iso":
            if len(parts) >= 3 and parts[1] == "yes":
                if parts[2] == "un":
                    _iso_unapply(ctx, chat, mid, user, parts[3])
                else:
                    _iso_apply(ctx, chat, mid, user, parts[2])
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            elif len(parts) >= 3 and parts[1] == "un":
                ctx.show(chat, mid,
                         f"تأكيد إلغاء عزل الملف {parts[2]}؟",
                         confirm_markup(f"{PREFIX}iso:yes:un:{parts[2]}", f"{PREFIX}iso:no"))
            else:
                _iso_screen(ctx, chat, mid)
        elif key == "chg":
            if len(parts) >= 3 and parts[1] == "pg":
                _chg_screen(ctx, chat, mid, int(parts[2]) if parts[2].lstrip("-").isdigit() else 0)
            elif len(parts) >= 2 and parts[1] == "clear":
                set_pending(db, user.telegram_id, "p4:chg", {})
                _chg_screen(ctx, chat, mid, 0)
            else:
                if _pending_payload(ctx, user.telegram_id)[0] != "p4:chg":
                    set_pending(db, user.telegram_id, "p4:chg", {})
                _chg_screen(ctx, chat, mid, 0)
        elif key == "lck":
            if len(parts) >= 3 and parts[1] == "yes":
                _lck_apply(ctx, chat, mid, user, parts[2])
            elif len(parts) >= 2 and parts[1] == "no":
                _cancelled(ctx, chat, mid)
            elif len(parts) >= 2 and parts[1] in ("lock", "unlock"):
                verb = "قفل" if parts[1] == "lock" else "فتح قفل"
                ctx.show(chat, mid, f"تأكيد {verb} التصدير؟",
                         confirm_markup(f"{PREFIX}lck:yes:{parts[1]}", f"{PREFIX}lck:no"))
            else:
                _lck_screen(ctx, chat, mid)
    except PermissionDenied:
        _deny(ctx, user, chat, mid, "manage")
    return True


def _ret_apply(ctx, chat, mid, user, kind, days_raw):
    if kind not in ("temp", "reports", "logs"):
        _show(ctx, chat, mid, "نوع احتفاظ غير معروف — لم يُغيَّر شيء.",
              [[("◀️ رجوع", PREFIX + "ret")]])
        return
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        days = -1
    if not 1 <= days <= 3650:
        _show(ctx, chat, mid, "عدد أيام غير صالح (1..3650) — لم يُغيَّر شيء.",
              [[("◀️ رجوع", PREFIX + "ret")]])
        return
    row = ctx.db.execute("SELECT days FROM nb_retention WHERE kind=?", (kind,)).fetchone()
    old = str(row["days"]) if row else ""
    ctx.db.execute("INSERT INTO nb_retention(kind, days) VALUES (?,?) "
                   "ON CONFLICT(kind) DO UPDATE SET days=excluded.days", (kind, days))
    ctx.db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) "
                   "VALUES (?,?,?,?)", (user.telegram_id, f"retention:{kind}", old, str(days)))
    audit_nb(ctx.db, user.telegram_id, "settings", "nb_retention", kind,
             {"kind": kind, "old": old, "new": str(days)})
    pop_pending(ctx.db, user.telegram_id)
    _show(ctx, chat, mid,
          f"✅ تم تعيين سياسة الاحتفاظ لنوع {kind}: {old} → {days} يوماً.\n"
          f"التنفيذ الفعلي للحذف ليس هنا — إجراء تنظيف آمن منفصل (المرحلة 5) يطبّقها.",
          [[("◀️ رجوع", PREFIX + "ret")]])


def _del_decide(ctx, chat, mid, user, verdict, raw_id):
    try:
        rid = int(raw_id)
    except (TypeError, ValueError):
        _show(ctx, chat, mid, "طلب غير موجود — لم يُنفَّذ شيء.",
              [[("◀️ رجوع", PREFIX + "del")]])
        return
    row = ctx.db.execute("SELECT * FROM nb_del_requests WHERE id=?", (rid,)).fetchone()
    if row is None:
        _show(ctx, chat, mid, "غير موجود — لا يوجد طلب حذف بهذا الرقم.",
              [[("◀️ رجوع", PREFIX + "del")]])
        return
    if row["status"] != "pending":
        _show(ctx, chat, mid,
              f"مرفوض: تم البت بهذا الطلب مسبقاً (حالته: {row['status']}) — لا قرار ثانٍ.",
              [[("◀️ رجوع", PREFIX + "del")]])
        return
    _action, payload = _pending_payload(ctx, user.telegram_id)
    reason = payload.get("reason", "") if _action == "p4:delrej" else ""
    status = "approved" if verdict == "ap" else "rejected"
    ctx.db.execute("UPDATE nb_del_requests SET status=?, decided_by=?, decide_reason=? WHERE id=?",
                   (status, user.telegram_id, reason, rid))
    audit_nb(ctx.db, user.telegram_id,
             "del_approve" if verdict == "ap" else "del_reject",
             "nb_del_requests", rid, {"status": status, "reason": reason})
    pop_pending(ctx.db, user.telegram_id)
    note = ("⚠️ الموافقة تعلّم الطلب 'approved' فقط — لا تحذف بيانات إنتاج بنفسها. "
            "الحذف الفعلي خطوة مشغّل منفصلة وصريحة يحددها الأب."
            if verdict == "ap" else f"تم رفض الطلب. السبب: {reason or '-'}")
    _show(ctx, chat, mid, f"✅ تم {('الموافقة على' if verdict == 'ap' else 'رفض')} طلب الحذف #{rid}.\n{note}",
          [[("◀️ رجوع", PREFIX + "del")]])


def _rst_apply(ctx, chat, mid, user):
    _action, payload = _pending_payload(ctx, user.telegram_id)
    name = payload.get("name", "") if _action == "p4:rst" else ""
    ctx.db.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) "
                   "VALUES (?,?,?,?)", (user.telegram_id, "restore_test", "", f"{name}|dry-run only"))
    audit_nb(ctx.db, user.telegram_id, "restore_test", "backup", name,
             {"backup": name, "result": "dry-run only"})
    pop_pending(ctx.db, user.telegram_id)
    _show(ctx, chat, mid,
          "🧪 اختبار استعادة — تجربة جافة فقط (DRY-RUN).\n"
          f"النسخة: {name or '-'} | النتيجة المسجلة: dry-run only.\n"
          "⚠️ لم تُنفَّذ أي استعادة ولم تُمس بيانات الإنتاج أبداً. "
          "الاستعادة الحقيقية تحتاج بيئة منفصلة ومسار الأب الطرفي (app.cli restore).",
          [[("◀️ رجوع", PREFIX + "sec")]])


def _iso_apply(ctx, chat, mid, user, raw_fid):
    try:
        fid = int(raw_fid)
    except (TypeError, ValueError):
        _show(ctx, chat, mid, "رقم ملف غير صالح — لم يُعزل شيء.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return
    if ctx.db.execute("SELECT id FROM files WHERE id=?", (fid,)).fetchone() is None:
        _show(ctx, chat, mid, "الملف غير موجود — لم يُعزل شيء.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return
    if ctx.db.execute("SELECT file_id FROM nb_isolated_files WHERE file_id=?", (fid,)).fetchone():
        _show(ctx, chat, mid, "الملف معزول أصلاً — لا إجراء مكرر.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return
    _action, payload = _pending_payload(ctx, user.telegram_id)
    reason = payload.get("reason", "") if _action == "p4:isoreason" else ""
    ctx.db.execute("INSERT INTO nb_isolated_files(file_id, reason, by_user) VALUES (?,?,?)",
                   (fid, reason, user.telegram_id))
    audit_nb(ctx.db, user.telegram_id, "isolate", "file", fid, {"reason": reason})
    pop_pending(ctx.db, user.telegram_id)
    _show(ctx, chat, mid,
          f"✅ تم عزل الملف {fid}. السبب: {reason or '-'}.\n"
          f"صف العزل حقيقي وقابل للاستعلام؛ التنفيذ عند البحث/الإدخال توصيل من الأب.",
          [[("◀️ رجوع", PREFIX + "iso")]])


def _iso_unapply(ctx, chat, mid, user, raw_fid):
    try:
        fid = int(raw_fid)
    except (TypeError, ValueError):
        _show(ctx, chat, mid, "رقم ملف غير صالح — لم يُنفَّذ شيء.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return
    if ctx.db.execute("SELECT file_id FROM nb_isolated_files WHERE file_id=?", (fid,)).fetchone() is None:
        _show(ctx, chat, mid, "الملف غير معزول أصلاً — لا شيء لإلغائه.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return
    ctx.db.execute("DELETE FROM nb_isolated_files WHERE file_id=?", (fid,))
    audit_nb(ctx.db, user.telegram_id, "unisolate", "file", fid, {})
    _show(ctx, chat, mid, f"✅ تم إلغاء عزل الملف {fid}.",
          [[("◀️ رجوع", PREFIX + "iso")]])


def _lck_apply(ctx, chat, mid, user, mode):
    if mode not in ("lock", "unlock"):
        _show(ctx, chat, mid, "إجراء غير معروف — لم يُنفَّذ شيء.",
              [[("◀️ رجوع", PREFIX + "lck")]])
        return
    _action, payload = _pending_payload(ctx, user.telegram_id)
    reason = payload.get("reason", "") if _action == "p4:lckreason" else ""
    locked = 1 if mode == "lock" else 0
    ctx.db.execute("UPDATE nb_export_lock SET locked=?, by_user=?, reason=? WHERE id=1",
                   (locked, user.telegram_id, reason))
    audit_nb(ctx.db, user.telegram_id, "export_lock", "nb_export_lock", 1,
             {"locked": locked, "reason": reason})
    pop_pending(ctx.db, user.telegram_id)
    _show(ctx, chat, mid,
          f"✅ التصدير الآن: {'🔒 مقفول' if locked else '🔓 مفتوح'}.\n"
          f"التنفيذ عند تقديم التصدير توصيل من الأب وليس هنا.",
          [[("◀️ رجوع", PREFIX + "lck")]])


# ---------------------------------------------------------------- text input

def handle_text(ctx, user, chat, text) -> bool:
    ctx_user_id[0] = user.telegram_id
    ensure_nb_tables(ctx.db)
    action, payload = _pending_payload(ctx, user.telegram_id)
    if action is None or not action.startswith("p4:"):
        return False
    try:
        require(user, "manage")
    except PermissionDenied:
        _deny(ctx, user, chat, 0, "manage")
        return True
    db = ctx.db
    txt = (text or "").strip()
    if action == "p4:sch":
        m = re.match(r"^(user|action|date):(.+)$", txt)
        if not m:
            _show(ctx, chat, 0,
                  "صيغة غير صالحة — اكتب user:<رقم> أو action:<اسم> أو date:<YYYY-MM-DD>.",
                  [[("◀️ رجوع", PREFIX + "sch")]])
            return True
        kind, value = m.group(1), m.group(2).strip()
        if kind == "user":
            if not value.isdigit():
                _show(ctx, chat, 0, "رقم مستخدم غير صالح.", [[("◀️ رجوع", PREFIX + "sch")]])
                return True
            value = int(value)
        elif kind == "date":
            if not _DATE_RE.match(value):
                _show(ctx, chat, 0, "تاريخ غير صالح — الصيغة YYYY-MM-DD.",
                      [[("◀️ رجوع", PREFIX + "sch")]])
                return True
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                _show(ctx, chat, 0, "تاريخ غير صالح — الصيغة YYYY-MM-DD.",
                      [[("◀️ رجوع", PREFIX + "sch")]])
                return True
        set_pending(db, user.telegram_id, "p4:sch", {"kind": kind, "value": value})
        _sch_screen(ctx, chat, 0, 0)
        return True
    if action == "p4:exp":
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$", txt)
        if not m:
            _show(ctx, chat, 0,
                  "نطاق غير صالح — الصيغة YYYY-MM-DD..YYYY-MM-DD.",
                  [[("◀️ رجوع", PREFIX + "exp")]])
            return True
        try:
            d0, d1 = datetime.strptime(m.group(1), "%Y-%m-%d"), datetime.strptime(m.group(2), "%Y-%m-%d")
        except ValueError:
            _show(ctx, chat, 0, "نطاق غير صالح — تواريخ غير صحيحة.",
                  [[("◀️ رجوع", PREFIX + "exp")]])
            return True
        if d1 < d0:
            _show(ctx, chat, 0, "نطاق غير صالح — تاريخ النهاية قبل البداية.",
                  [[("◀️ رجوع", PREFIX + "exp")]])
            return True
        set_pending(db, user.telegram_id, "p4:exp",
                    {"from": m.group(1), "to": m.group(2)})
        _show(ctx, chat, 0,
              f"نطاق التصدير: {m.group(1)} .. {m.group(2)} — اضغط تأكيد التصدير.",
              [[("✅ تأكيد التصدير", PREFIX + "exp:yes")],
               [("◀️ رجوع", PREFIX + "exp")]])
        return True
    if action == "p4:ret":
        kind = payload.get("kind")
        try:
            days = int(txt)
        except ValueError:
            days = -1
        if not 1 <= days <= 3650:
            _show(ctx, chat, 0,
                  "عدد أيام غير صالح (1..3650) — لم يُغيَّر شيء. أعد إدخال رقم صحيح.",
                  [[("◀️ رجوع", PREFIX + "ret")]])
            return True
        ctx.show(chat, 0,
                 f"تأكيد تعيين سياسة الاحتفاظ لنوع {kind} إلى {days} يوماً؟",
                 confirm_markup(f"{PREFIX}ret:yes:{kind}:{days}", f"{PREFIX}ret:no"))
        return True
    if action == "p4:delrej":
        rid = payload.get("id")
        set_pending(db, user.telegram_id, "p4:delrej", {"id": rid, "reason": txt})
        ctx.show(chat, 0,
                 f"تأكيد رفض طلب الحذف #{rid}؟ السبب: {txt}",
                 confirm_markup(f"{PREFIX}del:yes:rj:{rid}", f"{PREFIX}del:no"))
        return True
    if action == "p4:iso":
        if not txt.isdigit():
            _show(ctx, chat, 0, "رقم ملف غير صالح — اكتب رقماً صحيحاً.",
                  [[("◀️ رجوع", PREFIX + "iso")]])
            return True
        set_pending(db, user.telegram_id, "p4:isoreason", {"file_id": txt})
        _show(ctx, chat, 0,
              f"عزل الملف {txt} — اكتب سبب العزل (رسالة نصية) ثم أكّد.",
              [[("◀️ رجوع", PREFIX + "iso")]])
        return True
    if action == "p4:isoreason":
        fid = payload.get("file_id")
        set_pending(db, user.telegram_id, "p4:isoreason", {"file_id": fid, "reason": txt})
        ctx.show(chat, 0,
                 f"تأكيد عزل الملف {fid}؟ السبب: {txt}",
                 confirm_markup(f"{PREFIX}iso:yes:{fid}", f"{PREFIX}iso:no"))
        return True
    if action == "p4:rst":
        if not txt:
            _show(ctx, chat, 0, "اسم النسخة فارغ — اكتب اسم ملف النسخة الاحتياطية.",
                  [[("◀️ رجوع", PREFIX + "rst")]])
            return True
        set_pending(db, user.telegram_id, "p4:rst", {"name": txt})
        ctx.show(chat, 0,
                 f"تأكيد تسجيل اختبار استعادة (تجربة جافة فقط) للنسخة: {txt}؟\n"
                 f"⚠️ لا استعادة حقيقية ولا مساس ببيانات الإنتاج.",
                 confirm_markup(f"{PREFIX}rst:yes", f"{PREFIX}rst:no"))
        return True
    if action == "p4:chg":
        name = txt[8:].strip() if txt.startswith("setting:") else txt
        if not name:
            _show(ctx, chat, 0, "اسم إعداد فارغ — اكتب اسماً للتصفية.",
                  [[("◀️ رجوع", PREFIX + "chg")]])
            return True
        set_pending(db, user.telegram_id, "p4:chg", {"setting": name})
        _chg_screen(ctx, chat, 0, 0)
        return True
    if action == "p4:lckreason":
        set_pending(db, user.telegram_id, "p4:lckreason", {"reason": txt})
        _lck_screen_keep(ctx, chat, 0, txt)
        return True
    return False


def _lck_screen_keep(ctx, chat, mid, reason):
    # Re-show the lock screen without wiping the stored reason: _lck_screen
    # resets pending, so render here and restore the payload afterwards.
    row = ctx.db.execute("SELECT locked FROM nb_export_lock WHERE id=1").fetchone()
    locked = bool(row and row["locked"])
    btn = [("🔓 فتح القفل", PREFIX + "lck:unlock")] if locked else [("🔒 قفل التصدير", PREFIX + "lck:lock")]
    _show(ctx, chat, mid,
          "🔒 قفل التصدير مؤقتاً\n" + LINE + "\n"
          f"الحالة الحالية: {'🔒 مقفول' if locked else '🔓 مفتوح'}\n"
          f"السبب المحفوظ: {reason}\n"
          f"اضغط زر القفل/الفتح ثم أكّد.",
          [btn, [("◀️ رجوع", PREFIX + "sec")]])
    set_pending(ctx.db, ctx_user_id[0], "p4:lckreason", {"reason": reason})
