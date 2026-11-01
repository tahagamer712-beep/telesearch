"""New-buttons router + hub — VM tree only, nothing deployed.

Routes every ``nb:`` callback to its phase module (``newbuttons_p1`` ..
``newbuttons_p5``) and every pending-text message to the owning phase
module's ``handle_text``. Also owns the ``🗂 إدارة البيانات`` hub screens
(main-menu reorganization, LAST step of the program): the hub is pure
navigation — every target button keeps its own server-side permission
check, so hiding a button is never the security boundary.

Wiring the parent still owns (documented, not silently claimed):
- template "run" execution (P1 shows conditions + audit only; running
  the search needs ``Bot.run_search`` — a bot-level hook the module
  cannot reach through ``ctx``);
- live ``JobQueue.control`` for a RUNNING job's pause/cancel (P2 flips
  the DB status, which stops a *queued* job from ever being claimed,
  but a running handler does not poll the DB and overwrites the
  status when it finishes);
- limits enforcement beyond file size (concurrent tasks, exports/day
  need a product decision: per-user vs global, which kinds count);
- isolation enforcement at intake/search (``nb_isolated_files`` rows
  are real and queryable; blocking at intake/search is a core-path
  change, not a router change);
- template-run, notification-send and queue-control hooks are listed
  in the final report with their exact locations.

Privacy rule (same as every phase module): this file never reads
``record_values`` and never prints a record field value.
"""

from __future__ import annotations

import sqlite3

from . import newbuttons_p1 as p1
from . import newbuttons_p2 as p2
from . import newbuttons_p3 as p3
from . import newbuttons_p4 as p4
from . import newbuttons_p5 as p5
from .nb_store import ensure_nb_tables, maintenance_active, set_pending  # noqa: F401  (set_pending re-exported for tests)
from .ui import kb, nav

PHASE_MODULES = {"p1": p1, "p2": p2, "p3": p3, "p4": p4, "p5": p5}

MAINT_MSG = "🛑 وضع الصيانة نشط — العمليات الجديدة متوقفة مؤقتاً. جرّب لاحقاً."


class _Ctx:
    """Adapter: Bot -> the ctx protocol phase modules expect."""

    def __init__(self, bot):
        self.db = bot.db
        self.cfg = bot.cfg
        self._bot = bot

    def show(self, chat, mid, text, markup=None):
        self._bot.show(chat, mid, text, markup)

    def send(self, chat, text, markup=None):
        self._bot.send(chat, text, markup)

    def deny(self, uid, what):
        self._bot._deny(uid, what)


def _audit_ses_kill_count(conn, actor_uid: int) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE user_id=? AND action='nb_p3_ses_kill'",
            (actor_uid,),
        ).fetchone()
    except sqlite3.Error:
        return -1
    return row[0] if row else 0


def _purge_killed_session(bot, actor_uid: int, before: int) -> None:
    """In-memory half of P3 session kill (the DB half is P3's own).

    Only purges when a NEW ``nb_p3_ses_kill`` audit row appeared for
    this actor during this dispatch — a denied/forged ``:yes`` writes
    no audit row, so it purges nothing.
    """
    if before < 0:
        return
    try:
        after = _audit_ses_kill_count(bot.db, actor_uid)
        if after <= before:
            return
        row = bot.db.execute(
            "SELECT target_id FROM audit_log WHERE user_id=? AND action='nb_p3_ses_kill' "
            "ORDER BY id DESC LIMIT 1",
            (actor_uid,),
        ).fetchone()
    except sqlite3.Error:
        return
    if row and row["target_id"] and str(row["target_id"]).isdigit():
        bot.state.pop(int(row["target_id"]), None)


# ------------------------------------------------------------------ hub

def _hub(bot, user, chat, mid, rest) -> bool:
    section = rest[0] if rest else ""
    if section == "":
        rows = [
            [("🔎 البحث", "nb:hub:search"), ("📁 الملفات", "nb:hub:files")],
            [("📋 المهام", "nb:hub:jobs"), ("📊 التقارير", "nb:hub:reports")],
            [("🔔 إشعاراتي", "nb:p1:ntf"), ("🆘 المساعدة", "nb:p1:help")],
        ]
        if user.is_admin:
            rows.append([("⚙️ لوحة المدير", "nb:hub:admin")])
        bot.show(chat, mid, "🗂 إدارة البيانات\nاختر قسماً:", kb(*rows, nav()))
        return True
    if section == "search":
        bot.show(chat, mid, "🔎 البحث\nبحث سريع ومتقدم وقوالب محفوظة.",
                 kb([("⚡ بحث سريع", "q"), ("🔎 بحث متقدم", "adv")],
                    [("⭐ قوالب البحث", "nb:p1:tpl")], nav("nb:hub")))
        return True
    if section == "files":
        bot.show(chat, mid, "📁 الملفات\nمصادر واستيراد ودفعات وتفاصيل.",
                 kb([("📁 المصادر", "src:a:0"), ("📥 مركز استيراد", "nb:p2:imp")],
                    [("🗃 إدارة الدفعات", "nb:p2:bat"), ("📄 تفاصيل الملف", "nb:p1:fdet")],
                    [("🗂 فهرس الملفات والحقول", "nb:p2:idx"), ("🧪 فحص الملفات", "nb:p2:chk")],
                    nav("nb:hub")))
        return True
    if section == "jobs":
        bot.show(chat, mid, "📋 المهام\nمهامك وطابور المعالجة وسجل عملياتك.",
                 kb([("📋 المهام", "jobs:0"), ("⚙️ طابور المعالجة", "nb:p2:que")],
                    [("🕘 سجل عملياتي", "nb:p1:ops")], nav("nb:hub")))
        return True
    if section == "reports":
        bot.show(chat, mid, "📊 التقارير\nتقاريرك الجاهزة ودمج الملفات وتقرير صحة النظام.",
                 kb([("📤 التقارير", "rep:0"), ("🗂 دمج الملفات (Excel)", "master")],
                    [("📄 تقرير صحة النظام", "nb:p5:rep")], nav("nb:hub")))
        return True
    if section == "admin":
        if not user.is_admin:
            bot._deny(user.telegram_id, "nb_hub_admin")
            bot.show(chat, mid, "⛔ Access Denied", kb(nav()))
            return True
        bot.show(chat, mid, "⚙️ لوحة المدير — إدارة البيانات",
                 kb([("👥 المستخدمون", "usr:0"), ("👥 مجموعات الصلاحيات", "nb:p3:grp")],
                    [("📩 طلبات الصلاحيات", "nb:p3:req"), ("🧑‍⚖️ مراجعة الحسابات", "nb:p3:acc")],
                    [("📥 مركز استيراد", "nb:p2:imp"), ("🗃 إدارة الدفعات", "nb:p2:bat")],
                    [("🧪 فحص الملفات", "nb:p2:chk"), ("🛡 مراجعة الاستخدام المصرح", "nb:p2:auth")],
                    [("🛡 مركز الأمان", "nb:p4:sec"), ("🔎 البحث في سجل التدقيق", "nb:p4:sch")],
                    [("🔐 فحص النسخ الاحتياطية", "nb:p4:bkc"), ("🛑 وضع الصيانة", "nb:p5:mnt")],
                    [("📈 لوحة الأداء", "nb:p5:dash"), ("📋 قائمة المراجعات", "nb:p5:rev")],
                    nav("nb:hub")))
        return True
    return False


# ------------------------------------------------------------------ dispatch

def nb_dispatch(bot, user, chat, mid, data: str) -> bool:
    """Route one ``nb:`` callback. Returns False for unknown keys
    (caller shows nothing) — exactly the phase-module contract."""
    ensure_nb_tables(bot.db)
    parts = data.split(":")
    if len(parts) >= 2 and parts[1] == "hub":
        return _hub(bot, user, chat, mid, parts[2:])
    if len(parts) >= 3 and parts[1] in PHASE_MODULES:
        # Maintenance gate (defense in depth; Bot.on_callback gates too):
        # non-admin phase actions are new operations, so they pause.
        # Hub navigation above stays open (menu only, no data, no op).
        if not user.is_admin and maintenance_active(bot.db):
            bot.show(chat, mid, MAINT_MSG, kb(nav()))
            return True
        mod = PHASE_MODULES[parts[1]]
        before = _audit_ses_kill_count(bot.db, user.telegram_id) if data == "nb:p3:ses:yes" else -1
        ok = mod.handle(_Ctx(bot), user, chat, mid, parts[2:])
        if ok and data == "nb:p3:ses:yes":
            _purge_killed_session(bot, user.telegram_id, before)
        return ok
    return False


def nb_handle_text(bot, user, chat, text: str) -> bool:
    """Route one pending-text message to its owning phase module.

    Dispatch is by the pending action's prefix, and ONLY that module
    is called: P2's ``handle_text`` enforces ``manage`` before it
    checks the action, so calling it blindly would deny unrelated
    users' flows. Unknown actions return False with the row intact.
    """
    ensure_nb_tables(bot.db)
    try:
        row = bot.db.execute(
            "SELECT action FROM nb_pending_text WHERE user_id=?", (user.telegram_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    action = row[0]
    ctx = _Ctx(bot)
    if action.startswith("nb_p1_"):
        return p1.handle_text(ctx, user, chat, text)
    if action.startswith("p3_"):
        return p3.handle_text(ctx, user, chat, text)
    if action.startswith("p4:"):
        return p4.handle_text(ctx, user, chat, text)
    if action.startswith("nb_p2_"):
        return p2.handle_text(ctx, user, chat, text)
    return False
