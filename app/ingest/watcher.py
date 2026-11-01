"""Watches data/incoming for files copied there (USB/LAN/Termux) - the path for files
bigger than Telegram's download limit. A file is picked up only after it stops changing."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .classify import find_gap, find_multipart_group

log = logging.getLogger("pdb.watcher")
_SKIP_SUFFIX = (".part", ".tmp", ".crdownload", ".download")


class IncomingWatcher:
    def __init__(self, app, on_event: Callable[[str, dict], None] | None = None, interval: float = 5.0) -> None:
        self.app, self.on_event, self.interval = app, on_event or (lambda k, d: None), interval
        self.seen: dict[Path, tuple[int, float, float]] = {}  # path -> (size, mtime, first_stable_time)
        self.ignored: set[Path] = set()
        self.stop_event = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="watcher", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.scan_once()
            except Exception:
                log.exception("watcher scan failed")

    def _stable(self, p: Path, settle: float) -> bool:
        try:
            st = p.stat()
        except OSError:
            return False
        prev = self.seen.get(p)
        now = time.time()
        if prev and prev[0] == st.st_size and prev[1] == st.st_mtime:
            return now - prev[2] >= settle and now - st.st_mtime >= settle
        self.seen[p] = (st.st_size, st.st_mtime, now)
        return False

    def scan_once(self) -> None:
        settle = self.app.cfg.intake_settle_sec
        d = self.app.storage.dir("incoming")
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith(_SKIP_SUFFIX) or p in self.ignored:
                continue
            group = find_multipart_group(p)
            if group:
                if not all(self._stable(x, settle) for x in group):
                    continue
                if find_gap(p):
                    if p not in self.ignored:
                        self.ignored.add(p)
                        self.on_event("gap", {"path": p, "missing": find_gap(p)})
                    continue
            elif p.suffix[1:].isdigit() and len(p.suffix) == 4:
                continue  # جزء .00N بدون .001: ننتظر
            elif not self._stable(p, settle):
                continue
            try:
                res = self.app.ingest_path(p, None)
            except Exception as exc:
                log.exception("intake failed for %s", p.name)
                self.ignored.add(p)
                self.on_event("error", {"path": p, "error": str(exc)})
                continue
            self.seen.pop(p, None)
            if res.status == "duplicate":
                self.ignored.add(p)
            self.on_event(res.status, {"path": p, "result": res})
