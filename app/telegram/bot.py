"""Telegram control panel. Telegram is ONLY UI / intake / delivery - never storage or search."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import audit, maintenance
from ..ai.planner import AIQueryPlanner
from ..ai.provider import OpenRouterProvider
from ..auth import User, get_user
from ..errors import IngestError, InvalidQuery, PermissionDenied, SearchTimeout
from ..health import format_checks, run_checks
from ..norm.dates import parse_date
from ..search.dsl import Filter, SearchQuery, Term, parse_quick
from ..search.present import iter_results, mask_for
from ..service import App
from ..storage import sanitize_filename
from ..access.force_sub import ForceSubscription
from ..search.present import iter_groups
from .admin import ADMIN_HEADS, AdminMixin
from .api import TelegramAPI, TelegramError
from .ui import LINE, STATUS_ICON, ago, chunk_text, fmt_int, fmt_size, job_line, kb, nav, pager, result_text, unified_text

log = logging.getLogger("pdb.bot")
DENIED = "⛔ Access Denied"
NOPERM = "⛔ الصلاحية غير كافية"
# Phase 10: user-facing hints for job failures. Only fixed strings are ever
# shown; the raw job error stays in job_events / logs (never in the notify).
_TRANSIENT_HINT_PATTERNS = ("locked", "timed out", "timeout", "ssl", "connection", "reset by peer")


def transient_hint(error: str | None) -> str:
    e = (error or "").lower()
    if any(p in e for p in _TRANSIENT_HINT_PATTERNS):
        return " (سبب محتمل: مشكلة مؤقتة — أعد المحاولة من تفاصيل المهمة.)"
    return ""
FILTERS = {"a": ("الكل", None), "c": ("🟢 مكتمل", ("indexed",)), "p": ("🟡 جزئي", ("partial",)), "f": ("🔴 فاشل", ("failed", "quarantined")),
           "w": ("⏳ معلّق", ("received", "queued", "processing")), "u": ("⚪ غير مدعوم", ("unsupported",))}
FMT = {"h": "html", "t": "txt", "p": "pdf"}
LVL = {"r": "results", "e": "evidence", "m": "metadata"}
# Export styles (2026-10-27, additive): "A" = احترافي (default), "B" = جدولي.
# The style picker uses new callbacks (exs:...) only; exf keeps its old meaning.
STY = {"A": "احترافي", "B": "جدولي"}
ROLE_BTN = [("الاسم", "name"), ("الهاتف", "phone"), ("الإيميل", "email"), ("الهوية", "id"), ("التاريخ", "date"), ("أي حقل", "any")]
MODE_AR = {"exact": "تطابق تام", "partial": "تطابق جزئي", "fuzzy": "تطابق مرن"}


class Bot(AdminMixin):
    def __init__(self, app: App, api: TelegramAPI, planner: AIQueryPlanner | None = None) -> None:
        self.app, self.api, self.cfg = app, api, app.cfg
        self.planner = planner or AIQueryPlanner(OpenRouterProvider(app.cfg), app.cfg)
        self.state: dict[int, dict] = {}
        self.fs = ForceSubscription(lambda: self.app.db(), api, app.cfg.force_sub_cache_sec)
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg")
        self._locks: dict[int, threading.Lock] = {}
        self._denied_at: dict[int, float] = {}
        app.queue.add_listener(self._on_event)
        app.queue.register("master_excel", self.handle_master_job)
        app.notify.sender = lambda uid, text: self.api.send_message(uid, text)
        app.notify.gate = lambda u: self.fs.gate(u).allowed

    @property
    def db(self):
        return self.app.db()

    # ------------------------------------------------------------------ transport
    def run(self) -> None:
        offset = int((self.db.execute("SELECT value FROM settings WHERE key='tg_offset'").fetchone() or [0])[0])
        backoff = 1.0
        log.info("bot polling started")
        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(offset, self.cfg.telegram_poll_timeout)
                backoff = 1.0
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                self.stop_event.wait(exc.retry_after or backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                uid = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {}).get("id", 0)
                self.pool.submit(self._guard, u, uid)
            if updates:
                try:
                    self.db.execute("INSERT INTO settings(key,value) VALUES('tg_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                except Exception as exc:
                    # Never let a persist failure kill the poll loop (20k incident).
                    # Offset is monotonic and held in memory; the next successful
                    # batch persists the newer value, covering this gap.
                    log.warning("tg_offset persist failed (will retry next batch): %s", exc)

    def stop(self) -> None:
        self.stop_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _guard(self, u: dict, chat: int) -> None:
        lock = self._locks.setdefault(chat, threading.Lock())
        with lock:
            try:
                self.handle_update(u)
            except PermissionDenied:
                self._reply_any(u, NOPERM)
            except SearchTimeout as exc:
                # User-facing by design: the message already says what happened.
                log.warning("update search timeout: %s", exc)
                self._reply_any(u, f"❌ {exc}")
            except sqlite3.OperationalError as exc:
                log.exception("update failed")
                if "locked" in str(exc):
                    log.warning("db locked, queued silently")
                else:
                    self._reply_any(u, "❌ حدث خطأ أثناء المعالجة.")
            except TelegramError:
                log.exception("update failed")
                self._reply_any(u, "❌ تعذر الاتصال بتلغرام مؤقتاً — أعد المحاولة.")
            except FileNotFoundError:
                log.exception("update failed")
                self._reply_any(u, "❌ الملف المطلوب غير موجود.")
            except Exception as exc:
                log.exception("update failed")
                msg = "❌ حدث خطأ أثناء المعالجة."
                uid = self._uid(u)
                if uid and self.state.get(uid, {}).get("dev"):
                    msg += f"\n{type(exc).__name__}: {str(exc)[:200]}"
                self._reply_any(u, msg)

    def _uid(self, u: dict) -> int:
        return (u.get("message") or u.get("callback_query") or {}).get("from", {}).get("id", 0)

    def _reply_any(self, u: dict, text: str) -> None:
        chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {}).get("id")
        if chat:
            try:
                self.api.send_message(chat, text, kb(nav()))
            except TelegramError:
                pass

    # ------------------------------------------------------------------ output helpers
    def send(self, chat: int, text: str, markup: dict | None = None) -> None:
        parts = chunk_text(text, self.cfg.telegram_message_limit)
        for i, p in enumerate(parts):
            self.api.send_message(chat, p, markup if i == len(parts) - 1 else None)

    def show(self, chat: int, mid: int | None, text: str, markup: dict | None = None) -> None:
        if mid and len(text) <= self.cfg.telegram_message_limit:
            try:
                self.api.edit_message_text(chat, mid, text, markup)
                return
            except TelegramError as exc:
                if "not modified" in exc.description:
                    return
        self.send(chat, text, markup)

    def new_state(self, uid: int, payload: dict) -> int:
        return self.db.execute("INSERT INTO callback_state(user_id, payload) VALUES (?,?)", (uid, json.dumps(payload))).lastrowid

    def get_state(self, sid: int, uid: int) -> dict | None:
        r = self.db.execute("SELECT payload FROM callback_state WHERE id=? AND user_id=? AND created_at > strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
                            (sid, uid, f"-{self.cfg.callback_ttl_min} minutes")).fetchone()
        return json.loads(r[0]) if r else None

    def _deny(self, user_id: int, what: str, ident=None) -> None:
        audit.log(self.db, user_id, audit.DENIED, what, ident, {"reason": "ownership/forged/expired"})

    def own_search(self, user: User, sid: int):
        """IDOR guard: a search is only visible to the user who created it."""
        row = self.db.execute("SELECT * FROM search_history WHERE id=? AND user_id=?", (sid, user.telegram_id)).fetchone()
        if row is None:
            self._deny(user.telegram_id, "search", sid)
        return row

    def own_job(self, user: User, jid: int):
        j = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if j is None or not (user.can("manage") or j["created_by"] == user.telegram_id):
            self._deny(user.telegram_id, "job", jid)
            return None
        return j

    def notify(self, uid: int | None, text: str, markup: dict | None = None) -> None:
        targets = [uid] if uid else [r[0] for r in self.db.execute("SELECT telegram_id FROM users WHERE role='admin' AND enabled=1")]
        for t in targets:
            try:
                self.send(t, text, markup)
            except TelegramError as exc:
                log.warning("notify failed: %s", exc)

    # ------------------------------------------------------------------ dispatch
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            user = self._auth(cq["from"]["id"], cq["message"]["chat"]["id"])
            if user is None:
                self.api.answer_callback(cq["id"])
                return
            try:
                self.api.answer_callback(cq["id"])
            except TelegramError:
                pass
            data = cq.get("data")
            if data == "fsc":   # زر "تحقق من الاشتراك": يُسمح به لغير المشتركين
                return self.fs_check(user, cq["message"]["chat"]["id"], cq["message"]["message_id"])
            if not self.fs_allowed(user, cq["message"]["chat"]["id"], cq["message"]["message_id"]):
                return
            if not isinstance(data, str) or len(data.encode()) > 64 or not data.isprintable():
                self._deny(user.telegram_id, "callback", str(data)[:40])
                return
            try:
                self.on_callback(user, cq["message"]["chat"]["id"], cq["message"]["message_id"], data)
            except (ValueError, IndexError, KeyError):
                self._deny(user.telegram_id, "callback", data)
                self.send(cq["message"]["chat"]["id"], "⛔ طلب غير صالح", kb(nav()))
        elif "message" in u:
            m = u["message"]
            user = self._auth(m["from"]["id"], m["chat"]["id"])
            if user is None:
                return
            if not self.fs_allowed(user, m["chat"]["id"], None):
                return
            if "document" in m:
                self.on_document(user, m)
            elif "text" in m:
                self.on_text(user, m["chat"]["id"], m["text"])

    def _auth(self, uid: int, chat: int) -> User | None:
        user = self.app.authorize(uid)
        if user is None:
            now = time.time()
            if now - self._denied_at.get(uid, 0) > 60:
                self._denied_at[uid] = now
                audit.log(self.db, uid, audit.DENIED, "telegram", uid)
                try:
                    self.api.send_message(chat, DENIED)
                except TelegramError:
                    pass
        return user

    # ------------------------------------------------------------------ force subscription gate
    def fs_allowed(self, user: User, chat: int, mid: int | None) -> bool:
        res = self.fs.gate(user)
        if res.allowed:
            return True
        audit.log(self.db, user.telegram_id, audit.DENIED, "force_subscription", None, {"missing": len(res.missing), "error": bool(res.error)})
        self.fs_show_gate(chat, mid, res)
        return False

    def fs_show_gate(self, chat: int, mid: int | None, res) -> None:
        rows = []
        for ch in res.missing:
            url = ch.join_url
            rows.append([{"text": f"📢 {ch.title}", "url": url}] if url else [{"text": f"📢 {ch.title} ({ch.chat_ref})", "callback_data": "fsc"}])
        rows.append([{"text": "✅ تحقق من الاشتراك", "callback_data": "fsc"}])
        text = "⛔ يجب الاشتراك بالقنوات التالية لاستخدام البوت" + (f"\n\n{res.error}" if res.error else "")
        if mid:
            try:
                self.api.edit_message_text(chat, mid, text, {"inline_keyboard": rows})
                return
            except TelegramError:
                pass
        self.api.send_message(chat, text, {"inline_keyboard": rows})

    def fs_check(self, user: User, chat: int, mid: int) -> None:
        res = self.fs.gate(user, force_refresh=True)
        if res.allowed:
            return self.home(chat, mid, user)
        self.fs_show_gate(chat, mid, res)

    # ------------------------------------------------------------------ text
    def on_text(self, user: User, chat: int, text: str) -> None:
        st = self.state.setdefault(user.telegram_id, {})
        text = text.strip()
        if text.startswith("/"):
            st.pop("await", None)
            return self.command(user, chat, text)
        # New-buttons pending text (additive; returns False if none pending)
        from . import newbuttons as _nb_text
        if _nb_text.nb_handle_text(self, user, chat, text):
            return
        aw = st.pop("await", None)
        if aw in ("usr_add", "usr_s", "sch_add", "fs_add", "nt_add") or (aw or "").startswith("fs_edit:"):
            if not user.is_admin:
                self._deny(user.telegram_id, "admin_text", aw)
                return self.send(chat, DENIED)
            if aw == "sch_add":
                return self.sch_text(user, chat, text)
            if aw == "nt_add":
                return self.nt_text(user, chat, text)
            if aw == "fs_add" or aw.startswith("fs_edit:"):
                return self.fs_text(user, chat, aw, text)
            return self.users_text(user, chat, aw, text)
        if aw == "quick":
            user.require("search")
            self.run_search(user, chat, None, parse_quick(text), "quick")
        elif aw == "ai":
            user.require("smart")
            plan = self.planner.plan(text)
            sid = self.new_state(user.telegram_id, {"q": plan.query.to_json(), "text": text})
            note = f"\n⚠️ {plan.note}" if plan.note else ""
            body = "🧠 فهمت طلبك كالتالي:\n" + "\n".join(plan.query.explain()) + note
            self.send(chat, body, kb([("🔎 تنفيذ", f"aic:{sid}:run"), ("✏️ تعديل", "a")], nav("h")))
        elif aw and aw.startswith("adv:"):
            self.adv_value(user, chat, aw.split(":", 1)[1], text)
        else:
            self.home(chat, None, user)

    def command(self, user: User, chat: int, text: str) -> None:
        cmd = text.split()[0].split("@")[0].lower()
        st = self.state.setdefault(user.telegram_id, {})
        if cmd == "/admin":  # لا يظهر للمستخدمين، ولو كُتب يدوياً: Access Denied بلا تنفيذ
            if not user.is_admin:
                self._deny(user.telegram_id, "command", "/admin")
                return self.send(chat, DENIED)
            return self.admin_panel(chat, None)
        need = {"/search": "search", "/smart": "smart", "/sources": "sources", "/reports": "reports"}.get(cmd)
        if need and not user.can(need):
            return self.send(chat, NOPERM, kb(nav()))
        if cmd == "/search":
            st["await"] = "quick"
            return self.send(chat, "⚡ أرسل استعلام البحث:", kb(nav()))
        if cmd == "/smart":
            st["await"] = "ai"
            return self.send(chat, "🧠 اكتب طلبك بلغتك:", kb(nav()))
        if cmd == "/sources":
            return self.sources(user, chat, None, "a", 0)
        if cmd == "/tasks":
            return self.jobs(user, chat, None, 0)
        if cmd == "/reports":
            return self.reports(user, chat, None, 0)
        if cmd == "/help":
            return self.send(chat, "/start القائمة • /search بحث • /smart بحث ذكي • /sources المصادر • /tasks المهام • /reports التقارير" + ("\n/admin لوحة الادارة" if user.is_admin else ""), kb(nav()))
        self.home(chat, None, user)

    def publish_commands(self) -> None:
        """Command menu: users get the basic list; each ADMIN chat gets /admin too. (Menu hiding is cosmetic; /admin is protected server-side.)"""
        base = [("start", "القائمة الرئيسية"), ("search", "بحث"), ("smart", "بحث ذكي"), ("sources", "المصادر"), ("tasks", "المهام"), ("reports", "التقارير"), ("help", "مساعدة")]
        try:
            self.api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in base]})
            for aid in self.cfg.authorized_user_ids:
                self.api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in base + [("admin", "لوحة الادارة")]], "scope": {"type": "chat", "chat_id": aid}})
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc)

    # ------------------------------------------------------------------ files
    def on_document(self, user: User, m: dict) -> None:
        user.require("upload")
        chat, doc = m["chat"]["id"], m["document"]
        name = sanitize_filename(doc.get("file_name") or "file")
        size = doc.get("file_size", 0)
        inc = self.app.storage.dir("incoming")
        if size > self.cfg.telegram_download_limit_mb * 1024 * 1024:
            # MTProto bridge: oversized files from an authorized account
            # are fetched via the owner's account instead of rejected
            # (Bot API getFile refuses >20MB on the hosted API). Other
            # senders keep the original rejection below. Never print
            # on-device absolute paths to users (audit fix Phase 6 5.7).
            from ..mtproto.pending import create_pending, decide_document
            if decide_document(user.telegram_id, self.cfg.authorized_user_ids, size, self.cfg.telegram_download_limit_mb) == "bridge":
                create_pending(self.db, user.telegram_id, chat, m.get("message_id"), name, size, doc.get("file_id"))
                self.send(chat, f"📥 الملف ({fmt_size(size)}) اكبر من حد تنزيل Telegram Bot ({self.cfg.telegram_download_limit_mb}MB).\n"
                                "سأجلبه عبر حسابك الشخصي بدلاً من ذلك، وسيلتقطه النظام تلقائياً بعد اكتمال التنزيل.", kb(nav()))
                return
            self.send(chat, f"⚠️ الملف ({fmt_size(size)}) اكبر من حد تنزيل Telegram Bot ({self.cfg.telegram_download_limit_mb}MB).\n"
                            "انسخه مباشرة الى مجلد incoming داخل مجلد التطبيق،\nوسيلتقطه النظام تلقائياً بعد ما يكتمل نسخه.", kb(nav()))
            return
        tmp = self.app.storage.dir("temporary") / f"tg_{uuid.uuid4().hex}_{name}"
        try:
            info = self.api.get_file(doc["file_id"])
            self.api.download(info["file_path"], tmp, self.cfg.telegram_download_limit_mb * 1024 * 1024 + 1024)
        except TelegramError as exc:
            tmp.unlink(missing_ok=True)
            self.send(chat, f"❌ تعذر تنزيل الملف: {exc.description}", kb(nav()))
            return
        self.after_intake(user, chat, tmp, name, False)

    def after_intake(self, user: User, chat: int, path: Path, name: str, from_watcher: bool) -> None:
        try:
            res = self.app.ingest_path(path, user.telegram_id, original_name=name, scope=self.active_scope())
        except IngestError as exc:
            # Audit fix (Phase 5 A28): intake refusals (e.g. gapped multipart)
            # must reach the user as a readable message, not a silent failure.
            self.send(chat, f"❌ لم يُستلم الملف: {exc}", kb(nav()))
            return
        if res.status == "duplicate":
            sid = self.new_state(user.telegram_id, {"path": str(path), "name": name, "dup": res.duplicate_of, "w": from_watcher})
            self.send(chat, f"♻️ Duplicate File\nهذا الملف موجود مسبقاً (File #{res.duplicate_of}).",
                      kb([("استخدام الموجود", f"dup:{sid}:u")], [("الاحتفاظ بنسخة منفصلة", f"dup:{sid}:k")], [("إلغاء", f"dup:{sid}:c")]))
            return
        self.send(chat, f"📥 تم الاستلام\nFile:\n{res.name}\nSize:\n{fmt_size(res.size)}\nStatus:\n⏳ Queued", kb(nav()))

    # ------------------------------------------------------------------ callbacks
    def on_callback(self, user: User, chat: int, mid: int, data: str) -> None:
        uid = user.telegram_id
        head, *rest = data.split(":")
        st = self.state.setdefault(uid, {})
        if head in ADMIN_HEADS and not user.is_admin:  # كل callback ادمن يُفحص هنا server-side
            self._deny(uid, "admin_callback", data[:40])
            return self.show(chat, mid, DENIED, kb(nav()))
        if head == "ap":
            return self.admin_panel(chat, mid)
        if head == "usr":
            return self.users_cb(user, chat, mid, rest)
        if head == "fs":
            return self.fs_cb(user, chat, mid, rest)
        if head == "nt":
            return self.nt_cb(user, chat, mid, rest)
        if head == "er":
            return self.er_cb(user, chat, mid, rest)
        if head == "sch":
            return self.sch_cb(user, chat, mid, rest)
        if head == "bk":
            return self.backup_cb(user, chat, mid, rest)
        if head == "ai":
            return self.ai_screen(chat, mid)
        if head == "mt":
            return self.maint_cb(user, chat, mid, rest)
        if head in ("ent", "entw", "entm", "entu"):
            user.require("search")
            eid = int(rest[0])
            if head == "ent":
                return self.entity_screen(user, chat, mid, eid, f"res:{rest[1]}:{rest[2]}" if len(rest) >= 3 else "")
            if head == "entw":
                from ..er import entities as _e
                return self.show(chat, mid, _e.why(self.db, eid), kb(nav(f"ent:{eid}")))
            if head == "entm":
                return self.entity_matches(chat, mid, eid)
            return self.entity_unmerge(user, chat, mid, eid, len(rest) > 1 and rest[1] == "y")
        if head == "nb":
            from . import newbuttons as _nb
            if _nb.nb_dispatch(self, user, chat, mid, data):
                return
        if head == "h":
            st.pop("await", None)
            self.home(chat, mid, user)
        elif head == "sm":
            user.require("search")
            self.show(chat, mid, "🔎 البحث", kb([("⚡ بحث سريع", "q"), ("🔎 بحث متقدم", "adv")], nav()))
        elif head == "q":
            user.require("search")
            st["await"] = "quick"
            self.show(chat, mid, "⚡ بحث سريع\nأرسل استعلام البحث.\nمثلاً:\nمحمد\n0770...\nexample@email.com\nمحمد 1998", kb(nav("sm")))
        elif head == "a":
            user.require("smart")
            st["await"] = "ai"
            off = "" if self.cfg.ai_enabled else "\n⚠️ AI معطّل حالياً؛ سيُستخدم فهم بسيط للنص."
            self.show(chat, mid, "🧠 البحث الذكي\nاكتب طلبك بلغتك، مثال:\nدورلي على احمد مواليد 1998 والاسم ممكن مكتوب غلط" + off, kb(nav()))
        elif head == "aic":
            s = self.get_state(int(rest[0]), uid)
            if s and rest[1] == "run":
                self.run_search(user, chat, mid, SearchQuery.from_json(s["q"]), "ai")
        elif head == "adv":
            # Audit fix (Phase 4 gap): the menu, field prompts and value entry
            # used to open ungated for no-perm users (NOPERM only at execution).
            # The search menu ('sm') already requires 'search'; adv matches it.
            user.require("search")
            self.adv(user, chat, mid, rest)
        elif head == "add":
            user.require("upload")
            fmts = "Archives (zip/7z/rar/tar/gz/bz2/xz + multipart .001)، CSV، JSON، JSONL، SQL، SQLite، XLSX، XLS، DOC، MDB، ACCDB، TXT، PDF"
            self.show(chat, mid, f"⬆️ إضافة ملفات\n📥 أرسل الملف الآن (حتى {self.cfg.telegram_download_limit_mb}MB عبر Telegram).\nالصيغ المدعومة:\n{fmts}\n\n"
                                 f"للملفات الكبيرة (حتى ~4GB) انسخها الى:\n{self.app.storage.dir('incoming')}", kb(nav()))
        elif head == "dup":
            self.dup(user, chat, mid, int(rest[0]), rest[1])
        elif head == "src":
            user.require("sources")
            self.sources(user, chat, mid, rest[0], int(rest[1]))
        elif head == "sd":
            user.require("sources")
            self.source_detail(user, chat, mid, int(rest[0]), rest[1] if len(rest) > 1 else "a")
        elif head == "sdr":
            user.require("manage")
            self.reindex(user, chat, mid, int(rest[0]))
        elif head == "sdh":
            # Audit fix (Phase 6 S1): was ungated (only button visibility hid
            # it); a forged press revealed file existence + hash status.
            user.require("sources")
            self.check_hash(user, chat, mid, int(rest[0]))
        elif head == "st":
            user.require("stats")
            self.stats(chat, mid)
        elif head == "idx":
            # Audit note (Phase 3/21): UNREACHABLE BY DESIGN — no button
            # produces 'idx' (only the back button on its own 'idxr' screen
            # references it). Kept, admin-gated, for forged-callback
            # completeness; do NOT delete the handler.
            user.require("manage")
            self.indexing(user, chat, mid)
        elif head == "idxr":
            user.require("manage")
            n = self.retry_failed(user)
            self.show(chat, mid, f"🔄 أُعيدت المحاولة لـ {n} ملف", kb(nav("idx")))
        elif head == "scope":
            user.require("manage")
            self.toggle_scope(user, chat, mid)
        elif head == "jobs":
            self.jobs(user, chat, mid, int(rest[0]))
        elif head == "jd":
            self.job_detail(user, chat, mid, int(rest[0]))
        elif head == "jc":
            user.require("manage")
            if rest[1] not in ("pause", "resume", "cancel", "retry") or self.own_job(user, int(rest[0])) is None:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            new = self.app.queue.control(int(rest[0]), rest[1])
            audit.log(self.db, user.telegram_id, audit.JOB, "job", rest[0], {"action": rest[1]})
            self.job_detail(user, chat, mid, int(rest[0]), f"→ {new}")
        elif head == "rep":
            user.require("reports")
            self.reports(user, chat, mid, int(rest[0]))
        elif head == "rs":
            user.require("reports")
            self.send_report(user, chat, int(rest[0]))
        elif head == "log":
            user.require("manage")
            self.audit_screen(chat, mid, int(rest[0]))
        elif head == "set":
            self.settings(user, chat, mid)
        elif head == "setp":
            # Audit fix (Phase 6 S2): whitelist to the rendered buttons only;
            # an arbitrary value (e.g. 'abc') used to poison every later
            # results/settings screen for that user (int() crash).
            if rest[0] not in ("10", "25", "50", "100"):
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"pagesize:{user.telegram_id}", rest[0]))
            self.settings(user, chat, mid)
        elif head == "dev":
            user.require("manage")
            st["dev"] = not st.get("dev", False)
            self.settings(user, chat, mid)
        elif head == "clean":
            user.require("manage")
            self.clean(user, chat, mid, rest[0])
        elif head == "diag":
            user.require("manage")
            # Audit fix (Phase 4 gap): the back target used to be always 'set',
            # even when Diagnostics was opened from the admin panel. Buttons
            # now carry their origin (diag:set / diag:ap); bare 'diag' keeps
            # the old default for already-sent buttons.
            back = rest[0] if rest and rest[0] in ("set", "ap") else "set"
            self.show(chat, mid, "⚙️ Diagnostics\n" + format_checks(run_checks(self.cfg)) + "\nWorkers: " + str(self.cfg.max_workers), kb(nav(back)))
        elif head == "res":
            self.results(user, chat, mid, int(rest[0]), int(rest[1]))
        elif head == "resz":
            if rest[1] not in ("10", "25", "50", "100") or self.own_search(user, int(rest[0])) is None:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"pagesize:{uid}", rest[1]))
            self.results(user, chat, mid, int(rest[0]), 0)
        elif head == "why":
            self.why(user, chat, int(rest[0]), int(rest[1]))
        elif head == "ex":
            user.require("export")
            sid = rest[0]
            if self.own_search(user, int(sid)) is None:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            self.show(chat, mid, "اختر محتوى التقرير:", kb([("📋 النتائج فقط", f"exl:{sid}:r")], [("🔍 النتائج + الأدلة", f"exl:{sid}:e")], [("🗂 النتائج + البيانات الوصفية", f"exl:{sid}:m")], nav(f"sum:{sid}")))
        elif head == "exl":
            user.require("export")
            sid, lv = rest
            if lv not in LVL or self.own_search(user, int(sid)) is None:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            self.show(chat, mid, "📤 Export", kb([("🌐 HTML", f"exs:{sid}:{lv}:h"), ("📄 TXT", f"exs:{sid}:{lv}:t"), ("📕 PDF", f"exs:{sid}:{lv}:p")], [("📦 الكل", f"exs:{sid}:{lv}:a")], nav(f"ex:{sid}")))
        elif head == "exs":
            # Export style picker (2026-10-27, additive). 3 parts = pick a style,
            # 4 parts = submit with that style. Unknown style -> default A.
            user.require("export")
            if len(rest) == 3:
                sid, lv, f = rest
                if lv not in LVL or f not in ("h", "t", "p", "a") or self.own_search(user, int(sid)) is None:
                    return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
                self.show(chat, mid, "🎨 اختر شكل التقرير:", kb([("احترافي", f"exs:{sid}:{lv}:{f}:A")], [("جدولي", f"exs:{sid}:{lv}:{f}:B")], nav(f"exl:{sid}:{lv}")))
            elif len(rest) == 4:
                sid, lv, f, style = rest
                if lv not in LVL or f not in ("h", "t", "p", "a") or style not in STY or self.own_search(user, int(sid)) is None:
                    return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
                fmts = list(FMT.values()) if f == "a" else [FMT[f]]
                for _row in self.db.execute("SELECT id, payload FROM jobs WHERE kind='export' AND status IN ('queued','running') AND created_by=?", (user.telegram_id,)):
                    try:
                        _p = json.loads(_row["payload"] or "{}")
                    except Exception:
                        continue
                    if int(_p.get("search_id", -1)) == int(sid) and _p.get("level") == LVL[lv] and sorted(_p.get("formats") or []) == sorted(fmts) and _p.get("style", "A") == style:
                        return self.show(chat, mid, f"⏳ تقرير قيد الإنشاء بالفعل\nJOB-{_row['id']:06d}\n(ستصلك الملفات عند الانتهاء)", kb(nav()))
                jid = self.app.reports.request(user, int(sid), fmts, LVL[lv], style)
                self.show(chat, mid, f"⏳ جاري إنشاء التقرير ({STY[style]})\nJOB-{jid:06d}\n(ستصلك الملفات عند الانتهاء)", kb(nav()))
            else:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
        elif head == "exf":
            # Legacy submit path (pre-style buttons still in old messages):
            # behavior unchanged — submits with the default style A.
            sid, lv, f = rest
            if lv not in LVL or f not in ("h", "t", "p", "a") or self.own_search(user, int(sid)) is None:
                return self.show(chat, mid, "⛔ طلب غير صالح", kb(nav()))
            fmts = list(FMT.values()) if f == "a" else [FMT[f]]
            # Audit fix (Phase 4 F3): dedup like er:run/master — a pending
            # export job for the same (search, level, formats) by this user is
            # reported instead of duplicated (rapid repeat presses used to
            # create duplicate jobs AND duplicate deliveries).
            # Style-aware (2026-10-27): exf submits style A, so it only dedups
            # against pending style-A jobs; a pending style-B job is different.
            for _row in self.db.execute("SELECT id, payload FROM jobs WHERE kind='export' AND status IN ('queued','running') AND created_by=?", (user.telegram_id,)):
                try:
                    _p = json.loads(_row["payload"] or "{}")
                except Exception:
                    continue
                if int(_p.get("search_id", -1)) == int(sid) and _p.get("level") == LVL[lv] and sorted(_p.get("formats") or []) == sorted(fmts) and _p.get("style", "A") == "A":
                    return self.show(chat, mid, f"⏳ تقرير قيد الإنشاء بالفعل\nJOB-{_row['id']:06d}\n(ستصلك الملفات عند الانتهاء)", kb(nav()))
            jid = self.app.reports.request(user, int(sid), fmts, LVL[lv])
            self.show(chat, mid, f"⏳ جاري إنشاء التقرير\nJOB-{jid:06d}\n(ستصلك الملفات عند الانتهاء)", kb(nav()))
        elif head == "master":
            # Audit fix (Phase 6 5.5, P1): was gated only by 'export', so any
            # export holder could dump EVERY record in the database in one
            # press, including data they could never search. Admin-only now.
            user.require("manage")
            busy = self.db.execute("SELECT id FROM jobs WHERE kind='master_excel' AND status IN ('queued','running')").fetchone()
            if busy:
                self.show(chat, mid, f"⏳ دمج سابق قيد التنفيذ (JOB-{busy['id']:06d}) — الدمج يجري على أجزاء ويتقدم مع الطابور، انتظر وصول الملفات.", kb(nav()))
            else:
                jid = self.app.queue.submit("master_excel", None, {}, user.telegram_id)
                self.show(chat, mid, f"⏳ جاري دمج كل الملفات إلى Excel...\nJOB-{jid:06d}\nستصلك الملفات هنا عند الانتهاء (مقسمة على أجزاء إن تجاوز حجمها حد تيليجرام).", kb(nav()))
        elif head == "sum":
            self.summary(user, chat, mid, int(rest[0]))
        else:
            self.home(chat, mid, user)

    # ------------------------------------------------------------------ screens
    # ------------------------------------------------------------------ master excel job
    def handle_master_job(self, ctx) -> None:
        """Build the cumulative Master Excel (streaming, split delivery) and send it."""
        from ..reports.master import run_master_job

        run_master_job(ctx, self.api, self.app.storage.dir("reports"))

    def home(self, chat: int, mid: int | None, user: User | None = None) -> None:
        sc = self.active_scope()
        r = self.db.execute("SELECT (SELECT COUNT(*) FROM files WHERE parent_file_id IS NULL AND scope=?) f, "
                            "(SELECT COALESCE(SUM(record_count),0) FROM files WHERE status IN ('indexed','partial') AND detected_type NOT IN ('zip','7z','rar','gzip','bzip2','xz','tar') AND scope=?) r", (sc, sc)).fetchone()
        text = (f"🗄 PRIVATE DATABASE\nFiles: {fmt_int(r['f'])}\nRecords: {fmt_int(r['r'])}\nStatus: 🟢 ONLINE\nالنطاق: {'عمل' if sc == 'work' else 'رئيسي'}")
        can = (lambda p: user.can(p)) if user else (lambda p: False)
        rows, row = [], []
        for label, cb, perm in (("🔎 البحث", "sm", "search"), ("📁 المصادر", "src:a:0", "sources"), ("📊 الإحصائيات", "st", "stats"),
                                ("📋 المهام", "jobs:0", None), ("📤 التقارير", "rep:0", "reports"), ("⬆️ إضافة ملفات", "add", "upload"), ("🗂 دمج الملفات (Excel)", "master", "manage")):
            if perm is None or can(perm):
                row.append((label, cb))
                if len(row) == 2:
                    rows.append(row); row = []
        if row:
            rows.append(row)
        if user and user.is_admin:
            text += f"\n{LINE}\n⚙️ ADMIN PANEL"
            rows.append([("⚙️ لوحة الأدمن", "ap")])
        if can("manage"):
            rows.append([(f"🔀 النطاق: {'عمل' if sc == 'work' else 'رئيسي'} (اضغط للتبديل)", "scope")])
        self.show(chat, mid, text, kb(*rows))

    def page_size(self, uid: int) -> int:
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (f"pagesize:{uid}",)).fetchone()
        if not r:
            return self.cfg.result_page_size
        try:
            return int(r[0])
        except (ValueError, TypeError):
            # Audit fix (Phase 6 S2 defense): a value poisoned before the
            # setp whitelist existed must not break the user's screens.
            return self.cfg.result_page_size

    def active_scope(self) -> str:
        r = self.db.execute("SELECT value FROM settings WHERE key='active_scope'").fetchone()
        return r[0] if r and r[0] in ("main", "work") else "main"

    def toggle_scope(self, user: User, chat: int, mid: int) -> None:
        new = "work" if self.active_scope() == "main" else "main"
        self.db.execute("INSERT INTO settings(key,value) VALUES('active_scope',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new,))
        self.home(chat, mid, user)

    def run_search(self, user: User, chat: int, mid: int | None, q: SearchQuery, mode: str) -> None:
        user.require("search")
        q.scope = self.active_scope()
        try:
            s = self.app.search(q, user.telegram_id, mode)
        except SearchTimeout as exc:
            self.send(chat, f"⏱ {exc}\nضيّق البحث (اضف كلمة/سنة) وجرب مرة ثانية.", kb(nav()))
            return
        except InvalidQuery as exc:
            self.send(chat, f"⚠️ {exc}", kb(nav()))
            return
        self.summary(user, chat, mid, s.search_id)

    def summary(self, user: User, chat: int, mid: int | None, sid: int) -> None:
        row = self.own_search(user, sid)
        if row is None:
            self.show(chat, mid, "البحث منتهي الصلاحية (retention).", kb(nav()))
            return
        q = SearchQuery.from_json(row["query_json"])
        c = self.db.execute("SELECT COUNT(DISTINCT COALESCE(f.parent_file_id, f.id)) s, COUNT(DISTINCT f.id) n FROM search_hits h JOIN records r ON r.id=h.record_id "
                            "JOIN files f ON f.id=r.file_id WHERE h.search_id=?", (sid,)).fetchone()
        groups = self.db.execute("SELECT COUNT(*), COUNT(entity_id) FROM search_groups WHERE search_id=?", (sid,)).fetchone()
        text = (f"🔎 SEARCH COMPLETE\nQuery:\n" + "\n".join(q.explain()) + f"\n{LINE}\nResults:\n{fmt_int(groups[0])}\n(unified entities: {fmt_int(groups[1])} | matched records: {fmt_int(row['result_count'])})\nSources:\n{c['s']}\nFiles:\n{c['n']}\n{LINE}")
        rows = [[("👁 عرض النتائج", f"res:{sid}:0")]]
        if user.can("export"):
            rows.append([("📤 تصدير", f"ex:{sid}")])
        self.show(chat, mid, text, kb(*rows, nav()))

    def results(self, user: User, chat: int, mid: int, sid: int, page: int) -> None:
        user.require("search")
        row = self.own_search(user, sid)
        if row is None:
            self.show(chat, mid, "البحث منتهي الصلاحية.", kb(nav()))
            return
        total = self.db.execute("SELECT COUNT(*) FROM search_groups WHERE search_id=?", (sid,)).fetchone()[0]
        size = self.page_size(user.telegram_id)
        pages = max(1, -(-total // size))
        page = max(0, min(page, pages - 1))
        recs = list(iter_groups(self.db, sid, offset=page * size, limit=size))
        self.state.setdefault(user.telegram_id, {})["last_page"] = (sid, page)
        body = ("\n\n".join(unified_text(r, user) for r in recs)) or "لا توجد نتائج."
        if pages > 1:
            body = f"صفحة {page + 1} من {pages}\n\n" + body
        audit.log(self.db, user.telegram_id, audit.OPEN, "search", sid, {"page": page, "count": len(recs)})
        nv = pager(page, pages, f"res:{sid}")
        sizes = [(("✓" if size == n else "") + str(n), f"resz:{sid}:{n}") for n in (10, 25, 50, 100)]
        ent_btns = [(f"📚 {i + 1}", f"ent:{r.entity_id}:{sid}:{page}") for i, r in enumerate(recs) if r.entity_id][:8]
        parts = chunk_text(body, self.cfg.telegram_message_limit)
        for p in parts[:-1]:
            self.api.send_message(chat, p)
        self.send(chat, parts[-1], kb(*([nv] if nv else []), sizes, *([ent_btns] if ent_btns else []), [("🧠 لماذا ظهرت؟", f"why:{sid}:{page}")], [("◀️ الملخص", f"sum:{sid}")] + nav()[:1]))

    def why(self, user: User, chat: int, sid: int, page: int) -> None:
        user.require("search")
        if self.own_search(user, sid) is None:
            self.send(chat, "⛔ طلب غير صالح", kb(nav()))
            return
        from ..er import entities as _e
        size = self.page_size(user.telegram_id)
        lines = ["🧠 لماذا ظهرت النتائج (اعتماداً على الأدلة المخزّنة فقط، محلياً):"]
        for r in iter_groups(self.db, sid, offset=page * size, limit=size):
            base = r.record.why() if r.record else ""
            lines.append(f"#{r.rank}: {base}" + (f"\n   الكيان {r.public_id}: {r.view['state']} ({r.hit_count} سجل مطابق من {len(r.view['record_ids'])})" if r.view else ""))
        self.send(chat, "\n".join(lines), kb(nav(f"res:{sid}:{page}")))

    # ---- advanced search
    def adv(self, user: User, chat: int, mid: int, rest: list[str]) -> None:
        st = self.state.setdefault(user.telegram_id, {})
        a = st.setdefault("adv", {"items": [], "mode": "partial"})
        if rest and rest[0] == "f":
            st["await"] = f"adv:{rest[1]}"
            self.show(chat, mid, f"أرسل قيمة الحقل ({rest[1]}):" + ("\nمثال للتاريخ: 1998 أو 1998-05-10" if rest[1] == "date" else ""), kb(nav("adv")))
            return
        if rest and rest[0] == "m":
            a["mode"] = rest[1]
        elif rest and rest[0] == "clr":
            a["items"], a["mode"] = [], "partial"
        elif rest and rest[0] == "go":
            self.adv_run(user, chat, mid, a)
            return
        cur = "\n".join(f"• {r} = {v}" for r, v in a["items"]) or "(لا شروط بعد)"
        mode = {"exact": "Exact", "partial": "Partial", "fuzzy": "Fuzzy"}[a["mode"]]
        rows = [[(n, f"adv:f:{r}") for n, r in ROLE_BTN[i:i + 3]] for i in (0, 3)]
        rows.append([(("✓ " if a["mode"] == m else "") + MODE_AR[m], f"adv:m:{m}") for m in ("exact", "partial", "fuzzy")])
        rows.append([("🔎 بحث", "adv:go"), ("🗑 مسح", "adv:clr")])
        self.show(chat, mid, f"🔎 Advanced Search\nالشروط:\n{cur}\nالمطابقة: {mode}", kb(*rows, nav("sm")))

    def adv_value(self, user: User, chat: int, role: str, text: str) -> None:
        a = self.state.setdefault(user.telegram_id, {}).setdefault("adv", {"items": [], "mode": "partial"})
        a["items"].append((role, text[:100]))
        self.send(chat, f"✅ أُضيف: {role} = {text[:100]}", kb([("➕ شرط آخر / بحث", "adv")], nav()))

    def adv_run(self, user: User, chat: int, mid: int, a: dict) -> None:
        q = SearchQuery(fuzzy=a["mode"] == "fuzzy", exact=a["mode"] == "exact")
        for role, val in a["items"]:
            if role == "date":
                if val.isdigit() and len(val) == 4:
                    q.filters.append(Filter("date", "year", val))
                elif parse_date(val):
                    q.filters.append(Filter("date", "equals", val))
                else:
                    self.send(chat, "⚠️ صيغة التاريخ غير واضحة/ملتبسة. استخدم YYYY-MM-DD او سنة فقط.", kb(nav("adv")))
                    return
            else:
                q.terms.append(Term(val, "auto", None if role == "any" else role))
        self.run_search(user, chat, mid, q, "advanced")

    # ---- sources
    def sources(self, user: User, chat: int, mid: int, flt: str, page: int) -> None:
        label, statuses = FILTERS[flt]
        sc = self.active_scope()
        sc_label = "عمل" if sc == "work" else "رئيسي"
        where, args = ("parent_file_id IS NULL", [])
        if statuses:
            where += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args = list(statuses)
        where += " AND scope=?"
        args = [*args, sc]
        n = self.db.execute(f"SELECT COUNT(*) FROM files WHERE {where}", args).fetchone()[0]
        pages = max(1, -(-n // 8))
        page = max(0, min(page, pages - 1))
        counts = {k: self.db.execute("SELECT COUNT(*) FROM files WHERE parent_file_id IS NULL AND scope=?" + (" AND status IN (%s)" % ",".join("?" * len(v[1])) if v[1] else ""), [sc, *list(v[1] or [])]).fetchone()[0] for k, v in FILTERS.items()}
        rows = self.db.execute(f"SELECT id, original_name, status, size_bytes FROM files WHERE {where} ORDER BY id DESC LIMIT 8 OFFSET ?", [*args, page * 8]).fetchall()
        text = f"📁 المصادر\nالنطاق: {sc_label}\nTotal: {fmt_int(counts['a'])}\n🟢 Completed: {counts['c']}\n🟡 Partial: {counts['p']}\n🔴 Failed: {counts['f']}\n⏳ Pending: {counts['w']}\nالفلتر: {label}"
        if pages > 1:
            text += f"\nصفحة {page + 1} من {pages}"
        btns = [[(f"{STATUS_ICON.get(r['status'], '•')} {r['original_name'][:28]} ({fmt_size(r['size_bytes'])})", f"sd:{r['id']}:{flt}")] for r in rows]
        nv = pager(page, pages, f"src:{flt}")
        if nv:
            btns.append(nv)
        btns.append([(v[0], f"src:{k}:0") for k, v in list(FILTERS.items())[:3]])
        btns.append([(v[0], f"src:{k}:0") for k, v in list(FILTERS.items())[3:]])
        self.show(chat, mid, text, kb(*btns, nav()))

    def source_detail(self, user: User, chat: int, mid: int, fid: int, back: str) -> None:
        f = self.db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if f is None:
            self.show(chat, mid, "غير موجود", kb(nav()))
            return
        kids = self.db.execute("SELECT status, COUNT(*) c FROM files WHERE parent_file_id=? GROUP BY status", (fid,)).fetchall()
        warns = json.loads(f["warnings"]) if f["warnings"] else []
        text = (f"📦 {f['original_name']}\nSize:\n{fmt_size(f['size_bytes'])}\nSHA-256:\n{f['sha256']}\nStatus:\n{STATUS_ICON.get(f['status'], '')} {f['status']}\n"
                f"Records:\n{fmt_int(f['record_count'])}\nالنطاق: {f['scope'] if 'scope' in f.keys() else 'main'}\nParser:\n{f['parser'] or 'Not available'}\nImported:\n{ago(f['received_at'])}\nIndexed:\n{ago(f['indexed_at'])}")
        if kids:
            text += "\nContents:\n" + ", ".join(f"{k['status']}={k['c']}" for k in kids)
        if f["error"]:
            text += f"\nNote:\n{f['error'][:300]}"
        if warns:
            text += "\nWarnings:\n" + "\n".join(f"• {w[:150]}" for w in warns[:5])
        rows = [[("🔒 فحص سلامة المصدر", f"sdh:{fid}")]]
        if user.can("manage"):
            rows.insert(0, [("🔄 إعادة الفهرسة", f"sdr:{fid}")])
        self.show(chat, mid, text, kb(*rows, nav(f"src:{back}:0")))

    def reindex(self, user: User, chat: int, mid: int, fid: int) -> None:
        # Audit fix (Phase 4 F2): a nonexistent file id used to reach
        # queue.submit and crash with an unhandled FK IntegrityError (jobs
        # table references files). Same clean 'not found' as the siblings.
        if self.db.execute("SELECT 1 FROM files WHERE id=?", (fid,)).fetchone() is None:
            self.show(chat, mid, "غير موجود", kb(nav()))
            return
        self.db.execute("UPDATE files SET status='queued', error=NULL WHERE id=?", (fid,))
        jid = self.app.queue.submit("ingest", fid, {"reindex": True}, user.telegram_id)
        audit.log(self.db, user.telegram_id, audit.INDEX, "file", fid, {"reindex": True})
        self.show(chat, mid, f"🔄 أُضيفت مهمة إعادة الفهرسة JOB-{jid:06d}", kb(nav(f"sd:{fid}:a")))

    def check_hash(self, user: User, chat: int, mid: int, fid: int) -> None:
        from ..ingest.intake import check_source_changed
        try:
            changed, old, cur = check_source_changed(self.db, fid)
        except Exception as exc:
            self.show(chat, mid, f"⚠️ تعذر الفحص: {exc}", kb(nav(f"sd:{fid}:a")))
            return
        text = f"⚠️ Source changed\nOriginal SHA-256:\n{old}\nCurrent SHA-256:\n{cur}" if changed else "✅ المصدر لم يتغير (SHA-256 مطابق)."
        self.show(chat, mid, text, kb(*([[("🔄 إعادة الفهرسة", f"sdr:{fid}")]] if changed and user.can("manage") else []), nav(f"sd:{fid}:a")))

    def dup(self, user: User, chat: int, mid: int, sid: int, act: str) -> None:
        user.require("manage")
        s = self.get_state(sid, user.telegram_id)
        if not s:
            return
        p = Path(s["path"])
        if act == "k" and p.exists():
            self.app.ingest_path(p, user.telegram_id, allow_duplicate=True, original_name=s["name"], scope=self.active_scope())
            self.show(chat, mid, "✅ حُفظت نسخة منفصلة وأُضيفت للمعالجة.", kb(nav()))
            return
        if p.exists():
            if s.get("w"):  # ملف المستخدم في incoming: لا نحذفه، ننقله للحجر
                dst = self.app.storage.dir("quarantine") / f"duplicate_{uuid.uuid4().hex[:6]}_{p.name}"
                shutil.move(str(p), str(dst))
            else:
                p.unlink()  # نسخة Telegram المؤقتة فقط
        self.show(chat, mid, "تم. الملف الموجود مسبقاً يبقى كما هو." if act == "u" else "أُلغي.", kb(nav()))

    # ---- stats / indexing / jobs
    def stats(self, chat: int, mid: int) -> None:
        sc = self.active_scope()
        sc_label = "عمل" if sc == "work" else "رئيسي"
        c = self.db.execute("SELECT SUM(status='indexed') i, SUM(status IN ('failed','quarantined')) f, SUM(status='partial') p, COUNT(*) n FROM files WHERE parent_file_id IS NULL AND scope=?", (sc,)).fetchone()
        recs = self.db.execute("SELECT COALESCE(SUM(record_count),0) FROM files WHERE status IN ('indexed','partial') AND detected_type NOT IN ('zip','7z','rar','gzip','bzip2','xz','tar') AND scope=?", (sc,)).fetchone()[0]
        db_size = sum(p.stat().st_size for p in self.cfg.db_path.parent.glob(self.cfg.db_path.name + "*") if p.is_file())
        dups = self.db.execute("SELECT COUNT(*) FROM audit_log WHERE action='import' AND details LIKE '%\"duplicate\": true%'").fetchone()[0]
        snap = self.app.resources.snapshot()
        text = (f"📊 STATISTICS\nالنطاق: {sc_label}\nFiles:\n{fmt_int(c['n'])}\nIndexed:\n{c['i'] or 0}  (Partial: {c['p'] or 0})\nFailed:\n{c['f'] or 0}\nRecords:\n{fmt_int(recs)}\n"
                f"Database:\n{fmt_size(db_size)}\nIndexes:\nداخل قاعدة البيانات (FTS5)\nStorage:\n{snap['usage_gb']} GB مستخدم / {snap['free_gb']} GB متاح\nDuplicates:\n{dups}")
        self.show(chat, mid, text, kb(nav()))

    def indexing(self, user: User, chat: int, mid: int) -> None:
        rows = self.db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        text = "🔄 الفهرسة\n" + ("\n".join(f"{STATUS_ICON.get(r['status'], '•')} {r['status']}: {r['c']}" for r in rows) or "لا مهام")
        self.show(chat, mid, text, kb(*([[("🔁 إعادة محاولة الفاشل", "idxr")]] if user.can("manage") else []), [("📋 المهام", "jobs:0")], nav()))

    def retry_failed(self, user: User) -> int:
        n = 0
        for r in self.db.execute("SELECT id FROM files WHERE status='failed' AND detected_type != 'unknown'").fetchall():
            self.db.execute("UPDATE files SET status='queued', error=NULL WHERE id=?", (r["id"],))
            self.app.queue.submit("ingest", r["id"], {"retry": True}, user.telegram_id)
            n += 1
        audit.log(self.db, user.telegram_id, audit.RETRY, "files", None, {"count": n})
        return n

    def jobs(self, user: User, chat: int, mid: int | None, page: int) -> None:
        where, args = ("1=1", []) if user.is_admin else ("created_by=?", [user.telegram_id])  # المستخدم يرى مهامه فقط
        n = self.db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]
        pages = max(1, -(-n // 8))
        page = max(0, min(page, pages - 1))
        rows = self.db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT 8 OFFSET ?", [*args, page * 8]).fetchall()
        btns = [[(job_line(j), f"jd:{j['id']}")] for j in rows]
        nv = pager(page, pages, "jobs")
        if nv:
            btns.append(nv)
        title = "📋 المهام" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + ("" if rows else "\nلا توجد مهام")
        self.show(chat, mid, title, kb(*btns, nav()))

    def job_detail(self, user: User, chat: int, mid: int, jid: int, note: str = "") -> None:
        j = self.own_job(user, jid)
        if j is None:
            self.show(chat, mid, "غير موجود", kb(nav()))
            return
        ev = self.db.execute("SELECT ts, level, message FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 5", (jid,)).fetchall()
        text = f"JOB-{jid:06d}\n{j['kind']}\nStatus: {STATUS_ICON.get(j['status'], '')} {j['status']}  {note}\nProgress: {j['progress']:.0f}%\nFile: {j['file_id'] or '-'}"
        cp = json.loads(j["checkpoint"]) if j["checkpoint"] else None
        if cp:
            if j["kind"] == "er_match":
                text += f"\nالكتل: {cp.get('blocks', 0)}/{cp.get('total_blocks', '?')} • قورنت: {fmt_int(cp.get('compared', 0))} • خُزنت: {fmt_int(cp.get('stored', 0))}"
                if cp.get("skipped_blocks") or cp.get("skipped_pairs"):
                    text += f"\nمتخطاة: {cp.get('skipped_blocks', 0)} كتلة / {fmt_int(cp.get('skipped_pairs', 0))} زوج"
            else:
                text += f"\nCheckpoint: {cp.get('records', cp.get('done'))} records"
        # تقدم مباشر من الجداول — يعمل حتى قبل أول checkpoint (مرحلة البناء)
        if j["kind"] == "er_match":
            er_total = self.db.execute("SELECT COUNT(*) FROM er_records").fetchone()[0]
            st = {r["state"]: r["c"] for r in self.db.execute("SELECT state, COUNT(*) c FROM entity_matches WHERE decision IS NULL GROUP BY state").fetchall()}
            ents = self.db.execute("SELECT COUNT(*) FROM entities WHERE status='active'").fetchone()[0]
            multi = self.db.execute("SELECT COUNT(*) FROM (SELECT entity_id FROM entity_records WHERE active=1 GROUP BY entity_id HAVING COUNT(*)>1)").fetchone()[0]
            rvq = self.db.execute("SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW')").fetchone()[0]
            text += (f"\nهوية مبنية: {fmt_int(er_total)} سجلاً\nمطابقات: HIGH {st.get('HIGH_CONFIDENCE_MATCH', 0) + st.get('EXACT_MATCH', 0)} • مراجعة {rvq} • تعارض {st.get('CONFLICT', 0)}"
                     f"\nكيانات: {fmt_int(ents)} (متعددة السجلات: {fmt_int(multi)})")
        elif j["file_id"]:
            f = self.db.execute("SELECT record_count, status FROM files WHERE id=?", (j["file_id"],)).fetchone()
            if f:
                done = self.db.execute("SELECT COUNT(*) FROM records WHERE file_id=?", (j["file_id"],)).fetchone()[0]
                text += f"\nسجلات: {fmt_int(done)}/{fmt_int(f['record_count'] or 0)} ({f['status']})"
        if j["error"]:
            text += f"\nReason: {j['error'][:200]}"
        if ev:
            text += "\n" + "\n".join(f"• {e['message'][:100]}" for e in reversed(ev))
        text += f"\nآخر تحديث: {time.strftime('%H:%M:%S')}"
        rows = []
        if user.can("manage"):
            rows = [[("⏸ إيقاف", f"jc:{jid}:pause"), ("▶️ استئناف", f"jc:{jid}:resume")], [("❌ إلغاء", f"jc:{jid}:cancel"), ("🔄 إعادة المحاولة", f"jc:{jid}:retry")]]
        rows.append([("🔄 تحديث", f"jd:{jid}")])
        self.show(chat, mid, text, kb(*rows, nav("jobs:0")))

    # ---- reports / audit / settings
    def reports(self, user: User, chat: int, mid: int, page: int) -> None:
        n = self.db.execute("SELECT COUNT(*) FROM reports WHERE status='ready' AND user_id=?", (user.telegram_id,)).fetchone()[0]
        pages = max(1, -(-n // 8))
        page = max(0, min(page, pages - 1))
        rows = self.db.execute("SELECT * FROM reports WHERE status='ready' AND user_id=? ORDER BY id DESC LIMIT 8 OFFSET ?", (user.telegram_id, page * 8)).fetchall()
        btns = [[(f"EXPORT-{r['id']:06d} {r['format'].upper()} {fmt_int(r['result_count'])} نتيجة", f"rs:{r['id']}")] for r in rows]
        nv = pager(page, pages, "rep")
        if nv:
            btns.append(nv)
        title = "📤 Reports" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + ("" if rows else "\nلا توجد تقارير")
        self.show(chat, mid, title, kb(*btns, nav()))

    def send_report(self, user: User, chat: int, rid: int) -> None:
        r = self.db.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, user.telegram_id)).fetchone()
        if r is None:
            self._deny(user.telegram_id, "report", rid)
        if r is None or not Path(r["path"]).exists():
            self.send(chat, "التقرير غير موجود (ربما نُظّف حسب retention).", kb(nav()))
            return
        audit.log(self.db, user.telegram_id, audit.EXPORT, "report", rid, {"deliver": True})
        self.deliver(chat, r)

    def deliver(self, chat: int, r) -> None:
        p = Path(r["path"])
        if p.stat().st_size > self.cfg.telegram_upload_limit_mb * 1024 * 1024:
            # Audit fix (Phase 6 9.4, P3): file name only, never the full path.
            self.send(chat, f"Report generated locally.\nTelegram delivery limit exceeded.\nReport saved as:\n{p.name}", kb(nav()))
            return
        try:
            self.api.send_document(chat, p, f"EXPORT-{r['id']:06d} • {r['format'].upper()} • {fmt_int(r['result_count'])} results")
        except TelegramError as exc:
            self.send(chat, f"Report generated locally.\nTelegram delivery failed ({exc.description}).\nReport saved as:\n{p.name}", kb(nav()))

    def audit_screen(self, chat: int, mid: int, page: int) -> None:
        n = self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        pages = max(1, -(-n // 15))
        page = max(0, min(page, pages - 1))
        rows = audit.recent(self.db, 15, page * 15)
        text = "🛡 السجل" + (f" — صفحة {page + 1} من {pages}" if pages > 1 else "") + "\n" + "\n".join(f"{ago(r['ts'])} u={r['user_id']} {r['action']} {r['target_type'] or ''}{r['target_id'] or ''}" for r in rows)
        nv = pager(page, pages, "log")
        self.show(chat, mid, text, kb(*([nv] if nv else []), nav()))

    def settings(self, user: User, chat: int, mid: int) -> None:
        c = self.cfg
        size = self.page_size(user.telegram_id)
        text = (f"⚙️ Settings\nAI Model:\n{c.openrouter_model or '-'}\nAI:\n{'🟢 Enabled' if c.ai_enabled else '🔴 Disabled'}\nWorkers: {c.max_workers}\nCPU limit: {c.max_cpu_percent}%\n"
                f"RAM limit: {c.max_ram_percent}%\nStorage threshold: {c.min_free_storage_gb} GB\nResult page size: {size}\nReport retention: {c.report_retention_days} days\nAuto indexing: {c.auto_index}\n"
                f"Role: {user.role}" + ("\nDeveloper Mode: " + ("ON" if self.state.get(user.telegram_id, {}).get('dev') else "OFF") if user.can("manage") else ""))
        rows = [[(("✓" if size == n else "") + str(n), f"setp:{n}") for n in (10, 25, 50, 100)]]
        if user.can("manage"):
            rows += [[("🧹 التخزين", "clean:menu"), ("🩺 التشخيص", "diag:set")], [("🛠 وضع المطور", "dev")]]
        self.show(chat, mid, text, kb(*rows, nav()))

    def clean(self, user: User, chat: int, mid: int, what: str) -> None:
        s = self.app.storage
        if what == "menu":
            self.show(chat, mid, "⚙️ Storage\nTemporary files / Cache / Failed extraction / Old reports\n(لا تُحذف الملفات الاصلية ابداً)",
                      kb([("المؤقتة", "clean:tmp"), ("الذاكرة المؤقتة", "clean:cache")], [("فك الضغط الفاشل", "clean:fail"), ("التقارير القديمة", "clean:rep")], nav("set")))
            return
        n = {"tmp": lambda: maintenance.clean_temporary(s), "cache": lambda: maintenance.clean_cache(s), "fail": lambda: maintenance.clean_failed_extraction(self.db, s),
             "rep": lambda: maintenance.clean_old_reports(self.db, s, self.cfg.report_retention_days)}[what]()
        audit.log(self.db, user.telegram_id, audit.SETTINGS, "cleanup", what, {"removed": n})
        self.show(chat, mid, f"🧹 تم تنظيف {n} عنصر", kb(nav("clean:menu")))

    # ------------------------------------------------------------------ events from workers
    def _creator(self, fid: int) -> int | None:
        r = self.db.execute("SELECT created_by FROM jobs WHERE file_id=? AND kind='ingest' ORDER BY id LIMIT 1", (fid,)).fetchone()
        return r[0] if r else None

    def _on_event(self, event: str, ident: int, status: str) -> None:
        try:
            if event == "file":
                f = self.db.execute("SELECT original_name, record_count, error FROM files WHERE id=?", (ident,)).fetchone()
                if status in ("indexed", "partial"):
                    txt = f"✅ Processing Complete\nFile:\n{f['original_name']}\nRecords:\n{fmt_int(f['record_count'])}\nErrors:\n" + ("0" if status == "indexed" else f"⚠️ Partial: {f['error']}")
                elif status == "unsupported":
                    txt = f"⚠️ Unsupported format\nFile:\n{f['original_name']}\n{f['error'] or ''}\nالملف الأصلي محفوظ."
                else:
                    txt = f"❌ Processing Failed\nFile:\n{f['original_name']}\nReason:\n{(f['error'] or '')[:300]}"
                self.notify(self._creator(ident), txt, kb([("📁 التفاصيل", f"sd:{ident}:a")]))
            elif event == "job":
                j = self.db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
                if j["kind"] == "er_match" and status == "done":
                    cp = json.loads(j["checkpoint"]) if j["checkpoint"] else {}
                    self.notify(j["created_by"], f"✅ Entity Resolution انتهى\nمقارنات: {cp.get('compared', '-')}\nراجع: 🔗 Entity Resolution → Review", kb([("🔗 مطابقة الكيانات", "er")]))
                    if (self.db.execute("SELECT value FROM settings WHERE key='er_auto_export'").fetchone() or ["0"])[0] == "1":
                        self.app.er.submit_export(j["created_by"])
                elif j["kind"] in ("er_match", "er_export") and status == "failed":
                    self.notify(j["created_by"], f"❌ فشلت مهمة {j['kind']} JOB-{ident:06d}{transient_hint(j['error'])}", kb([("عرض التفاصيل", f"jd:{ident}")]))
                if j["kind"] in ("export", "er_export") and status == "done":
                    for r in self.app.reports.reports_for_job(self.db, ident):
                        self.notify(j["created_by"], f"📤 Report Ready\nReport:\nEXPORT-{r['id']:06d}\nResults:\n{fmt_int(r['result_count'])}")
                        self.deliver(j["created_by"], r)
                elif j["kind"] == "export" and status == "failed":
                    self.notify(j["created_by"], f"❌ حدث خطأ أثناء المعالجة.{transient_hint(j['error'])}\nJob:\nJOB-{ident:06d}", kb([("عرض التفاصيل", f"jd:{ident}")]))
                elif status == "failed" and j["kind"] != "ingest":
                    self.notify(j["created_by"], f"❌ حدث خطأ أثناء المعالجة.{transient_hint(j['error'])}\nJob:\nJOB-{ident:06d}", kb([("عرض التفاصيل", f"jd:{ident}")]))
        except Exception:
            log.exception("event notify failed")

    def on_watcher_event(self, kind: str, data: dict) -> None:
        if kind == "registered":
            r = data["result"]
            self.notify(None, f"📥 تم الاستلام (مجلد incoming)\nFile:\n{r.name}\nSize:\n{fmt_size(r.size)}\nStatus:\n⏳ Queued")
        elif kind == "duplicate":
            r = data["result"]
            admin = self.db.execute("SELECT telegram_id FROM users WHERE role='admin' AND enabled=1 LIMIT 1").fetchone()
            if admin:
                sid = self.new_state(admin[0], {"path": str(data["path"]), "name": r.name, "dup": r.duplicate_of, "w": True})
                self.notify(admin[0], f"♻️ Duplicate File\nهذا الملف موجود مسبقاً (File #{r.duplicate_of}).",
                            kb([("استخدام الموجود", f"dup:{sid}:u")], [("الاحتفاظ بنسخة منفصلة", f"dup:{sid}:k")], [("إلغاء", f"dup:{sid}:c")]))
        elif kind == "gap":
            self.notify(None, f"⚠️ ارشيف متعدد الاجزاء ناقص\n{data['path'].name}\nالاجزاء المفقودة: {data['missing']}")
        elif kind == "error":
            self.notify(None, f"❌ تعذر استلام {data['path'].name}: {data['error'][:200]}")
