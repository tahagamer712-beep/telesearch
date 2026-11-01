"""Phase 3 (users/permissions) of the new-buttons program: nb:p3:*

All buttons are admin-only (perm "manage"). No jobs are submitted by this
phase, so the no-duplicate-jobs rule is vacuous here.

Honest limits (stated on the screens themselves, not hidden):
- Temp-perm expiry is enforced only by filtering at check time
  (active_temp_perms); wiring it into authorize() is the parent's job.
- Session kill purges DB callback_state rows only; purging the bot's
  in-memory bot.state is parent wiring.
- Usage limits are stored + validated here only; enforcement at submit
  time is parent wiring.
- The files table has no created_by column, so per-account upload size
  is attributed via jobs (created_by) -> files (file_id) -> size_bytes,
  and the screen says so. No attribution is invented from files alone.
- The users table has no standalone "suspended" flag, so the review
  screen uses: enabled=0 (inactive), no recent jobs (dormant), and
  updated_at (falling back to created_at) older than 90 days
  (periodic review). The screen states this basis.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..auth import PERMISSIONS, PERM_AR, set_perms
from ..errors import PermissionDenied
from .nb_store import (
    audit_nb,
    ensure_nb_tables,
    page_slice,
    require,
    set_pending,
)
from .ui import LINE, ago, fmt_int, fmt_size, kb, nav, pager

P3 = "nb:p3:"

BUTTONS = [
    {"key": "grp", "label": "👥 مجموعات الصلاحيات", "perm": "manage", "confirm": True, "audit": "nb_p3_grp_assign"},
    {"key": "prv", "label": "🧾 معاينة صلاحيات المستخدم", "perm": "manage", "confirm": True, "audit": "nb_p3_prv_save"},
    {"key": "tmp", "label": "⏳ صلاحية مؤقتة", "perm": "manage", "confirm": True, "audit": "nb_p3_tmp_grant"},
    {"key": "ses", "label": "🔒 إيقاف الجلسات", "perm": "manage", "confirm": True, "audit": "nb_p3_ses_kill"},
    {"key": "use", "label": "📊 استخدام المستخدمين", "perm": "manage", "confirm": False, "audit": None},
    {"key": "lim", "label": "🚦 حدود الاستخدام", "perm": "manage", "confirm": True, "audit": "nb_p3_lim_set"},
    {"key": "req", "label": "📩 طلبات الصلاحيات", "perm": "manage", "confirm": True, "audit": "nb_p3_req_approve"},
    {"key": "acc", "label": "🧑‍⚖️ مراجعة الحسابات", "perm": "manage", "confirm": True, "audit": "nb_p3_acc_flag"},
]

_KEYS = {b["key"] for b in BUTTONS}
_PAGE = 10

LIMITS = {
    "concurrent": ("nb_limit:concurrent", "المهام المتزامنة"),
    "max_file_mb": ("nb_limit:max_file_mb", "أقصى حجم ملف (MB)"),
    "exports_per_day": ("nb_limit:exports_per_day", "التصديرات اليومية"),
}
_DURATIONS = {"1h": ("ساعة واحدة", timedelta(hours=1)),
              "1d": ("يوم واحد", timedelta(days=1)),
              "7d": ("7 أيام", timedelta(days=7)),
              "ساعة": ("ساعة واحدة", timedelta(hours=1)),
              "يوم": ("يوم واحد", timedelta(days=1)),
              "أسبوع": ("7 أيام", timedelta(days=7))}


# ---------------------------------------------------------------- helpers

def _commit(conn) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _show(ctx, chat, mid, text, markup) -> None:
    ctx.show(chat, mid, text, markup)


def _deny(ctx, user, chat, mid, what: str) -> None:
    ctx.deny(user.telegram_id, what)
    _show(ctx, chat, mid, "⛔ مرفوض: لا تملك صلاحية الإدارة المطلوبة لهذا الإجراء.",
          kb(nav()))


def _home_markup(back: str | None = None):
    return kb(nav(back))


def _perm_list(perms) -> str:
    perms = [p for p in perms if p in PERMISSIONS]
    return "، ".join(PERM_AR[p] for p in perms) if perms else "— لا صلاحيات —"


def _stored_perms(conn, uid: int) -> set:
    row = conn.execute("SELECT permissions FROM users WHERE telegram_id=?", (uid,)).fetchone()
    if row is None:
        return set()
    try:
        return {p for p in json.loads(row[0] or "[]") if p in PERMISSIONS}
    except (ValueError, TypeError):
        return set()


def _user_row(conn, uid: int):
    return conn.execute("SELECT telegram_id, display_name, role, enabled, permissions, "
                        "created_at, updated_at FROM users WHERE telegram_id=?", (uid,)).fetchone()


def _display(conn, uid: int) -> str:
    row = _user_row(conn, uid)
    if row is None:
        return f"#{uid}"
    name = row["display_name"] or ""
    return f"{name} (#{uid})" if name else f"#{uid}"


def _group_perms(conn, gid: int) -> set:
    row = conn.execute("SELECT perms FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
    if row is None:
        return set()
    try:
        return {p for p in json.loads(row[0] or "[]") if p in PERMISSIONS}
    except (ValueError, TypeError):
        return set()


def _user_group_perms(conn, uid: int) -> set:
    out = set()
    for r in conn.execute("SELECT group_id FROM nb_group_members WHERE user_id=?", (uid,)):
        out |= _group_perms(conn, r[0])
    return out


def active_temp_perms(conn, uid: int):
    """Active temp perms only: expired rows (expires_at <= now) are excluded."""
    return conn.execute(
        "SELECT * FROM nb_temp_perms WHERE user_id=? AND "
        "expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now') ORDER BY expires_at",
        (uid,)).fetchall()


def _active_temp_set(conn, uid: int) -> set:
    return {r["perm"] for r in active_temp_perms(conn, uid) if r["perm"] in PERMISSIONS}


def effective_perms(conn, uid: int, include_temp: bool = True) -> set:
    out = _stored_perms(conn, uid) | _user_group_perms(conn, uid)
    if include_temp:
        out |= _active_temp_set(conn, uid)
    return out


def _settings_log(conn, uid: int, setting: str, old: str, new: str) -> None:
    conn.execute("INSERT INTO nb_settings_log(user_id, setting, old_value, new_value) "
                 "VALUES (?,?,?,?)", (uid, setting, old, new))
    _commit(conn)


def _peek_pending(conn, uid: int):
    row = conn.execute("SELECT action, payload FROM nb_pending_text WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return None
    try:
        return row["action"], json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return row["action"], {}


def _pop_pending(conn, uid: int) -> None:
    conn.execute("DELETE FROM nb_pending_text WHERE user_id=?", (uid,))
    _commit(conn)


def _confirm_screen(ctx, chat, mid, text, key: str, back: str) -> None:
    _show(ctx, chat, mid, text,
          kb([("✅ تأكيد", f"{P3}{key}:yes"), ("❌ إلغاء", f"{P3}{key}:no")], nav(back)))


def _cancelled(ctx, chat, mid, back: str) -> None:
    _show(ctx, chat, mid, "أُلغي — لم يُنفَّذ شيء.", kb(nav(back)))


# ---------------------------------------------------------------- grp

def _grp_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT id, name, perms FROM nb_perm_groups ORDER BY id").fetchall()
    lines = ["👥 مجموعات الصلاحيات",
             "المجموعات الجاهزة + أي مجموعة ينشئها الأدمن. التعيين يضيف صلاحيات "
             "المجموعة إلى صلاحيات المستخدم (اتحاد، لا تُزال صلاحياته السابقة)."]
    sliced, pages, page = page_slice(rows, page, _PAGE)
    for g in sliced:
        try:
            perms = {p for p in json.loads(g["perms"] or "[]") if p in PERMISSIONS}
        except (ValueError, TypeError):
            perms = set()
        cnt = conn.execute("SELECT COUNT(*) FROM nb_group_members WHERE group_id=?", (g["id"],)).fetchone()[0]
        lines.append(f"{LINE}\n#{g['id']} {g['name']} — {cnt} عضو\n{_perm_list(sorted(perms))}")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    buttons = [[(f"#{g['id']} {g['name']}", f"{P3}grp:g:{g['id']}")] for g in sliced]
    buttons.append([("➕ مجموعة جديدة", f"{P3}grp:new")])
    prow = pager(page, pages, f"{P3}grp")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


def _grp_detail(ctx, chat, mid, gid: int) -> None:
    conn = ctx.db
    g = conn.execute("SELECT id, name, perms FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
    if g is None:
        _show(ctx, chat, mid, "المجموعة غير موجودة.", _home_markup(f"{P3}grp"))
        return
    try:
        perms = {p for p in json.loads(g["perms"] or "[]") if p in PERMISSIONS}
    except (ValueError, TypeError):
        perms = set()
    members = conn.execute("SELECT user_id FROM nb_group_members WHERE group_id=? ORDER BY user_id", (gid,)).fetchall()
    lines = [f"👥 مجموعة #{g['id']} {g['name']}", _perm_list(sorted(perms)), LINE, "الأعضاء:"]
    buttons = []
    for m in members:
        lines.append(f"• {_display(conn, m['user_id'])}")
        buttons.append([(f"❌ إزالة {_display(conn, m['user_id'])}", f"{P3}grp:un:{gid}:{m['user_id']}")])
    if not members:
        lines.append("— لا أعضاء —")
    buttons.append([("➕ تعيين مستخدم", f"{P3}grp:assign:{gid}")])
    buttons.append(nav(f"{P3}grp"))
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


def _grp_yes(ctx, user, chat, mid, payload: dict) -> None:
    conn = ctx.db
    action = payload.get("_action")
    if action == "grp_new":
        name, perms = payload.get("name", ""), payload.get("perms", [])
        if conn.execute("SELECT 1 FROM nb_perm_groups WHERE name=?", (name,)).fetchone():
            _show(ctx, chat, mid, "المجموعة موجودة أصلًا — لم يُنشأ شيء.", _home_markup(f"{P3}grp"))
            return
        conn.execute("INSERT INTO nb_perm_groups(name, perms) VALUES (?,?)", (name, json.dumps(perms)))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_grp_create", "nb_perm_group", name,
                 {"perms": perms})
        _show(ctx, chat, mid, f"✅ أُنشئت المجموعة «{name}» بصلاحيات: {_perm_list(perms)}",
              _home_markup(f"{P3}grp"))
    elif action == "grp_assign":
        gid, uid = payload.get("gid"), payload.get("uid")
        g = conn.execute("SELECT name FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
        if g is None or _user_row(conn, uid) is None:
            _show(ctx, chat, mid, "المجموعة أو المستخدم غير موجود — لم يُنفَّذ شيء.",
                  _home_markup(f"{P3}grp"))
            return
        perms = _group_perms(conn, gid)
        conn.execute("INSERT OR IGNORE INTO nb_group_members(group_id, user_id) VALUES (?,?)", (gid, uid))
        before = _stored_perms(conn, uid)
        new = sorted(before | perms)
        if _user_row(conn, uid)["role"] == "user":
            set_perms(conn, uid, new)
        # Snapshot pre-assign perms so a later unassign can restore them
        # exactly (stored perms alone cannot tell group perms apart from the
        # user's own identical perms).
        _settings_log(conn, user.telegram_id, f"grp_before:{gid}:{uid}",
                      json.dumps(sorted(before)), json.dumps(new))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_grp_assign", "user", uid,
                 {"group_id": gid, "group": g["name"], "perms": new})
        _show(ctx, chat, mid,
              f"✅ عُيّنت مجموعة «{g['name']}» للمستخدم {_display(conn, uid)}.\n"
              f"صلاحياته الآن: {_perm_list(new)} (اتحاد صلاحياته السابقة + المجموعة).",
              _home_markup(f"{P3}grp:g:{gid}"))
    elif action == "grp_un":
        gid, uid = payload.get("gid"), payload.get("uid")
        g = conn.execute("SELECT name FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
        if g is None:
            _show(ctx, chat, mid, "المجموعة غير موجودة.", _home_markup(f"{P3}grp"))
            return
        conn.execute("DELETE FROM nb_group_members WHERE group_id=? AND user_id=?", (gid, uid))
        # Revert: restore the pre-assign snapshot if one exists (exact);
        # otherwise stored minus this group's perms, plus remaining groups'.
        snap = conn.execute("SELECT old_value FROM nb_settings_log WHERE setting=? "
                            "ORDER BY id DESC LIMIT 1", (f"grp_before:{gid}:{uid}",)).fetchone()
        if snap is not None:
            try:
                base = {p for p in json.loads(snap[0] or "[]") if p in PERMISSIONS}
            except (ValueError, TypeError):
                base = set()
        else:
            base = _stored_perms(conn, uid) - _group_perms(conn, gid)
        new = sorted(base | _user_group_perms(conn, uid))
        if _user_row(conn, uid) is not None and _user_row(conn, uid)["role"] == "user":
            set_perms(conn, uid, new)
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_grp_unassign", "user", uid,
                 {"group_id": gid, "group": g["name"], "perms": new})
        _show(ctx, chat, mid,
              f"✅ أُزيلت عضوية {_display(conn, uid)} من «{g['name']}».\n"
              f"سلوك الإزالة المؤكد: أُزيلت العضوية، وأُعيدت users.permissions إلى "
              f"ما كانت عليه قبل التعيين (لقطة التعيين المحفوظة؛ وإن لم توجد لقطة "
              f"تُحذف صلاحيات هذه المجموعة فقط مع الاحتفاظ بصلاحيات مجموعاته "
              f"الأخرى): {_perm_list(new)}.",
              _home_markup(f"{P3}grp:g:{gid}"))
    else:
        _show(ctx, chat, mid, "لا يوجد إجراء معلق.", _home_markup(f"{P3}grp"))


# ---------------------------------------------------------------- prv

def _prv_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT telegram_id, display_name, role, enabled FROM users ORDER BY telegram_id").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = ["🧾 معاينة صلاحيات المستخدم",
             "اختر مستخدمًا لمعاينة صلاحياته الفعلية قبل أي حفظ. المعاينة للقراءة "
             "فقط ولا تحفظ شيئًا بنفسها."]
    buttons = []
    for r in sliced:
        lines.append(f"• {_display(conn, r['telegram_id'])} — {r['role']} — {'مفعّل' if r['enabled'] else 'غير مفعّل'}")
        buttons.append([(f"🧾 {_display(conn, r['telegram_id'])}", f"{P3}prv:u:{r['telegram_id']}")])
    lines.append(f"\nصفحة {page + 1} من {pages}")
    prow = pager(page, pages, f"{P3}prv")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


def _prv_detail(ctx, chat, mid, uid: int) -> None:
    conn = ctx.db
    if _user_row(conn, uid) is None:
        _show(ctx, chat, mid, "المستخدم غير موجود.", _home_markup(f"{P3}prv"))
        return
    stored = _stored_perms(conn, uid)
    group = _user_group_perms(conn, uid)
    temp = _active_temp_set(conn, uid)
    eff = stored | group | temp
    added = sorted(group - stored)  # what a save would add (group perms only)
    lines = [f"🧾 معاينة: {_display(conn, uid)}",
             f"الصلاحيات الحالية (users.permissions): {_perm_list(sorted(stored))}",
             f"صلاحيات المجموعات: {_perm_list(sorted(group))}",
             f"صلاحيات مؤقتة سارية: {_perm_list(sorted(temp))} (لا تُثبَّت بالحفظ — تنتهي بانتهائها)",
             f"الصلاحيات الفعلية الكاملة: {_perm_list(sorted(eff))}",
             LINE,
             f"ما سيضيفه الحفظ: {_perm_list(added) if added else 'لا شيء جديد'}",
             "ما سيُزال بالحفظ: لا شيء (الحفظ يضيف فقط ولا يزيل).",
             "المعاينة للقراءة فقط؛ الحفظ زر مستقل بتأكيد وتدقيق."]
    buttons = [[("💾 حفظ صلاحيات المجموعات", f"{P3}prv:save:{uid}")], nav(f"{P3}prv")]
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- tmp

def _tmp_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute(
        "SELECT * FROM nb_temp_perms WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "ORDER BY expires_at").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = ["⏳ الصلاحيات المؤقتة السارية",
             "الصفوف المنتهية لا تظهر هنا ولا تُحتسب (يُتجاهلها active_temp_perms عند الفحص)."]
    buttons = []
    for r in sliced:
        lines.append(f"• #{r['id']} {_display(conn, r['user_id'])} — {PERM_AR.get(r['perm'], r['perm'])} — تنتهي {ago(r['expires_at'])}")
        buttons.append([(f"❌ إلغاء #{r['id']}", f"{P3}tmp:rev:{r['id']}")])
    if not sliced:
        lines.append("— لا صلاحيات مؤقتة سارية —")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    buttons.append([("➕ منح صلاحية مؤقتة", f"{P3}tmp:grant")])
    prow = pager(page, pages, f"{P3}tmp")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- ses

def _ses_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT user_id, COUNT(*) AS c FROM callback_state GROUP BY user_id ORDER BY c DESC").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = ["🔒 الجلسات الحية (callback_state)",
             "الإيقاف يحذف صفوف callback_state من قاعدة البيانات ويسجّل في التدقيق "
             "وnb_settings_log. مسح حالة الذاكرة داخل البوت (bot.state) ليس من هذا "
             "الموديول — توصيل من الأب."]
    buttons = []
    for r in sliced:
        lines.append(f"• {_display(conn, r['user_id'])} — {fmt_int(r['c'])} صف جلسة")
        buttons.append([(f"🔒 إيقاف جلسات {_display(conn, r['user_id'])}", f"{P3}ses:kill:{r['user_id']}")])
    if not sliced:
        lines.append("— لا صفوف جلسات معلقة —")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    prow = pager(page, pages, f"{P3}ses")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- use

def _use_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT telegram_id, display_name, role, enabled FROM users ORDER BY telegram_id").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = ["📊 استخدام المستخدمين (إحصاءات فقط — بلا محتوى شخصي وبلا قيم سجلات)"]
    for r in sliced:
        uid = r["telegram_id"]
        jobs = conn.execute("SELECT status, COUNT(*) AS c FROM jobs WHERE created_by=? GROUP BY status", (uid,)).fetchall()
        job_txt = "، ".join(f"{j['status']}: {fmt_int(j['c'])}" for j in jobs) or "لا مهام"
        up = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM files WHERE id IN "
                          "(SELECT file_id FROM jobs WHERE created_by=? AND file_id IS NOT NULL)", (uid,)).fetchone()[0]
        rep = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(result_count),0) AS s FROM reports WHERE user_id=?", (uid,)).fetchone()
        lines.append(f"{LINE}\n{_display(conn, uid)} — {r['role']} — {'مفعّل' if r['enabled'] else 'غير مفعّل'}"
                     f"\nمهام: {job_txt}"
                     f"\nحجم الرفع (ملفات مرتبطة بمهامه): {fmt_size(up)}"
                     f"\nتصديرات: {fmt_int(rep['c'])} تقرير — مجموع نتائج {fmt_int(rep['s'])}")
    lines.append("\nملاحظة: جدول files بلا عمود created_by، لذا يُحسب حجم الرفع من ملفات "
                 "مرتبطة بمهام المستخدم فقط، لا من files مباشرة.")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    prow = pager(page, pages, f"{P3}use")
    buttons = ([prow] if prow else []) + [nav()]
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- lim

def _lim_show(ctx, chat, mid) -> None:
    conn = ctx.db
    lines = ["🚦 حدود الاستخدام",
             "القيم المخزنة هنا فقط؛ التنفيذ عند الإرسال توصيل من الأب، لا ينفذه "
             "هذا الموديول."]
    buttons = []
    for short, (key, label) in LIMITS.items():
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        val = row[0] if row else "غير مضبوط"
        lines.append(f"• {label} ({key}): {val}")
        buttons.append([(f"✏️ {label}", f"{P3}lim:set:{short}")])
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- req

def _req_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT * FROM nb_perm_requests ORDER BY "
                        "CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC").fetchall()
    sliced, pages, page = page_slice(rows, page, _PAGE)
    lines = ["📩 طلبات الصلاحيات (المعلقة أولًا)"]
    buttons = []
    for r in sliced:
        lines.append(f"{LINE}\n#{r['id']} {_display(conn, r['user_id'])} — {PERM_AR.get(r['perm'], r['perm'])} — {r['status']}"
                     + (f"\nسبب الطلب: {r['reason']}" if r["reason"] else ""))
        if r["status"] == "pending":
            buttons.append([("✅ موافقة", f"{P3}req:ok:{r['id']}"), ("❌ رفض", f"{P3}req:rej:{r['id']}")])
    if not sliced:
        lines.append("— لا طلبات —")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    prow = pager(page, pages, f"{P3}req")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- acc

def _acc_list(ctx, chat, mid, page: int) -> None:
    conn = ctx.db
    rows = conn.execute("SELECT telegram_id, display_name, role, enabled, created_at, updated_at "
                        "FROM users ORDER BY telegram_id").fetchall()
    flagged = []
    for r in rows:
        reasons = []
        if not r["enabled"]:
            reasons.append("غير مفعّل (enabled=0)")
        recent = conn.execute("SELECT 1 FROM jobs WHERE created_by=? AND created_at > "
                              "strftime('%Y-%m-%dT%H:%M:%fZ','now','-90 days') LIMIT 1",
                              (r["telegram_id"],)).fetchone()
        if recent is None:
            reasons.append("بلا مهام حديثة (آخر 90 يومًا)")
        stale = conn.execute("SELECT 1 WHERE COALESCE(?, ?) < strftime('%Y-%m-%dT%H:%M:%fZ','now','-90 days')",
                             (r["updated_at"], r["created_at"])).fetchone()
        if stale is not None:
            reasons.append("آخر تحديث أقدم من 90 يومًا (مراجعة دورية)")
        if reasons:
            flagged.append((r, reasons))
    sliced, pages, page = page_slice(flagged, page, _PAGE)
    lines = ["🧑‍⚖️ حسابات تحتاج مراجعة",
             "الأساس بصراحة: لا يوجد حقل 'موقوف' مستقل في جدول users؛ المراجعة تشمل "
             "الحساب غير المفعّل، والحساب بلا مهام حديثة، والحساب الذي لم يُحدَّث منذ "
             "أكثر من 90 يومًا (updated_at، أو created_at إن كان updated_at فارغًا). "
             "التعليم للمراجعة لا يغيّر الحساب نفسه."]
    buttons = []
    for r, reasons in sliced:
        lines.append(f"• {_display(conn, r['telegram_id'])} — {'؛ '.join(reasons)}")
        buttons.append([(f"🚩 تعليم للمراجعة {_display(conn, r['telegram_id'])}", f"{P3}acc:flag:{r['telegram_id']}")])
    if not sliced:
        lines.append("— لا حسابات تحتاج مراجعة —")
    lines.append(f"\nصفحة {page + 1} من {pages}")
    prow = pager(page, pages, f"{P3}acc")
    if prow:
        buttons.append(prow)
    buttons.append(nav())
    _show(ctx, chat, mid, "\n".join(lines), kb(*buttons))


# ---------------------------------------------------------------- confirm dispatch

def _do_yes(ctx, user, chat, mid, key: str) -> None:
    conn = ctx.db
    peek = _peek_pending(conn, user.telegram_id)
    if peek is None:
        _show(ctx, chat, mid, "لا يوجد إجراء معلق.", _home_markup())
        return
    _action, payload = peek
    _pop_pending(conn, user.telegram_id)
    if key == "grp":
        _grp_yes(ctx, user, chat, mid, payload)
    elif key == "prv" and payload.get("_action") == "prv_save":
        uid = payload.get("uid")
        if _user_row(conn, uid) is None:
            _show(ctx, chat, mid, "المستخدم غير موجود.", _home_markup(f"{P3}prv"))
            return
        new = sorted(_stored_perms(conn, uid) | _user_group_perms(conn, uid))
        if _user_row(conn, uid)["role"] == "user":
            set_perms(conn, uid, new)
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_prv_save", "user", uid, {"perms": new})
        _show(ctx, chat, mid,
              f"✅ حُفظت صلاحيات المجموعات للمستخدم {_display(conn, uid)}: {_perm_list(new)}.\n"
              "الصلاحيات المؤقتة لم تُثبَّت (تنتهي بانتهائها).",
              _home_markup(f"{P3}prv:u:{uid}"))
    elif key == "tmp" and payload.get("_action") == "tmp_grant":
        uid, perm, dur = payload.get("uid"), payload.get("perm"), payload.get("dur")
        label, delta = _DURATIONS[dur]
        exp = (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%f") + "Z"
        conn.execute("INSERT INTO nb_temp_perms(user_id, perm, expires_at, granted_by) VALUES (?,?,?,?)",
                     (uid, perm, exp, user.telegram_id))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_tmp_grant", "user", uid, {"perm": perm, "expires_at": exp})
        _show(ctx, chat, mid,
              f"✅ مُنحت {PERM_AR.get(perm, perm)} للمستخدم {_display(conn, uid)} لمدة {label} (تنتهي {ago(exp)}).",
              _home_markup(f"{P3}tmp"))
    elif key == "tmp" and payload.get("_action") == "tmp_rev":
        rid = payload.get("id")
        row = conn.execute("SELECT user_id, perm FROM nb_temp_perms WHERE id=?", (rid,)).fetchone()
        if row is None:
            _show(ctx, chat, mid, "الصلاحية المؤقتة غير موجودة.", _home_markup(f"{P3}tmp"))
            return
        conn.execute("DELETE FROM nb_temp_perms WHERE id=?", (rid,))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_tmp_revoke", "user", row["user_id"], {"perm": row["perm"]})
        _show(ctx, chat, mid, f"✅ أُلغيت الصلاحية المؤقتة #{rid} مبكرًا.", _home_markup(f"{P3}tmp"))
    elif key == "ses" and payload.get("_action") == "ses_kill":
        uid = payload.get("uid")
        cnt = conn.execute("SELECT COUNT(*) FROM callback_state WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute("DELETE FROM callback_state WHERE user_id=?", (uid,))
        _commit(conn)
        _settings_log(conn, user.telegram_id, "sessions_killed", str(cnt), "0")
        audit_nb(conn, user.telegram_id, "nb_p3_ses_kill", "user", uid, {"rows_deleted": cnt})
        _show(ctx, chat, mid,
              f"✅ أُوقفت جلسات {_display(conn, uid)}: حُذف {fmt_int(cnt)} صف من callback_state.\n"
              "مسح حالة الذاكرة داخل البوت (bot.state) توصيل من الأب، لا ينفذه هذا الموديول.",
              _home_markup(f"{P3}ses"))
    elif key == "lim" and payload.get("_action") == "lim_set":
        short, val = payload.get("short"), payload.get("value")
        full, label = LIMITS[short]
        old_row = conn.execute("SELECT value FROM settings WHERE key=?", (full,)).fetchone()
        old = old_row[0] if old_row else ""
        conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (full, str(val)))
        _commit(conn)
        _settings_log(conn, user.telegram_id, full, old, str(val))
        audit_nb(conn, user.telegram_id, "nb_p3_lim_set", "setting", full, {"old": old, "new": str(val)})
        _show(ctx, chat, mid, f"✅ حُدّث {label} ({full}) من «{old or 'غير مضبوط'}» إلى «{val}».",
              _home_markup(f"{P3}lim"))
    elif key == "req" and payload.get("_action") == "req_ok":
        rid = payload.get("id")
        row = conn.execute("SELECT * FROM nb_perm_requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            _show(ctx, chat, mid, "الطلب غير موجود.", _home_markup(f"{P3}req"))
            return
        if row["status"] != "pending":
            _show(ctx, chat, mid,
                  f"مرفوض: الطلب #{rid} سبق اتخاذ قرار بشأنه ({row['status']}) — لا تطبيق مزدوج.",
                  _home_markup(f"{P3}req"))
            return
        urow = _user_row(conn, row["user_id"])
        if urow is None or urow["role"] != "user":
            _show(ctx, chat, mid, "المستخدم المستهدف غير موجود كحساب user — لم يُتخذ قرار.",
                  _home_markup(f"{P3}req"))
            return
        new = sorted(_stored_perms(conn, row["user_id"]) | {row["perm"]})
        set_perms(conn, row["user_id"], new)
        conn.execute("UPDATE nb_perm_requests SET status='approved', decided_by=? WHERE id=?",
                     (user.telegram_id, rid))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_req_approve", "user", row["user_id"],
                 {"perm": row["perm"], "request_id": rid})
        _show(ctx, chat, mid,
              f"✅ وُوفق على الطلب #{rid}: أُضيفت {PERM_AR.get(row['perm'], row['perm'])} "
              f"إلى {_display(conn, row['user_id'])}.",
              _home_markup(f"{P3}req"))
    elif key == "req" and payload.get("_action") == "req_rej":
        rid, reason = payload.get("id"), payload.get("reason", "")
        row = conn.execute("SELECT * FROM nb_perm_requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            _show(ctx, chat, mid, "الطلب غير موجود.", _home_markup(f"{P3}req"))
            return
        if row["status"] != "pending":
            _show(ctx, chat, mid,
                  f"مرفوض: الطلب #{rid} سبق اتخاذ قرار بشأنه ({row['status']}) — لا قرار مزدوج.",
                  _home_markup(f"{P3}req"))
            return
        conn.execute("UPDATE nb_perm_requests SET status='rejected', decided_by=?, decide_reason=? WHERE id=?",
                     (user.telegram_id, reason, rid))
        _commit(conn)
        audit_nb(conn, user.telegram_id, "nb_p3_req_reject", "user", row["user_id"],
                 {"request_id": rid, "reason": reason})
        _show(ctx, chat, mid, f"✅ رُفض الطلب #{rid} بالسبب: {reason}", _home_markup(f"{P3}req"))
    elif key == "acc" and payload.get("_action") == "acc_flag":
        uid = payload.get("uid")
        _settings_log(conn, user.telegram_id, "account_review_flag", "", f"flagged:{uid}")
        audit_nb(conn, user.telegram_id, "nb_p3_acc_flag", "user", uid, {})
        _show(ctx, chat, mid,
              f"✅ عُلّم {_display(conn, uid)} للمراجعة (سُجّل في nb_settings_log والتدقيق فقط — "
              "لم يتغير الحساب نفسه).",
              _home_markup(f"{P3}acc"))
    else:
        _show(ctx, chat, mid, "لا يوجد إجراء معلق مطابق.", _home_markup())


# ---------------------------------------------------------------- handle

def handle(ctx, user, chat, mid, parts) -> bool:
    ensure_nb_tables(ctx.db)
    if not parts or parts[0] not in _KEYS:
        return False
    key = parts[0]
    try:
        require(user, "manage")
    except PermissionDenied:
        _deny(ctx, user, chat, mid, key)
        return True
    conn = ctx.db
    rest = parts[1:]
    try:
        if rest and rest[0] == "yes":
            _do_yes(ctx, user, chat, mid, key)
            return True
        if rest and rest[0] == "no":
            _pop_pending(conn, user.telegram_id)
            _cancelled(ctx, chat, mid, f"{P3}{key}")
            return True
        if key == "grp":
            if not rest or rest[0].isdigit():
                _grp_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "g" and len(rest) > 1 and rest[1].isdigit():
                _grp_detail(ctx, chat, mid, int(rest[1]))
            elif rest[0] == "new":
                set_pending(conn, user.telegram_id, "p3_grp_new", {})
                _show(ctx, chat, mid,
                      "أرسل اسم المجموعة والصلاحيات بالصيغة:\nالاسم | perm1,perm2\n"
                      f"الصلاحيات المتاحة: {', '.join(PERMISSIONS)}",
                      _home_markup(f"{P3}grp"))
            elif rest[0] == "assign" and len(rest) > 1 and rest[1].isdigit():
                set_pending(conn, user.telegram_id, "p3_grp_assign", {"gid": int(rest[1])})
                _show(ctx, chat, mid, "أرسل رقم (telegram_id) المستخدم المراد تعيينه للمجموعة.",
                      _home_markup(f"{P3}grp:g:{rest[1]}"))
            elif rest[0] == "un" and len(rest) > 2 and rest[1].isdigit() and rest[2].isdigit():
                gid, uid = int(rest[1]), int(rest[2])
                g = conn.execute("SELECT name FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
                if g is None:
                    _show(ctx, chat, mid, "المجموعة غير موجودة.", _home_markup(f"{P3}grp"))
                else:
                    set_pending(conn, user.telegram_id, "p3_grp_un", {"_action": "grp_un", "gid": gid, "uid": uid})
                    _confirm_screen(ctx, chat, mid,
                        f"إزالة عضوية {_display(conn, uid)} من «{g['name']}»؟\n"
                        "عند التأكيد: تُزال العضوية، وتُعاد users.permissions إلى ما كانت "
                        "عليه قبل التعيين (لقطة التعيين المحفوظة؛ وإن لم توجد لقطة تُحذف "
                        "صلاحيات هذه المجموعة فقط مع الاحتفاظ بصلاحيات مجموعاته الأخرى).",
                        "grp", f"{P3}grp:g:{gid}")
            else:
                _grp_list(ctx, chat, mid, 0)
        elif key == "prv":
            if not rest or rest[0].isdigit():
                _prv_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "u" and len(rest) > 1 and rest[1].isdigit():
                _prv_detail(ctx, chat, mid, int(rest[1]))
            elif rest[0] == "save" and len(rest) > 1 and rest[1].isdigit():
                uid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_prv_save", {"_action": "prv_save", "uid": uid})
                _confirm_screen(ctx, chat, mid,
                    f"حفظ صلاحيات المجموعات للمستخدم {_display(conn, uid)} في users.permissions؟\n"
                    "الصلاحيات المؤقتة لا تُثبَّت. المعاينة نفسها لم تحفظ شيئًا.",
                    "prv", f"{P3}prv:u:{uid}")
            else:
                _prv_list(ctx, chat, mid, 0)
        elif key == "tmp":
            if not rest or rest[0].isdigit():
                _tmp_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "grant":
                set_pending(conn, user.telegram_id, "p3_tmp_grant", {})
                _show(ctx, chat, mid,
                      "أرسل المنح بالصيغة:\nuid:perm:مدة\nالمدة: 1h (ساعة) أو 1d (يوم) أو 7d (أسبوع).\n"
                      f"الصلاحيات المتاحة: {', '.join(PERMISSIONS)}",
                      _home_markup(f"{P3}tmp"))
            elif rest[0] == "rev" and len(rest) > 1 and rest[1].isdigit():
                rid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_tmp_rev", {"_action": "tmp_rev", "id": rid})
                _confirm_screen(ctx, chat, mid, f"إلغاء الصلاحية المؤقتة #{rid} مبكرًا؟",
                                "tmp", f"{P3}tmp")
            else:
                _tmp_list(ctx, chat, mid, 0)
        elif key == "ses":
            if not rest or rest[0].isdigit():
                _ses_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "kill" and len(rest) > 1 and rest[1].isdigit():
                uid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_ses_kill", {"_action": "ses_kill", "uid": uid})
                _confirm_screen(ctx, chat, mid,
                    f"إيقاف جلسات {_display(conn, uid)}؟ سيُحذف كل صف callback_state له من "
                    "قاعدة البيانات ويُسجَّل الإجراء. مسح حالة الذاكرة داخل البوت توصيل "
                    "من الأب.",
                    "ses", f"{P3}ses")
            else:
                _ses_list(ctx, chat, mid, 0)
        elif key == "use":
            _use_list(ctx, chat, mid, int(rest[0]) if rest and rest[0].isdigit() else 0)
        elif key == "lim":
            if rest and rest[0] == "set" and len(rest) > 1 and rest[1] in LIMITS:
                short = rest[1]
                set_pending(conn, user.telegram_id, "p3_lim_set", {"short": short})
                _show(ctx, chat, mid,
                      f"أرسل قيمة جديدة لـ {LIMITS[short][1]} ({LIMITS[short][0]}): عدد صحيح أكبر من 0.",
                      _home_markup(f"{P3}lim"))
            else:
                _lim_show(ctx, chat, mid)
        elif key == "req":
            if not rest or rest[0].isdigit():
                _req_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "ok" and len(rest) > 1 and rest[1].isdigit():
                rid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_req_ok", {"_action": "req_ok", "id": rid})
                _confirm_screen(ctx, chat, mid, f"الموافقة على الطلب #{rid} ومنح صلاحيته للمستخدم؟",
                                "req", f"{P3}req")
            elif rest[0] == "rej" and len(rest) > 1 and rest[1].isdigit():
                rid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_req_rej", {"id": rid})
                _show(ctx, chat, mid, f"أرسل سبب رفض الطلب #{rid}.", _home_markup(f"{P3}req"))
            else:
                _req_list(ctx, chat, mid, 0)
        elif key == "acc":
            if not rest or rest[0].isdigit():
                _acc_list(ctx, chat, mid, int(rest[0]) if rest else 0)
            elif rest[0] == "flag" and len(rest) > 1 and rest[1].isdigit():
                uid = int(rest[1])
                set_pending(conn, user.telegram_id, "p3_acc_flag", {"_action": "acc_flag", "uid": uid})
                _confirm_screen(ctx, chat, mid,
                    f"تعليم {_display(conn, uid)} للمراجعة؟ هذا لا يغيّر الحساب نفسه — "
                    "يُسجَّل فقط في nb_settings_log والتدقيق.",
                    "acc", f"{P3}acc")
            else:
                _acc_list(ctx, chat, mid, 0)
    except PermissionDenied:
        _deny(ctx, user, chat, mid, key)
    return True


def handle_text(ctx, user, chat, text) -> bool:
    ensure_nb_tables(ctx.db)
    peek = _peek_pending(ctx.db, user.telegram_id)
    if peek is None:
        return False
    action, payload = peek
    if action not in ("p3_grp_new", "p3_grp_assign", "p3_tmp_grant", "p3_lim_set", "p3_req_rej"):
        return False
    try:
        require(user, "manage")
    except PermissionDenied:
        _deny(ctx, user, chat, 0, action)
        return True
    conn = ctx.db
    text = (text or "").strip()
    if action == "p3_grp_new":
        _pop_pending(conn, user.telegram_id)
        if "|" not in text:
            set_pending(conn, user.telegram_id, "p3_grp_new", {})
            _show(ctx, chat, 0, "صيغة خاطئة — أرسل: الاسم | perm1,perm2 (لم يُنشأ شيء).",
                  _home_markup(f"{P3}grp"))
            return True
        name, raw = text.split("|", 1)
        name = name.strip()
        perms = [p.strip() for p in raw.split(",") if p.strip()]
        bad = [p for p in perms if p not in PERMISSIONS]
        if not name or not perms or bad:
            set_pending(conn, user.telegram_id, "p3_grp_new", {})
            _show(ctx, chat, 0,
                  f"إدخال مرفوض (اسم فارغ أو صلاحية غير معروفة: {', '.join(bad) or '—'}) — لم يُنشأ شيء.",
                  _home_markup(f"{P3}grp"))
            return True
        set_pending(conn, user.telegram_id, "p3_grp_new_confirm",
                    {"_action": "grp_new", "name": name, "perms": perms})
        _confirm_screen(ctx, chat, 0, f"إنشاء مجموعة «{name}» بصلاحيات: {_perm_list(perms)}؟",
                        "grp", f"{P3}grp")
        return True
    if action == "p3_grp_assign":
        _pop_pending(conn, user.telegram_id)
        gid = payload.get("gid")
        if not text.isdigit() or _user_row(conn, int(text)) is None:
            _show(ctx, chat, 0, "رقم مستخدم غير صالح أو غير موجود — لم يُنفَّذ شيء.",
                  _home_markup(f"{P3}grp:g:{gid}"))
            return True
        uid = int(text)
        g = conn.execute("SELECT name FROM nb_perm_groups WHERE id=?", (gid,)).fetchone()
        set_pending(conn, user.telegram_id, "p3_grp_assign_confirm",
                    {"_action": "grp_assign", "gid": gid, "uid": uid})
        _confirm_screen(ctx, chat, 0,
            f"تعيين مجموعة «{g['name'] if g else gid}» للمستخدم {_display(conn, uid)}؟\n"
            "سيُضاف اتحاد صلاحيات المجموعة إلى صلاحياته الحالية.",
            "grp", f"{P3}grp:g:{gid}")
        return True
    if action == "p3_tmp_grant":
        _pop_pending(conn, user.telegram_id)
        parts = [p.strip() for p in text.split(":")]
        if len(parts) != 3 or not parts[0].isdigit() or parts[1] not in PERMISSIONS or parts[2] not in _DURATIONS \
                or _user_row(conn, int(parts[0])) is None:
            set_pending(conn, user.telegram_id, "p3_tmp_grant", {})
            _show(ctx, chat, 0,
                  "إدخال مرفوض — الصيغة: uid:perm:مدة (المدة 1h أو 1d أو 7d، والصلاحية من القائمة). لم يُمنح شيء.",
                  _home_markup(f"{P3}tmp"))
            return True
        uid, perm, dur = int(parts[0]), parts[1], parts[2]
        set_pending(conn, user.telegram_id, "p3_tmp_grant_confirm",
                    {"_action": "tmp_grant", "uid": uid, "perm": perm, "dur": dur})
        _confirm_screen(ctx, chat, 0,
            f"منح {PERM_AR.get(perm, perm)} للمستخدم {_display(conn, uid)} لمدة {_DURATIONS[dur][0]}؟",
            "tmp", f"{P3}tmp")
        return True
    if action == "p3_lim_set":
        _pop_pending(conn, user.telegram_id)
        short = payload.get("short")
        if short not in LIMITS or not text.isdigit() or int(text) <= 0:
            set_pending(conn, user.telegram_id, "p3_lim_set", {"short": short})
            _show(ctx, chat, 0, "قيمة مرفوضة: يجب أن تكون عددًا صحيحًا أكبر من 0 — لم يُكتب شيء.",
                  _home_markup(f"{P3}lim"))
            return True
        set_pending(conn, user.telegram_id, "p3_lim_set_confirm",
                    {"_action": "lim_set", "short": short, "value": int(text)})
        _confirm_screen(ctx, chat, 0,
            f"تغيير {LIMITS[short][1]} ({LIMITS[short][0]}) إلى «{int(text)}»؟",
            "lim", f"{P3}lim")
        return True
    if action == "p3_req_rej":
        _pop_pending(conn, user.telegram_id)
        rid = payload.get("id")
        if not text:
            set_pending(conn, user.telegram_id, "p3_req_rej", {"id": rid})
            _show(ctx, chat, 0, "سبب الرفض فارغ — أرسل سببًا.", _home_markup(f"{P3}req"))
            return True
        set_pending(conn, user.telegram_id, "p3_req_rej_confirm",
                    {"_action": "req_rej", "id": rid, "reason": text})
        _confirm_screen(ctx, chat, 0, f"رفض الطلب #{rid} بالسبب: {text}؟", "req", f"{P3}req")
        return True
    return False
