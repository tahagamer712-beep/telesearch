"""Force Subscription: a GENERIC Telegram access-control module.
 * independent of patient data / search / Entity Resolution (imports nothing from them; never posts to channels)
 * ADMIN bypasses; disabled (or no enabled channel) => zero effect
 * membership is verified server-side with Bot API getChatMember; ANY failure => fail closed
Config (switch + channels) lives in the local DB (settings + force_sub_channels)."""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

MEMBER_STATUSES = {"creator", "administrator", "member"}
_REF = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{3,31}|-100\d{5,14})$")


class ForceSubError(ValueError):
    pass


@dataclass
class Channel:
    id: int
    chat_ref: str
    title: str
    invite_link: str | None
    enabled: bool

    @property
    def join_url(self) -> str | None:
        if self.invite_link:
            return self.invite_link
        if self.chat_ref.startswith("@"):
            return f"https://t.me/{self.chat_ref[1:]}"
        return None   # قناة خاصة بلا رابط دعوة: لا يمكن بناء زر Join


@dataclass
class GateResult:
    allowed: bool
    missing: list[Channel] = field(default_factory=list)
    error: str | None = None           # عند الفشل في التحقق (fail closed)
    enforced: bool = False             # هل كان الشرط فعّالاً أصلاً


class ForceSubscription:
    def __init__(self, db: Callable[[], sqlite3.Connection], api, cache_sec: int = 60) -> None:
        self._db, self.api, self.cache_sec = db, api, cache_sec
        self._cache: dict[int, float] = {}   # user_id -> وقت آخر تحقق ناجح (اشتراك كامل فقط؛ الاخطاء لا تُخزَّن)

    # ---------------- configuration (persisted in DB)
    def enabled(self) -> bool:
        r = self._db().execute("SELECT value FROM settings WHERE key='force_sub_enabled'").fetchone()
        return bool(r and r[0] == "1")

    def set_enabled(self, on: bool) -> None:
        self._db().execute("INSERT INTO settings(key,value) VALUES('force_sub_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if on else "0",))
        self._cache.clear()

    def channels(self, only_enabled: bool = False) -> list[Channel]:
        q = "SELECT * FROM force_sub_channels" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        return [Channel(r["id"], r["chat_ref"], r["title"], r["invite_link"], bool(r["enabled"])) for r in self._db().execute(q)]

    def get(self, cid: int) -> Channel | None:
        return next((c for c in self.channels() if c.id == cid), None)

    def add_channel(self, ref: str, title: str | None = None, invite_link: str | None = None, enabled: bool = True) -> int:
        ref = ref.strip()
        if not _REF.match(ref):
            raise ForceSubError("المعرّف يجب ان يكون @username او رقم القناة -100...")
        if invite_link and not invite_link.startswith(("https://t.me/", "http://t.me/")):
            raise ForceSubError("رابط الانضمام يجب ان يبدأ بـhttps://t.me/")
        try:
            cur = self._db().execute("INSERT INTO force_sub_channels(chat_ref, title, invite_link, enabled) VALUES (?,?,?,?)",
                                     (ref, (title or ref).strip()[:80], invite_link, 1 if enabled else 0))
        except sqlite3.IntegrityError:
            raise ForceSubError("القناة مضافة مسبقاً") from None
        self._cache.clear()
        return cur.lastrowid

    def edit_channel(self, cid: int, title: str | None = None, invite_link: str | None = None) -> None:
        if title is not None:
            self._db().execute("UPDATE force_sub_channels SET title=? WHERE id=?", (title.strip()[:80], cid))
        if invite_link is not None:
            if invite_link and not invite_link.startswith(("https://t.me/", "http://t.me/")):
                raise ForceSubError("رابط الانضمام يجب ان يبدأ بـhttps://t.me/")
            self._db().execute("UPDATE force_sub_channels SET invite_link=? WHERE id=?", (invite_link or None, cid))
        self._cache.clear()

    def set_channel_enabled(self, cid: int, on: bool) -> None:
        self._db().execute("UPDATE force_sub_channels SET enabled=? WHERE id=?", (1 if on else 0, cid))
        self._cache.clear()

    def remove_channel(self, cid: int) -> None:
        self._db().execute("DELETE FROM force_sub_channels WHERE id=?", (cid,))
        self._cache.clear()

    # ---------------- verification
    def _is_member(self, ch: Channel, user_id: int) -> bool:
        """True/False from Bot API; raises on ANY failure (caller fails closed)."""
        m = self.api.call("getChatMember", {"chat_id": ch.chat_ref, "user_id": user_id})
        status = (m or {}).get("status")
        if status in MEMBER_STATUSES:
            return True
        if status == "restricted":
            return bool(m.get("is_member"))
        if status in ("left", "kicked"):
            return False
        raise ForceSubError(f"حالة عضوية غير مفهومة: {status!r}")

    def gate(self, user, force_refresh: bool = False) -> GateResult:
        """Decide whether `user` may use the bot. `user` needs .telegram_id and .is_admin."""
        if getattr(user, "is_admin", False):
            return GateResult(True)                       # ADMIN يتجاوز
        if not self.enabled():
            return GateResult(True)                       # معطّل = لا تأثير ولا استدعاء API
        chans = self.channels(only_enabled=True)
        if not chans:
            return GateResult(True)
        now = time.monotonic()
        if not force_refresh and now - self._cache.get(user.telegram_id, -1e9) < self.cache_sec:
            return GateResult(True, enforced=True)
        missing: list[Channel] = []
        try:
            for ch in chans:
                if not self._is_member(ch, user.telegram_id):
                    missing.append(ch)
        except Exception as exc:                          # اي فشل (شبكة/صلاحيات/قناة غير موجودة/رد غريب) => fail closed
            return GateResult(False, chans, f"تعذر التحقق من الاشتراك الآن ({type(exc).__name__}). حاول لاحقاً.", True)
        if missing:
            self._cache.pop(user.telegram_id, None)
            return GateResult(False, missing, None, True)
        self._cache[user.telegram_id] = now
        return GateResult(True, enforced=True)

    def test_channel(self, cid: int) -> str:
        """Admin helper: can the bot read this channel's members? (bot must be admin there)"""
        ch = self.get(cid)
        if ch is None:
            return "غير موجودة"
        try:
            chat = self.api.call("getChat", {"chat_id": ch.chat_ref})
            me = self.api.call("getMe")
            st = self.api.call("getChatMember", {"chat_id": ch.chat_ref, "user_id": me["id"]}).get("status")
        except Exception as exc:
            return f"❌ فشل الفحص: {type(exc).__name__}"
        ok = st in ("administrator", "creator")
        return f"{'✅' if ok else '⚠️'} {chat.get('title', ch.chat_ref)} — حالة البوت: {st}" + ("" if ok else " (يجب ان يكون البوت مشرفاً في القناة ليتحقق من الاعضاء)")
