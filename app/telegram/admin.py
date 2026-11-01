"""Admin Panel + user management + Entity Resolution screens + unified entity screens.
Every method here is reached only through Bot.on_callback, which enforces ADMIN server-side
(ADMIN_HEADS guard) - hiding buttons/commands is never the security boundary."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import audit, auth, maintenance
from ..auth import PERM_AR, PERMISSIONS, User
from ..backup import create_backup
from ..er import entities, schema_map
from ..er.matcher import MATCHER_VERSION
from ..errors import AppError
from ..health import format_checks, run_checks
from .ui import LINE, ago, fmt_int, fmt_size, kb, nav, pager

log = logging.getLogger("pdb.admin")
ADMIN_HEADS = {"nt", "fs", "ap", "usr", "er", "sch", "bk", "ai", "mt", "diag", "log", "clean", "dev", "idx", "idxr", "entu", "sdr", "jc"}
COMMON_FIELDS = ["patient_id", "national_id", "full_name", "date_of_birth", "sex", "phone", "visit_date", "department", "test_date", "admission_date", "dispense_date", "notes"]


def _nm(r, k="name_norm"):
    return (r or {}).get(k) or "-"


class AdminMixin:
    def _clear_user_state(self, uid: int) -> None:
        """Audit fix (Phase 6 4.4): disabling/removing a user must purge their
        live session state, or a stale await fires again after re-enable
        (in-memory bot.state awaits + callback_state rows, TTL 1440 min)."""
        try:
            self.state.pop(uid, None)
        except AttributeError:
            pass
        self.db.execute("DELETE FROM callback_state WHERE user_id=?", (uid,))

    # ------------------------------------------------------------------ panel
    def admin_panel(self, chat, mid):
        self.show(chat, mid, "⚙️ ADMIN PANEL", kb(
            [("👥 المستخدمون", "usr:0"), ("🔗 مطابقة الكيانات", "er")],
            [("🧬 ربط الحقول", "sch:0"), ("🤖 OpenRouter", "ai")],
            [("💾 النسخ الاحتياطي", "bk"), ("🛠 التشخيص", "diag:ap")],
            [("📢 الاشتراك الإجباري", "fs"), ("🔔 الإشعارات المجدولة", "nt")],
            [("👨‍💻 وضع المطور", "dev")],
            [("🛡 سجل التدقيق", "log:0")],
            [("💾 التخزين", "clean:menu"), ("🔄 الصيانة", "mt")],
            [("⚙️ إعدادات البوت", "set")], nav()))

    # ------------------------------------------------------------------ force subscription (generic access control; admin only)
    def fs_cb(self, user: User, chat, mid, rest):
        a = rest[0] if rest else ""
        fs = self.fs
        uid = user.telegram_id
        if a == "":
            chans = fs.channels()
            on = fs.enabled()
            return self.show(chat, mid, f"📢 Mandatory Channels\nForce Subscription: {'🟢 مفعّل' if on else '🔴 معطّل'}\nChannels: {len(chans)} (فعّالة: {sum(c.enabled for c in chans)})\nADMIN يتجاوز الشرط. التحقق من Telegram على الخادم، وأي فشل = منع (fail closed).",
                             kb([("➕ إضافة قناة", "fs:add"), ("📋 القنوات", "fs:l")], [(("🔴 تعطيل" if on else "🟢 تفعيل") + " الاشتراك الإجباري", "fs:tg")], nav("ap")))
        if a == "tg":
            fs.set_enabled(not fs.enabled())
            audit.log(self.db, uid, audit.SETTINGS, "force_subscription", "enabled", {"value": fs.enabled()})
            return self.fs_cb(user, chat, mid, [])
        if a == "add":
            self.state.setdefault(uid, {})["await"] = "fs_add"
            return self.show(chat, mid, "➕ أرسل: معرّف القناة ثم الاسم (اختياري) ثم رابط الانضمام (للقنوات الخاصة)\nأمثلة:\n@mychannel قناتنا\n-1001234567890 القناة الخاصة https://t.me/+abcdef\n(يجب ان يكون البوت مشرفاً في القناة)", kb(nav("fs")))
        if a == "l":
            rows = [[(f"{'🟢' if c.enabled else '🔴'} {c.title[:24]} ({c.chat_ref[:16]})", f"fs:v:{c.id}")] for c in fs.channels()]
            return self.show(chat, mid, "📋 القنوات" + ("" if rows else "\nلا توجد قنوات"), kb(*rows, nav("fs")))
        cid = int(rest[1])
        ch = fs.get(cid)
        if ch is None:
            return self.show(chat, mid, "القناة غير موجودة.", kb(nav("fs")))
        if a == "v":
            return self.show(chat, mid, f"📢 {ch.title}\nID: {ch.chat_ref}\nJoin link: {ch.join_url or 'لا يوجد (قناة خاصة بلا رابط)'}\nStatus: {'🟢 Enabled' if ch.enabled else '🔴 Disabled'}",
                             kb([("✏️ تعديل", f"fs:ed:{cid}"), (("🔴 تعطيل" if ch.enabled else "🟢 تفعيل"), f"fs:te:{cid}")], [("🔍 اختبار", f"fs:t:{cid}"), ("🗑 حذف", f"fs:rm:{cid}")], nav("fs:l")))
        if a == "te":
            fs.set_channel_enabled(cid, not ch.enabled)
            audit.log(self.db, uid, audit.SETTINGS, "force_sub_channel", cid, {"enabled": not ch.enabled})
            return self.fs_cb(user, chat, mid, ["v", str(cid)])
        if a == "ed":
            self.state.setdefault(uid, {})["await"] = f"fs_edit:{cid}"
            return self.show(chat, mid, "✏️ أرسل الاسم الجديد، وبعده (اختياري) رابط الانضمام:\nمثال: قناتنا الرسمية https://t.me/+abc", kb(nav(f"fs:v:{cid}")))
        if a == "t":
            return self.show(chat, mid, "🔍 " + fs.test_channel(cid), kb(nav(f"fs:v:{cid}")))
        if a == "rm":
            return self.show(chat, mid, f"⚠️ حذف القناة {ch.title}؟", kb([("🗑 تأكيد الحذف", f"fs:rmc:{cid}")], nav(f"fs:v:{cid}")))
        if a == "rmc":
            fs.remove_channel(cid)
            audit.log(self.db, uid, audit.SETTINGS, "force_sub_channel", cid, {"removed": True})
            return self.show(chat, mid, "🗑 تم الحذف.", kb(nav("fs")))

    def fs_text(self, user: User, chat, kind, text):
        from ..access.force_sub import ForceSubError
        parts = text.split()
        try:
            if kind == "fs_add":
                if not parts:
                    raise ForceSubError("أرسل معرّف القناة")
                link = next((p for p in parts[1:] if p.startswith("http")), None)
                title = " ".join(p for p in parts[1:] if not p.startswith("http")) or None
                cid = self.fs.add_channel(parts[0], title, link)
                audit.log(self.db, user.telegram_id, audit.SETTINGS, "force_sub_channel", cid, {"added": parts[0]})
                return self.send(chat, "✅ أُضيفت القناة. تأكد ان البوت مشرف فيها (🔍 Test).", kb([("📋 القنوات", "fs:l")], nav("fs")))
            cid = int(kind.split(":", 1)[1])
            link = next((p for p in parts if p.startswith("http")), None)
            title = " ".join(p for p in parts if not p.startswith("http")) or None
            self.fs.edit_channel(cid, title, link)
            audit.log(self.db, user.telegram_id, audit.SETTINGS, "force_sub_channel", cid, {"edited": True})
            return self.send(chat, "✅ تم التعديل.", kb(nav(f"fs:v:{cid}")))
        except ForceSubError as exc:
            self.send(chat, f"⚠️ {exc}", kb(nav("fs")))

    # ------------------------------------------------------------------ scheduled notifications (ADMIN only; no record content)
    def nt_cb(self, user: User, chat, mid, rest):
        a = rest[0] if rest else ""
        svc, uid = self.app.notify, user.telegram_id
        if a == "":
            rows = svc.list()
            btns = [[(f"{'🟢' if r['enabled'] else '🔴'} {r['name'][:22]} ({r['content_type']})", f"nt:v:{r['id']}")] for r in rows]
            return self.show(chat, mid, "🔔 Scheduled Notifications\nالمحتوى المسموح: stats • ops_summary • task_events\n(لا ارسال مجدول لنتائج بحث/تقارير فيها سجلات)", kb([("➕ جدولة جديدة", "nt:add")], *btns, nav("ap")))
        if a == "add":
            self.state.setdefault(uid, {})["await"] = "nt_add"
            return self.show(chat, mid, "أرسل في سطر واحد:\nالاسم | IDs مفصولة بفاصلة | النوع | الجدولة | الحد الاقصى\nمثال:\nاحصائيات يومية | 123,456 | stats | daily 08:30 | 10\nالانواع: stats / ops_summary / task_events\nالجدولة: every N • daily HH:MM • weekly D HH:MM", kb(nav("nt")))
        sid = int(rest[1])
        r = svc._row(sid)
        if r is None:
            return self.show(chat, mid, "غير موجودة", kb(nav("nt")))
        if a == "v":
            return self.show(chat, mid, f"🔔 {r['name']}\nType: {r['content_type']}\nTargets: {', '.join(map(str, json.loads(r['targets'])))}\nSchedule: {r['frequency']} {r['time_of_day'] or r['interval_min'] or ''}\nMax items: {r['max_items']} • Format: text\nStatus: {'🟢 Enabled' if r['enabled'] else '🔴 Disabled'}\nNext: {r['next_run']}\nLast: {r['last_run'] or '-'} ({r['last_status'] or '-'})",
                             kb([(("🔴 تعطيل" if r["enabled"] else "🟢 تفعيل"), f"nt:te:{sid}"), ("▶️ تشغيل الآن", f"nt:run:{sid}")], [("🗑 حذف", f"nt:rm:{sid}")], nav("nt")))
        if a == "te":
            svc.set_enabled(sid, not r["enabled"], uid)
            return self.nt_cb(user, chat, mid, ["v", str(sid)])
        if a == "run":
            jid = svc.run_now(sid, uid)
            return self.show(chat, mid, f"⏳ JOB-{jid:06d}", kb(nav(f"nt:v:{sid}")))
        if a == "rm":
            # Audit fix (Phase 4 F4): was an immediate delete, unlike fs:rm /
            # usr:rm which confirm. One accidental tap destroyed a schedule.
            return self.show(chat, mid, f"⚠️ حذف الجدولة {r['name']}؟", kb([("🗑 تأكيد الحذف", f"nt:rmc:{sid}")], nav(f"nt:v:{sid}")))
        if a == "rmc":
            svc.delete(sid, uid)
            return self.show(chat, mid, "🗑 حُذفت.", kb(nav("nt")))

    def nt_text(self, user: User, chat, text):
        from ..notify.service import NotifyError
        parts = [p.strip() for p in text.split("|")]
        try:
            if len(parts) < 4:
                raise NotifyError("الصيغة: الاسم | IDs | النوع | الجدولة | [الحد الاقصى]")
            ids = [int(x) for x in parts[1].replace(" ", "").split(",") if x]
            sid = self.app.notify.create(user.telegram_id, parts[0], ids, parts[2], parts[3], int(parts[4]) if len(parts) > 4 and parts[4] else 10)
            return self.send(chat, f"✅ جدولة #{sid} أُنشئت.", kb([("🔔 الجدولات", "nt")], nav("ap")))
        except (NotifyError, ValueError) as exc:
            self.send(chat, f"⚠️ {exc}", kb(nav("nt")))

    # ------------------------------------------------------------------ users
    def users_list(self, chat, mid, page, query: str | None = None):
        where, args = "role='user'", []
        if query:
            where += " AND (CAST(telegram_id AS TEXT) LIKE ? OR display_name LIKE ? OR username LIKE ?)"
            args = [f"%{query}%"] * 3
        n = self.db.execute(f"SELECT COUNT(*) FROM users WHERE {where}", args).fetchone()[0]
        pages = max(1, -(-n // 8))
        page = max(0, min(page, pages - 1))
        rows = self.db.execute(f"SELECT * FROM users WHERE {where} ORDER BY telegram_id LIMIT 8 OFFSET ?", [*args, page * 8]).fetchall()
        admins = ", ".join(str(i) for i in sorted(self.cfg.authorized_user_ids))
        btns = [[(f"{'🟢' if r['enabled'] else '🔴'} {(r['display_name'] or '-')[:18]} ({r['telegram_id']})", f"usr:v:{r['telegram_id']}")] for r in rows]
        nv = pager(page, pages, "usr")
        if nv:
            btns.append(nv)
        self.show(chat, mid, f"👥 Users" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + f"\nADMIN (من .env، لا يُعدَّل من هنا): {admins}\nUSERS: {n}", kb([("➕ إضافة مستخدم", "usr:add"), ("🔍 بحث عن مستخدم", "usr:s")], *btns, nav("ap")))

    def user_view(self, chat, mid, uid, note=""):
        r = self.db.execute("SELECT * FROM users WHERE telegram_id=? AND role='user'", (uid,)).fetchone()
        if r is None:
            return self.show(chat, mid, "المستخدم غير موجود (او هو ADMIN).", kb(nav("usr:0")))
        perms = set(json.loads(r["permissions"] or "[]"))
        text = (f"👤 {r['display_name'] or '-'}  {('@' + r['username']) if r['username'] else ''}\nID: {uid}\nStatus: {'🟢 Active' if r['enabled'] else '🔴 Disabled'}\nPermissions:\n"
                + "\n".join(f"{'✅' if p in perms else '⬜'} {PERM_AR[p]}" for p in PERMISSIONS) + (f"\n{note}" if note else "") + "\n(لا توجد صلاحيات Admin قابلة للمنح)")
        rows = [[(("✅ " if p in perms else "⬜ ") + PERM_AR[p], f"usr:t:{uid}:{i}")] for i, p in enumerate(PERMISSIONS)]
        rows.append([("🚫 تعطيل", f"usr:x:{uid}")] if r["enabled"] else [("✅ تفعيل", f"usr:e:{uid}")])
        rows.append([("📋 نشاط المستخدم", f"usr:act:{uid}"), ("🗑 حذف", f"usr:rm:{uid}")])
        self.show(chat, mid, text, kb(*rows, nav("usr:0")))

    def users_cb(self, user: User, chat, mid, rest):
        a = rest[0]
        st = self.state.setdefault(user.telegram_id, {})
        if a.isdigit():
            return self.users_list(chat, mid, int(a))
        if a == "add":
            st["await"] = "usr_add"
            return self.show(chat, mid, "➕ Add User\nأرسل: Telegram User ID ثم الاسم (اختياري) ثم @username (اختياري)\nمثال:\n123456789 أحمد @ahmed", kb(nav("usr:0")))
        if a == "s":
            st["await"] = "usr_s"
            return self.show(chat, mid, "🔍 أرسل ID او اسم او username:", kb(nav("usr:0")))
        uid = int(rest[1])
        if uid in self.cfg.authorized_user_ids:
            return self.show(chat, mid, "⛔ هذا ADMIN (مصدره .env) ولا يُعدَّل من Telegram.", kb(nav("usr:0")))
        if a == "v":
            return self.user_view(chat, mid, uid)
        if a == "t":
            p = PERMISSIONS[int(rest[2])]
            cur = set(json.loads((self.db.execute("SELECT permissions FROM users WHERE telegram_id=? AND role='user'", (uid,)).fetchone() or ['[]'])[0]))
            cur ^= {p}
            auth.set_perms(self.db, uid, cur)
            audit.log(self.db, user.telegram_id, audit.USERS, "user", uid, {"perms": sorted(cur)})
            return self.user_view(chat, mid, uid)
        if a in ("x", "e"):
            auth.set_enabled(self.db, uid, a == "e")
            if a == "x":
                self._clear_user_state(uid)
            audit.log(self.db, user.telegram_id, audit.USERS, "user", uid, {"enabled": a == "e"})
            return self.user_view(chat, mid, uid)
        if a == "rm":
            return self.show(chat, mid, f"⚠️ حذف المستخدم {uid} نهائياً؟ (سجله في Audit يبقى)", kb([("🗑 تأكيد الحذف", f"usr:rmc:{uid}")], nav(f"usr:v:{uid}")))
        if a == "rmc":
            auth.remove_user(self.db, uid)
            self._clear_user_state(uid)
            audit.log(self.db, user.telegram_id, audit.USERS, "user", uid, {"removed": True})
            return self.show(chat, mid, "🗑 تم الحذف.", kb(nav("usr:0")))
        if a == "act":
            rows = self.db.execute("SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()
            return self.show(chat, mid, f"📋 Activity {uid}\n" + ("\n".join(f"{ago(r['ts'])} {r['action']} {r['target_type'] or ''}{r['target_id'] or ''}" for r in rows) or "لا نشاط"), kb(nav(f"usr:v:{uid}")))

    def users_text(self, user: User, chat, kind, text):
        if kind == "usr_add":
            parts = text.split()
            if not parts or not parts[0].lstrip("-").isdigit():
                return self.send(chat, "⚠️ ابدأ بالـID رقماً.", kb(nav("usr:0")))
            uid = int(parts[0])
            if uid in self.cfg.authorized_user_ids:
                return self.send(chat, "هذا ADMIN أصلاً.", kb(nav("usr:0")))
            uname = next((p[1:] for p in parts[1:] if p.startswith("@")), None)
            name = " ".join(p for p in parts[1:] if not p.startswith("@")) or None
            auth.add_user(self.db, uid, name, uname, ("search",))
            audit.log(self.db, user.telegram_id, audit.USERS, "user", uid, {"added": True})
            return self.user_view(chat, None, uid, "✅ أُضيف. حدّد الصلاحيات:")
        if kind == "usr_s":
            return self.users_list(chat, None, 0, text.strip())

    # ------------------------------------------------------------------ entity resolution admin
    def er_home(self, chat, mid, note=""):
        s = entities.statistics(self.db)
        auto = (self.db.execute("SELECT value FROM settings WHERE key='er_auto'").fetchone() or ["1"])[0] == "1"
        aex = (self.db.execute("SELECT value FROM settings WHERE key='er_auto_export'").fetchone() or ["0"])[0] == "1"
        text = (f"🔗 Entity Resolution (matcher v{s['matcher_version']})\nRecords:\n{fmt_int(s['records_total'])} (هوية: {fmt_int(s['records_in_er'])})\nExact duplicates:\n{fmt_int(s['exact_duplicate_records'])}\n"
                f"Entities:\n{fmt_int(s['entities'])} (متعددة المصادر: {fmt_int(s['multi_record_entities'])})\nCandidates compared:\n{fmt_int(s['candidates_compared'])}\n"
                f"High-confidence/Exact matches:\n{fmt_int(s['high_confidence'] + s['exact'])}\nProbable+Possible:\n{fmt_int(s['probable'] + s['possible'])}\nNeeds Review:\n{fmt_int(s['needs_review'])}\nConflicts:\n{fmt_int(s['conflicts'])}\n"
                f"Rejected by admin:\n{fmt_int(s['rejected_by_admin'])}\nSkipped oversized blocks:\n{s['skipped_oversized_blocks']}\nFields needing review:\n{s['fields_needing_review']}\n"
                f"دقة المطابقة على بياناتك الحقيقية: UNVERIFIED" + (f"\n{note}" if note else ""))
        self.show(chat, mid, text, kb([("▶️ تشغيل المطابقة", "er:run"), ("🔁 إعادة بناء", "er:rebuild")], [("⚠️ التعارضات", "er:cf:0")],
                                       [("🕘 سجل الدمج", "er:mh:0"), ("📤 إكسل الموحد", "er:xl")], [(("🟢" if auto else "🔴") + " المطابقة التلقائية", "er:auto"), (("🟢" if aex else "🔴") + " إكسل التلقائي", "er:ax")],
                                       [("⏯ المهام (إيقاف/استئناف/إلغاء)", "jobs:0")], nav("ap")))

    def er_cb(self, user: User, chat, mid, rest):
        a = rest[0] if rest else ""
        uid = user.telegram_id
        setv = lambda k, v: self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        if a == "":
            return self.er_home(chat, mid)
        if a in ("run", "rebuild"):
            jid = self.app.er.submit(uid, rebuild=(a == "rebuild"))
            audit.log(self.db, uid, audit.INDEX, "entity_resolution", jid, {"rebuild": a == "rebuild"})
            return self.er_home(chat, mid, f"⏳ JOB-{jid:06d} (ER)")
        if a == "xl":
            jid = self.app.er.submit_export(uid)
            return self.er_home(chat, mid, f"⏳ Unified Excel JOB-{jid:06d}: سيصلك الملف عند الانتهاء")
        if a in ("auto", "ax"):
            key = "er_auto" if a == "auto" else "er_auto_export"
            cur = (self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() or ["1" if a == "auto" else "0"])[0]
            setv(key, "0" if cur == "1" else "1")
            audit.log(self.db, uid, audit.SETTINGS, "setting", key, {"value": "0" if cur == "1" else "1"})
            return self.er_home(chat, mid)
        if a in ("rv", "cf"):
            page = int(rest[1])
            if a == "rv":
                total = self.db.execute("SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW')").fetchone()[0]
            else:
                total = self.db.execute("SELECT COUNT(*) FROM entity_matches WHERE state='CONFLICT' AND (decision IS NULL OR decision='conflict')").fetchone()[0]
            pages = max(1, -(-total // 8))
            page = max(0, min(page, pages - 1))
            rows = entities.review_queue(self.db, offset=page * 8) if a == "rv" else entities.conflicts(self.db, offset=page * 8)
            btns = []
            for m in rows:
                d = entities.match_detail(self.db, m["id"])
                btns.append([(f"#{m['id']} {entities.STATE_LABEL[m['state']].split(' ', 1)[1][:10]} {m['confidence']:.2f} {_nm(d['a'])[:12]}↔{_nm(d['b'])[:12]}", f"er:m:{m['id']}:{a}{page}")])
            nv = pager(page, pages, f"er:{a}")
            if nv:
                btns.append(nv)
            title = ("🔍 Match Review" if a == "rv" else "⚠️ Conflicts") + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "")
            return self.show(chat, mid, title + ("" if rows else "\nلا شيء للمراجعة 👍"), kb(*btns, nav("er")))
        if a == "m":
            return self.match_screen(chat, mid, int(rest[1]), rest[2] if len(rest) > 2 else "rv0")
        if a in ("c", "r", "f"):
            mid_ = int(rest[1])
            fn = {"c": entities.confirm_match, "r": entities.reject_match, "f": entities.mark_conflict}[a]
            try:
                out = fn(self.db, mid_, uid)
            except ValueError as exc:
                return self.show(chat, mid, f"⚠️ {exc}", kb(nav("er")))
            msg = {"c": "✅ تم تأكيد التطابق", "r": "❌ رُفض (NEVER MATCH)", "f": "⚠️ وُسم كتعارض"}[a]
            return self.show(chat, mid, f"{msg}\nSplits: {out.get('splits', 0)} | Merges: {out.get('merges', 0)}", kb([("🔍 التالي", "er:rv:0")], nav("er")))
        if a == "mh":
            page = int(rest[1])
            total = self.db.execute("SELECT COUNT(*) FROM entity_merge_history").fetchone()[0]
            pages = max(1, -(-total // 10))
            page = max(0, min(page, pages - 1))
            rows = entities.history(self.db, 10, page * 10)
            text = "🕘 Merge History" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + "\n" + "\n".join(f"{ago(r['ts'])} {('U-%06d' % r['entity_id']) if r['entity_id'] else '-'} {r['action']} v{r['matcher_version']} {r['actor'] or ''}" for r in rows)
            nv = pager(page, pages, "er:mh")
            return self.show(chat, mid, text, kb(*([nv] if nv else []), nav("er")))

    def match_screen(self, chat, mid, match_id, back="rv0"):
        d = entities.match_detail(self.db, match_id)
        if d is None:
            return self.show(chat, mid, "غير موجود", kb(nav("er")))
        m, A, B = d["match"], d["a"], d["b"]
        ev = {i["field"]: i for i in d["evidence"].get("items", [])}
        cf = {c["field"]: c for c in d["conflicts"]}
        def line(label, ka, key):
            mark = "✓" if key in ev and ev[key]["kind"] in ("exact", "similar") else ("✗" if key in cf else "·")
            return f"{label} {mark}\nA: {A.get(ka) or '-'}\nB: {B.get(ka) or '-'}"
        text = (f"🔗 Match #{match_id}\nState: {entities.STATE_LABEL[m['state']]}\nConfidence: {m['confidence']:.2f} (rule {d['evidence'].get('rule')}, v{m['matcher_version']})\n{LINE}\n"
                f"{line('NAME', 'name_norm', 'full_name')}\n{line('PATIENT ID', 'patient_id', 'patient_id')}\n{line('NATIONAL ID', 'national_id', 'national_id')}\n{line('DOB', 'dob', 'date_of_birth')}\n{line('SEX', 'sex', 'sex')}\n{line('PHONE', 'phone', 'phone')}\n{LINE}\n"
                f"SOURCE\nA: {(A.get('pname') + ' → ') if A.get('pname') else ''}{A.get('fname')} / {A.get('locator')}\nB: {(B.get('pname') + ' → ') if B.get('pname') else ''}{B.get('fname')} / {B.get('locator')}")
        if cf:
            text += "\nCONFLICTS:\n" + "\n".join(f"✗ {c['field']}: {c.get('detail', '')}" for c in cf.values())
        text += f"\nDecision: {m['decision'] or 'pending'}"
        self.show(chat, mid, text, kb([("✅ تأكيد المطابقة", f"er:c:{match_id}")], [("❌ رفض المطابقة", f"er:r:{match_id}")], [("⚠️ وسم كتعارض", f"er:f:{match_id}")], nav("er:" + back[:2] + ":" + back[2:] if back[:2] in ("rv", "cf") else "er")))

    # ------------------------------------------------------------------ schema mapping admin
    def sch_cb(self, user: User, chat, mid, rest):
        a = rest[0]
        if a.isdigit():
            page = int(a)
            total = self.db.execute("SELECT COUNT(*) FROM field_mappings WHERE confidence IN ('NEEDS_REVIEW','LOW') AND status='auto'").fetchone()[0]
            pages = max(1, -(-total // 8))
            page = max(0, min(page, pages - 1))
            rows = self.db.execute("SELECT fm.field_id, fl.name, f.original_name fname, fm.confidence FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id JOIN files f ON f.id=fl.file_id "
                                   "WHERE fm.confidence IN ('NEEDS_REVIEW','LOW') AND fm.status='auto' ORDER BY fm.field_id LIMIT 8 OFFSET ?", (page * 8,)).fetchall()
            syn = self.db.execute("SELECT COUNT(*) FROM schema_synonyms").fetchone()[0]
            btns = [[(f"{r['name'][:30]} ({r['fname'][:14]})", f"sch:f:{r['field_id']}")] for r in rows]
            nv = pager(page, pages, "sch")
            if nv:
                btns.append(nv)
            return self.show(chat, mid, f"🧬 Schema Mapping" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + f"\nSynonyms: {syn}\nأعمدة تحتاج مراجعة:" + ("" if rows else "\nلا شيء 👍"), kb([("➕ مرادف جديد", "sch:add")], *btns, nav("ap")))
        if a == "add":
            self.state.setdefault(user.telegram_id, {})["await"] = "sch_add"
            return self.show(chat, mid, "أرسل: مرادف => حقل_قياسي\nمثال: رقم المراجع => patient_id\nالحقول: " + ", ".join(schema_map.STANDARD_FIELDS[:14]) + " ...", kb(nav("sch:0")))
        fid = int(rest[1])
        if a == "f":
            r = self.db.execute("SELECT fm.*, fl.name, f.original_name fname FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id JOIN files f ON f.id=fl.file_id WHERE fm.field_id=?", (fid,)).fetchone()
            if r is None:
                return self.show(chat, mid, "غير موجود", kb(nav("sch:0")))
            samples = [x[0] for x in self.db.execute("SELECT original_value FROM record_values WHERE field_id=? AND original_value IS NOT NULL LIMIT 5", (fid,))]
            rows = [[(c, f"sch:s:{fid}:{i}") for i, c in enumerate(COMMON_FIELDS) if i // 3 == k] for k in range(4)]
            return self.show(chat, mid, f"🧬 {r['name']}\nFile: {r['fname']}\nCurrent: {r['standard_field'] or '-'} ({r['confidence']}, {r['status']})\nReason: {r['reason']}\nSamples: {' | '.join(s[:20] for s in samples)}\nاختر الحقل القياسي:",
                             kb(*rows, [("🚫 تجاهل (يبقى حقلاً اضافياً)", f"sch:s:{fid}:99")], nav("sch:0")))
        if a == "s":
            idx = int(rest[2])
            std = COMMON_FIELDS[idx] if idx < len(COMMON_FIELDS) else None
            n = schema_map.apply_admin_mapping(self.db, fid, std)
            audit.log(self.db, user.telegram_id, audit.SETTINGS, "field_mapping", fid, {"standard": std})
            return self.show(chat, mid, f"✅ حُفظ ({std or 'ignored'}). أُبطلت {n} سجلات هوية للملف؛ شغّل Run Matching.", kb([("▶️ تشغيل المطابقة", "er:run")], nav("sch:0")))

    def sch_text(self, user, chat, text):
        if "=>" not in text:
            return self.send(chat, "⚠️ الصيغة: مرادف => حقل_قياسي", kb(nav("sch:0")))
        syn, std = (x.strip() for x in text.split("=>", 1))
        try:
            schema_map.add_synonym(self.db, syn, std)
        except ValueError as exc:
            return self.send(chat, f"⚠️ {exc}", kb(nav("sch:0")))
        audit.log(self.db, user.telegram_id, audit.SETTINGS, "synonym", syn, {"standard": std})
        self.send(chat, "✅ أُضيف المرادف. يُطبَّق على الملفات القادمة (للملفات الحالية: عدّل العمود من القائمة).", kb(nav("sch:0")))

    # ------------------------------------------------------------------ backup / ai / maintenance
    def backup_cb(self, user: User, chat, mid, rest):
        a = rest[0] if rest else ""
        d = Path(self.cfg.base_dir) / "backups"
        if a == "new":
            try:
                out = create_backup(self.cfg, self.db)
                audit.log(self.db, user.telegram_id, audit.BACKUP, "backup", out.name, {"size": out.stat().st_size})
                msg = f"✅ {out.name} ({fmt_size(out.stat().st_size)})"
            except AppError as exc:
                msg = f"❌ {exc}"
            return self.show(chat, mid, "💾 " + msg, kb(nav("bk")))
        files = sorted(d.glob("backup_*.tar.gz*"))[-6:] if d.exists() else []
        files = [f for f in files if not f.name.endswith(".sha256")]
        self.show(chat, mid, "💾 النسخ الاحتياطي\nالتشفير: " + ("🟢 AES-256 (openssl)" if self.cfg.backup_passphrase else "🔴 غير مضبوط (BACKUP_PASSPHRASE)") + "\n" + ("\n".join(f"• {f.name} {fmt_size(f.stat().st_size)}" for f in files) or "لا نسخ")
                  + "\nالاستعادة من الطرفية فقط: python -m app.cli restore <file>", kb([("💾 إنشاء نسخة احتياطية", "bk:new")], nav("ap")))

    def ai_screen(self, chat, mid):
        c = self.cfg
        self.show(chat, mid, f"🤖 OpenRouter\nAI: {'🟢 Enabled' if c.ai_enabled else '🔴 Disabled'}\nModel: {c.openrouter_model or '-'}\nTimeout: {c.ai_timeout_sec}s\nيُرسل فقط نص سؤالك (الأرقام/الإيميلات تُحجب). لا بيانات ولا نتائج.\n"
                  "التغيير من .env ثم ./stop.sh && ./start.sh (المفتاح لا يُعرض ولا يُدخل من Telegram).", kb(nav("ap")))

    def maint_cb(self, user, chat, mid, rest):
        a = rest[0] if rest else ""
        if a == "ret":
            r = maintenance.run_retention(self.db, self.app.storage, self.cfg)
            return self.show(chat, mid, f"🔄 تم: {r}", kb(nav("mt")))
        if a == "opt":
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("PRAGMA optimize")
            return self.show(chat, mid, "🔄 WAL checkpoint + optimize ✅", kb(nav("mt")))
        self.show(chat, mid, "🔄 Maintenance", kb([("🧹 الاحتفاظ (تقارير/بحث قديم)", "mt:ret")], [("🗜 نقطة تفتيش WAL وتحسين", "mt:opt")], nav("ap")))

    # ------------------------------------------------------------------ unified entity screens
    def entity_screen(self, user: User, chat, mid, eid, back=""):
        """Unified Record Profile. Audit fix (Phase 6 2.7, P1): a 'search'
        holder used to open ANY entity profile (victim name + phone) without
        ever searching that data, breaking the own_search boundary enforced
        everywhere else. Non-admins may now only open entities appearing in
        their own searches; admins (manage) keep review access. Every value
        still has a source; no external data."""
        if not user.can("manage"):
            own = self.db.execute("SELECT 1 FROM search_groups sg JOIN search_history sh ON sh.id=sg.search_id WHERE sg.entity_id=? AND sh.user_id=? LIMIT 1",
                                  (eid, user.telegram_id)).fetchone()
            if own is None:
                audit.log(self.db, user.telegram_id, audit.DENIED, "entity_profile", eid, {"reason": "ownership"})
                return self.show(chat, mid, "⛔ هذا الكيان ليس من نتائج بحثك.", kb(nav()))
        from .ui import chunk_text, profile_text
        v = entities.entity_view(self.db, eid)
        if v is None or v["status"] != "active":
            return self.show(chat, mid, "الكيان غير موجود/تغيّر. أعد البحث.", kb(nav()))
        audit.log(self.db, user.telegram_id, audit.OPEN, "entity_profile", eid, {"public_id": v["public_id"], "records": len(v["record_ids"])})
        rows = [[("🧠 لماذا؟", f"entw:{eid}"), ("🔗 التطابقات", f"entm:{eid}")]]
        if user.is_admin:
            rows.append([("✂️ فك الدمج", f"entu:{eid}")])
        parts = chunk_text(profile_text(v), self.cfg.telegram_message_limit)
        if len(parts) == 1:
            return self.show(chat, mid, parts[0], kb(*rows, nav(back or None)))
        for p in parts[:-1]:
            self.api.send_message(chat, p)
        self.send(chat, parts[-1], kb(*rows, nav(back or None)))

    @staticmethod
    def _src_line(s: dict) -> str:
        return f"{(s['archive'] + ' → ') if s['archive'] else ''}{s['file']} / {s['locator']}"

    def entity_matches(self, chat, mid, eid):
        v = entities.entity_view(self.db, eid)
        rows = self.db.execute(
            "SELECT * FROM entity_matches WHERE record_a IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1) OR record_b IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1) ORDER BY confidence DESC LIMIT 15", (eid, eid)).fetchall()
        text = f"🔗 Matches {entities.public_id(eid)}\n" + ("\n".join(f"#{r['id']} {entities.STATE_LABEL[r['state']]} {r['confidence']:.2f} [{r['decision'] or 'pending'}] ({r['record_a']}↔{r['record_b']})" for r in rows) or "لا تطابقات (مصدر واحد)")
        self.show(chat, mid, text, kb(nav(f"ent:{eid}")))

    def entity_unmerge(self, user, chat, mid, eid, confirm):
        if not confirm:
            return self.show(chat, mid, f"⚠️ فك دمج {entities.public_id(eid)}؟ كل سجلاته الخام تبقى؛ يُسجَّل NEVER MATCH بينها.", kb([("✂️ تأكيد فك الدمج", f"entu:{eid}:y")], nav(f"ent:{eid}")))
        try:
            out = entities.unmerge(self.db, eid, user.telegram_id)
        except ValueError as exc:
            return self.show(chat, mid, f"⚠️ {exc}", kb(nav(f"ent:{eid}")))
        self.show(chat, mid, f"✅ تم فك الدمج (entities جديدة: {out.get('entities_created', 0)}). لا فقدان للسجلات الخام.", kb(nav()))
