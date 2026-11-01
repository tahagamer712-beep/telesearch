"""StorageManager: single owner of all filesystem paths."""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

from .errors import StorageError, UnsafePathError

_LAYOUT = {
    "incoming": "data/incoming",
    "originals": "data/originals",
    "extracted": "data/extracted",
    "temporary": "data/temporary",
    "quarantine": "data/quarantine",
    "database": "database",
    "indexes": "indexes",
    "reports": "reports",
    "logs": "logs",
    "backups": "backups",
    "cache": "cache",
}
_BAD_CHARS = re.compile(r"[^\w.\- ]", re.UNICODE)


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Return a safe file name (no dirs, no control chars). Keeps Arabic letters.
    Audit fix 2026-10-27 (Phase 5 A33): max_len is a UTF-8 BYTE budget, not a
    character count — 120 Arabic chars are 240 bytes, and with the 65-byte
    '{sha}_' prefix the stored name reached 305 bytes > the 255-byte filesystem
    limit, crashing ingest with an uncaught OSError. Truncation never splits a
    UTF-8 sequence (a cut multi-byte char is dropped whole)."""
    name = unicodedata.normalize("NFC", name.replace("\\", "/").split("/")[-1])
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        return "file"
    if len(name.encode("utf-8")) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext.encode("utf-8")) <= 12:
            budget = max_len - len(ext.encode("utf-8")) - 1
            stem = stem.encode("utf-8")[:max(0, budget)].decode("utf-8", errors="ignore")
            name = stem + "." + ext
        else:
            name = name.encode("utf-8")[:max_len].decode("utf-8", errors="ignore")
    return name


class StorageManager:
    def __init__(self, base_dir: Path) -> None:
        self.base = Path(base_dir).resolve()

    def dir(self, key: str) -> Path:
        try:
            return self.base / _LAYOUT[key]
        except KeyError:
            raise StorageError(f"مجلد غير معروف: {key}") from None

    def ensure_layout(self) -> None:
        for key in _LAYOUT:
            path = self.dir(key)
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass  # بعض انظمة الملفات (مثل /sdcard) ما تدعم الصلاحيات

    def safe_join(self, root_key: str, *parts: str) -> Path:
        """Join under a layout root; raise if result escapes the root."""
        root = self.dir(root_key).resolve()
        target = root.joinpath(*parts).resolve()
        if target != root and not target.is_relative_to(root):
            raise UnsafePathError(f"مسار خارج المجلد المسموح: {'/'.join(parts)}")
        return target

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.base).free
