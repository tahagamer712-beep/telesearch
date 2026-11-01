"""Resource monitor: CPU/RAM/storage/temperature with graceful fallbacks.
Android may deny some /proc or /sys reads; anything unreadable returns None (never guessed)."""
from __future__ import annotations

import glob
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .errors import JobInterrupted

log = logging.getLogger("pdb.resources")
GB = 1024 ** 3


class ResourceMonitor:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self._last_cpu: tuple[int, int] | None = None
        self._last_cpu_val: float | None = None
        self._last_cpu_t = 0.0
        self._usage_cache: tuple[float, int] = (0.0, 0)
        self._lock = threading.Lock()

    def cpu_percent(self) -> float | None:
        with self._lock:
            now = time.monotonic()
            if now - self._last_cpu_t < 1.0:
                return self._last_cpu_val
            try:
                with open("/proc/stat") as f:
                    vals = [int(x) for x in f.readline().split()[1:]]
                total, idle = sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)
            except (OSError, ValueError, IndexError):
                # Audit fix 2026-10-27 (Phase 8 P0): Android's bionic libc has no
                # getloadavg, so the fallback itself raised AttributeError (not
                # OSError) and crashed ingest on any device where /proc/stat is
                # unreadable. CPU unknown -> None (pressure() treats it as no
                # pressure, same as the first-sample path).
                try:
                    if not hasattr(os, "getloadavg"):
                        return None
                    return round(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 1)
                except (OSError, AttributeError):
                    return None
            prev, self._last_cpu, self._last_cpu_t = self._last_cpu, (total, idle), now
            if prev is None or total == prev[0]:
                return self._last_cpu_val
            self._last_cpu_val = round(100.0 * (1 - (idle - prev[1]) / (total - prev[0])), 1)
            return self._last_cpu_val

    def ram_percent(self) -> float | None:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k] = int(v.split()[0])
            avail = info.get("MemAvailable", info.get("MemFree", 0) + info.get("Cached", 0))
            return round(100.0 * (1 - avail / info["MemTotal"]), 1)
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return None

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.cfg.base_dir).free

    def temperature_c(self) -> float | None:
        best = None
        for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                v = int(Path(p).read_text().strip())
                v = v / 1000 if v > 1000 else v
                if 0 < v < 150:
                    best = max(best or v, v)
            except (OSError, ValueError):
                continue
        return best

    def storage_usage_bytes(self, ttl: float = 120.0) -> int:
        t, val = self._usage_cache
        if time.monotonic() - t < ttl:
            return val
        total = 0
        for sub in ("data", "database", "indexes", "reports", "backups"):
            for root, _, files in os.walk(self.cfg.base_dir / sub):
                for f in files:
                    try:
                        total += os.lstat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
        self._usage_cache = (time.monotonic(), total)
        return total

    def snapshot(self) -> dict:
        return {"cpu_percent": self.cpu_percent(), "ram_percent": self.ram_percent(), "free_gb": round(self.free_bytes() / GB, 2),
                "temp_c": self.temperature_c(), "usage_gb": round(self.storage_usage_bytes() / GB, 2)}

    def pressure(self) -> str | None:
        if self.free_bytes() < self.cfg.min_free_storage_gb * GB:
            return "storage"
        cap = self.cfg.max_storage_usage_gb
        if cap and self.storage_usage_bytes() > cap * GB:
            return "quota"
        ram, cpu = self.ram_percent(), self.cpu_percent()
        if ram is not None and ram > self.cfg.max_ram_percent:
            return "ram"
        if cpu is not None and cpu > self.cfg.max_cpu_percent:
            return "cpu"
        return None

    def gate(self, stopping: Callable[[], bool] = lambda: False, wait_max: float = 120.0) -> None:
        """Call between batches. Throttles on CPU/RAM; pauses the job on storage problems."""
        waited = 0.0
        while True:
            reason = self.pressure()
            if reason in ("storage", "quota"):
                raise JobInterrupted(f"paused:{reason}")
            if reason is None or waited >= wait_max or stopping():
                break
            if waited == 0:
                log.warning("resource pressure (%s) - throttling", reason)
            time.sleep(2.0)
            waited += 2.0
        if self.cfg.throttle_delay_sec:
            time.sleep(self.cfg.throttle_delay_sec)
