"""Scheduled notifications (ADMIN-defined) - restricted to content that carries NO individual records:
  stats        : aggregate counts only                         (recipient needs 'stats')
  ops_summary  : operational summary (jobs/disk/ER counters)   (ADMIN recipients only)
  task_events  : import/reindex/export/matching completion     (own tasks for USER, all for ADMIN)
Saved-search / query / report delivery is intentionally NOT supported (no automatic distribution of records).
Every run re-checks the recipient's CURRENT authorization, writes audit entries, runs as a Task (job kind
'notify'), chunks to Telegram limits and retries failed deliveries with backoff."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Callable

from .. import audit
from ..auth import authorize
from ..jobs import JobContext, JobQueue
from ..telegram.ui import chunk_text, fmt_int, fmt_size

log = logging.getLogger("pdb.notify")
CONTENT_TYPES = {"stats": "stats", "ops_summary": None, "task_events": None}   # type -> required permission for USER recipients
UNSUPPORTED_RECORD_TYPES = {"saved_search", "query", "report", "search", "records", "profile"}
MAX_RETRIES = 3
BACKOFF_SEC = 60


class NotifyError(ValueError):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class NotificationService:
    def __init__(self, cfg, queue: JobQueue, resources=None) -> None:
        self.cfg, self.queue, self.res = cfg, queue, resources
        self.sender: Callable[[int, str], None] | None = None     # يضبطه البوت
        self.gate: Callable[[object], bool] | None = None          # اختياري: شرط اضافي (مثل Force Subscription) للمستلمين غير الادمن
        queue.register("notify", self.handle)

    # ------------------------------------------------------------------ definition (ADMIN only; caller enforces)
    def create(self, admin_id: int, name: str, targets: list[int], content_type: str, schedule: str, max_items: int = 10, now: datetime | None = None) -> int:
        if content_type in UNSUPPORTED_RECORD_TYPES or content_type not in CONTENT_TYPES:
            raise NotifyError("نوع المحتوى غير مدعوم. المسموح: stats (احصائيات مجمعة) • ops_summary (ملخص تشغيلي) • task_events (اشعارات اكتمال المهام). "
                              "ارسال نتائج بحث/استعلام/تقارير فيها سجلات بشكل مجدول غير مدعوم عمداً.")
        if not targets or len(targets) > 50 or not all(isinstance(t, int) for t in targets):
            raise NotifyError("حدد من 1 الى 50 مستخدماً (Telegram IDs)")
        if not (1 <= max_items <= 50):
            raise NotifyError("max_items بين 1 و50")
        db = self.queue.conn()
        for t in targets:
            u = authorize(db, self.cfg, t)
            if u is None:
                raise NotifyError(f"المستخدم {t} غير مصرح له/غير موجود")
            self._require_allowed(u, content_type)
        freq = self._parse_schedule(schedule)
        now = now or datetime.now()
        sid = db.execute("INSERT INTO notification_schedules(name, created_by, targets, content_type, frequency, interval_min, time_of_day, weekday, max_items, next_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (name.strip()[:60] or content_type, admin_id, json.dumps(targets), content_type, freq["frequency"], freq.get("interval_min"), freq.get("time_of_day"), freq.get("weekday"), max_items, None)).lastrowid
        db.execute("UPDATE notification_schedules SET next_run=? WHERE id=?", (_iso(self._next(self._row(sid), now)), sid))
        audit.log(db, admin_id, audit.SETTINGS, "notification_schedule", sid, {"created": True, "type": content_type, "targets": len(targets), "schedule": schedule})
        return sid

    @staticmethod
    def _parse_schedule(text: str) -> dict:
        t = text.strip().lower()
        m = re.fullmatch(r"every\s+(\d{1,5})", t)
        if m:
            n = int(m[1])
            if n < 5:
                raise NotifyError("اقل فاصل 5 دقائق")
            return {"frequency": "interval", "interval_min": n}
        m = re.fullmatch(r"daily\s+([01]\d|2[0-3]):([0-5]\d)", t)
        if m:
            return {"frequency": "daily", "time_of_day": f"{m[1]}:{m[2]}"}
        m = re.fullmatch(r"weekly\s+([0-6])\s+([01]\d|2[0-3]):([0-5]\d)", t)
        if m:
            return {"frequency": "weekly", "weekday": int(m[1]), "time_of_day": f"{m[2]}:{m[3]}"}
        raise NotifyError("صيغة الجدولة: every N (دقائق) • daily HH:MM • weekly D HH:MM (0=الاثنين)")

    def _require_allowed(self, user, content_type: str) -> None:
        if content_type == "ops_summary" and not user.is_admin:
            raise NotifyError("الملخص التشغيلي للادمن فقط")
        perm = CONTENT_TYPES[content_type]
        if perm and not user.can(perm):
            raise NotifyError(f"المستخدم {user.telegram_id} لا يملك صلاحية '{perm}' لهذا المحتوى")

    def _row(self, sid: int):
        return self.queue.conn().execute("SELECT * FROM notification_schedules WHERE id=?", (sid,)).fetchone()

    def list(self):
        return self.queue.conn().execute("SELECT * FROM notification_schedules ORDER BY id").fetchall()

    def set_enabled(self, sid: int, on: bool, actor: int, now: datetime | None = None) -> None:
        db = self.queue.conn()
        db.execute("UPDATE notification_schedules SET enabled=? WHERE id=?", (1 if on else 0, sid))
        if on:
            db.execute("UPDATE notification_schedules SET next_run=?, retry_count=0, retry_targets=NULL WHERE id=?", (_iso(self._next(self._row(sid), now or datetime.now())), sid))
        audit.log(db, actor, audit.SETTINGS, "notification_schedule", sid, {"enabled": on})

    def delete(self, sid: int, actor: int) -> None:
        self.queue.conn().execute("DELETE FROM notification_schedules WHERE id=?", (sid,))
        audit.log(self.queue.conn(), actor, audit.SETTINGS, "notification_schedule", sid, {"deleted": True})

    @staticmethod
    def _next(row, after: datetime) -> datetime:
        if row["frequency"] == "interval":
            return after + timedelta(minutes=row["interval_min"])
        hh, mm = (int(x) for x in row["time_of_day"].split(":"))
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if row["frequency"] == "weekly":
            cand += timedelta(days=(row["weekday"] - cand.weekday()) % 7)
        if cand <= after:
            cand += timedelta(days=1 if row["frequency"] == "daily" else 7)
        return cand

    # ------------------------------------------------------------------ scheduling
    def tick(self, now: datetime | None = None) -> int:
        """Submit a Task for every due schedule. Returns number of tasks submitted."""
        now = now or datetime.now()
        db = self.queue.conn()
        n = 0
        for r in db.execute("SELECT * FROM notification_schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run <= ?", (_iso(now),)).fetchall():
            pending = db.execute("SELECT 1 FROM jobs WHERE kind='notify' AND status IN ('queued','running','paused','interrupted') AND payload LIKE ?", (f'%"schedule_id": {r["id"]}%',)).fetchone()
            if pending:
                continue
            self.queue.submit("notify", None, {"schedule_id": r["id"]}, r["created_by"])
            db.execute("UPDATE notification_schedules SET next_run=? WHERE id=?", (_iso(self._next(r, now)), r["id"]))
            n += 1
        return n

    def run_now(self, sid: int, admin_id: int) -> int:
        return self.queue.submit("notify", None, {"schedule_id": sid}, admin_id)

    # ------------------------------------------------------------------ content (permission-scoped)
    def build(self, user, content_type: str, max_items: int, since: str, db) -> str | None:
        self._require_allowed(user, content_type)
        if content_type == "stats":
            r = db.execute("SELECT (SELECT COUNT(*) FROM files WHERE parent_file_id IS NULL) f, (SELECT COUNT(*) FROM files WHERE parent_file_id IS NULL AND status='indexed') ok, "
                           "(SELECT COUNT(*) FROM files WHERE parent_file_id IS NULL AND status IN ('failed','quarantined')) bad, "
                           "(SELECT COALESCE(SUM(record_count),0) FROM files WHERE status IN ('indexed','partial') AND detected_type NOT IN ('zip','7z','rar','gzip','bzip2','xz','tar')) rec, "
                           "(SELECT COUNT(*) FROM entities WHERE status='active') ent").fetchone()
            return f"📊 إحصائيات (مجمّعة)\nFiles: {fmt_int(r['f'])} (مفهرس: {fmt_int(r['ok'])}، فاشل: {fmt_int(r['bad'])})\nRecords: {fmt_int(r['rec'])}\nUnified entities: {fmt_int(r['ent'])}"
        if content_type == "ops_summary":
            jobs = {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
            snap = self.res.snapshot() if self.res else {}
            er = db.execute("SELECT (SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW')) rv, (SELECT COUNT(*) FROM entity_matches WHERE decision IS NULL AND state='CONFLICT') cf").fetchone()
            return ("🛠 ملخص تشغيلي\nJobs: " + (", ".join(f"{k}={v}" for k, v in jobs.items()) or "-") + f"\nStorage free: {snap.get('free_gb', '?')} GB • RAM: {snap.get('ram_percent', '?')}% • CPU: {snap.get('cpu_percent', '?')}%"
                    f"\nER review items: {er['rv']} • conflicts: {er['cf']}")
        rows = db.execute("SELECT j.id, j.kind, j.status, f.original_name FROM jobs j LEFT JOIN files f ON f.id=j.file_id WHERE j.finished_at IS NOT NULL AND j.finished_at > ? AND j.kind IN ('ingest','export','er_match','er_export') "
                          + ("" if user.is_admin else "AND j.created_by = ? ") + "ORDER BY j.id DESC LIMIT ?", (since, *(() if user.is_admin else (user.telegram_id,)), max_items)).fetchall()
        if not rows:
            return None   # لا شيء جديد: لا ارسال
        icon = {"done": "✅", "failed": "❌", "cancelled": "🚫"}
        return "📋 اكتمال المهام\n" + "\n".join(f"{icon.get(r['status'], '•')} JOB-{r['id']:06d} {r['kind']} {r['status']}" + (f" — {r['original_name']}" if r["original_name"] else "") for r in rows)

    # ------------------------------------------------------------------ task handler
    def handle(self, ctx: JobContext) -> None:
        db = ctx.conn
        sid = int(ctx.payload["schedule_id"])
        r = db.execute("SELECT * FROM notification_schedules WHERE id=?", (sid,)).fetchone()
        if r is None or not r["enabled"]:
            ctx.log("الجدولة محذوفة/معطّلة: تخطّي")
            return
        if self.sender is None:
            raise RuntimeError("لا يوجد مرسل (البوت غير مشغّل)")
        targets = json.loads(r["retry_targets"]) if r["retry_targets"] else json.loads(r["targets"])
        since = r["last_run"] or r["created_at"].replace("Z", "")[:19]
        now = datetime.now()
        failed: list[int] = []
        sent = 0
        for uid in targets:
            ctx.check()
            user = authorize(db, self.cfg, uid)                           # صلاحية المستلم الحالية، لا وقت الانشاء
            if user is None:
                audit.log(db, uid, audit.DENIED, "notification", sid, {"reason": "recipient_not_authorized"})
                continue
            if not user.is_admin and self.gate is not None and not self.gate(user):
                audit.log(db, uid, audit.DENIED, "notification", sid, {"reason": "access_gate"})
                continue
            try:
                text = self.build(user, r["content_type"], r["max_items"], since, db)
            except ValueError as exc:                                      # فقدان الصلاحية لهذا النوع
                audit.log(db, uid, audit.DENIED, "notification", sid, {"reason": str(exc)[:120]})
                continue
            if text is None:
                audit.log(db, uid, "notification", "notification_schedule", sid, {"status": "nothing_new", "type": r["content_type"]})
                continue
            try:
                for part in chunk_text(text, self.cfg.telegram_message_limit):
                    self.sender(uid, part)
                sent += 1
                audit.log(db, uid, "notification", "notification_schedule", sid, {"status": "sent", "type": r["content_type"], "chars": len(text)})
            except Exception as exc:
                failed.append(uid)
                audit.log(db, uid, "notification", "notification_schedule", sid, {"status": "send_failed", "error": type(exc).__name__})
        if failed:
            n = (r["retry_count"] or 0) + 1
            if n <= MAX_RETRIES:
                nxt = now + timedelta(seconds=BACKOFF_SEC * 2 ** (n - 1))
                db.execute("UPDATE notification_schedules SET retry_count=?, retry_targets=?, next_run=?, last_status='retrying' WHERE id=?", (n, json.dumps(failed), _iso(nxt), sid))
                ctx.log(f"فشل الارسال لـ{len(failed)}؛ اعادة محاولة {n}/{MAX_RETRIES} عند {_iso(nxt)}", "warn")
                raise RuntimeError(f"send failed for {failed}; retry {n}/{MAX_RETRIES}")
            db.execute("UPDATE notification_schedules SET retry_count=0, retry_targets=NULL, last_status='failed', last_run=? WHERE id=?", (_iso(now), sid))
            raise RuntimeError(f"send failed for {failed}; retries exhausted")
        db.execute("UPDATE notification_schedules SET retry_count=0, retry_targets=NULL, last_status='ok', last_run=? WHERE id=?", (_iso(now), sid))
        ctx.log(f"أُرسل لـ{sent} مستلم")
