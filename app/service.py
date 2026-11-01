"""Application container: wires config, storage, DB, queue, pipeline, search."""
from __future__ import annotations

import logging
from pathlib import Path

from . import audit, auth
from .config import Config
from .db.connection import connect
from .db.migrator import ensure_scope_column, migrate
from .index.indexer import ensure_fts
from .ingest.classify import find_gap, find_multipart_group
from .ingest.intake import IntakeResult, register_file, register_multipart
from .jobs import JobQueue
from .pipeline import Pipeline
from .resources import ResourceMonitor
from .search.dsl import SearchQuery, parse_quick
from .search.engine import SearchEngine, SearchSummary
from .storage import StorageManager

log = logging.getLogger("pdb.app")


class App:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.storage = StorageManager(config.base_dir)
        self.storage.ensure_layout()
        boot = connect(config.db_path)
        migrate(boot)
        ensure_scope_column(boot)
        ensure_fts(boot)
        auth.sync_admins(boot, config.authorized_user_ids)
        boot.close()
        self.resources = ResourceMonitor(config)
        self.queue = JobQueue(config)
        self.pipeline = Pipeline(config, self.storage, self.queue, self.resources)
        self._register_reports()
        from .er.service import ERService
        self.er = ERService(config, self.queue, self.resources)
        from .notify.service import NotificationService
        self.notify = NotificationService(config, self.queue, self.resources)

    def _register_reports(self) -> None:
        # Audit fix (Phase 1 P2): the old fail-soft swallowed ImportError and
        # left reports=None, which crashed later at export time with a
        # confusing AttributeError. Reports are a core subsystem: fail fast
        # at boot with the real import error instead.
        from .reports.exporter import ReportService
        self.reports = ReportService(self.cfg, self.storage, self.queue)

    def close(self) -> None:
        """Release the queue connection (tests / graceful shutdown paths).
        Never called by the running bot; safe to call repeatedly."""
        try:
            self.queue.close()
        except Exception:
            pass

    def authorize(self, telegram_id: int):
        return auth.authorize(self.db(), self.cfg, telegram_id)

    def db(self):
        return self.queue.conn()

    # ---- intake
    def ingest_path(self, path: Path, user_id: int | None = None, allow_duplicate: bool = False, original_name: str | None = None, scope: str = "main") -> IntakeResult:
        conn = self.db()
        path = Path(path)
        parts = find_multipart_group(path)
        if parts and len(parts) >= 1 and not find_gap(path):
            res = register_multipart(conn, self.storage, parts, allow_duplicate, scope)
        elif parts:
            # Audit fix (Phase 5 A28): a gapped set (001+003, 002 missing)
            # used to fall through to single-file registration of part 001,
            # silently presenting HALF the file as complete with no warning.
            # Refuse loudly instead; the caller (bot) shows this message.
            from .errors import IngestError
            missing = find_gap(path)
            raise IngestError(f"أجزاء ناقصة: {missing} — أرسل كل الأجزاء كاملة قبل الفهرسة")
        else:
            res = register_file(conn, self.storage, path, original_name, self.cfg.chunk_size, allow_duplicate, scope=scope)
        if res.status == "registered":
            conn.execute("UPDATE files SET status='queued' WHERE id=? AND status='received'", (res.file_id,))
            self.queue.submit("ingest", res.file_id, created_by=user_id)
            audit.log(conn, user_id, audit.IMPORT, "file", res.file_id, {"name": res.name, "size": res.size})
        else:
            audit.log(conn, user_id, audit.IMPORT, "file", res.duplicate_of, {"duplicate": True})
        return res

    # ---- search
    def search(self, query: SearchQuery | str, user_id: int, mode: str = "quick") -> SearchSummary:
        q = parse_quick(query) if isinstance(query, str) else query
        conn = self.db()
        summary = SearchEngine(conn, self.cfg).run(q, user_id, mode)
        audit.log(conn, user_id, audit.SEARCH, "search", summary.search_id, {"mode": mode, "results": summary.total})
        return summary
