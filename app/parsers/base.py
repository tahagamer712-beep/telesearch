"""Parser interface. A parser yields Records lazily (streaming), deterministically,
and supports `skip` so an interrupted index job can resume."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..config import Config
from ..ingest.classify import FileInfo


@dataclass
class Record:
    dataset: str  # ورقة/جدول/"main"
    locator: str  # مكان السجل بالملف الاصلي (provenance)
    values: dict[str, str | None]


def to_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    return str(v)


def flatten(obj, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict[str, str | None]:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and depth < 3:
                flatten(v, key, depth + 1, out)
            elif isinstance(v, (list, dict)):
                import json
                out[key] = json.dumps(v, ensure_ascii=False)
            else:
                out[key] = to_text(v)
    else:
        out[prefix or "value"] = to_text(obj) if not isinstance(obj, list) else __import__("json").dumps(obj, ensure_ascii=False)
    return out


class Parser(abc.ABC):
    name = "base"
    kinds: tuple[str, ...] = ()
    multi_dataset = False  # True => field names become "<dataset>.<column>"

    def __init__(self, path: Path, info: FileInfo, config: Config) -> None:
        self.path, self.info, self.cfg = Path(path), info, config
        self.warnings: list[str] = []
        self.error_count = 0

    @property
    def text_encoding(self) -> str:
        enc = self.info.encoding or "utf-8"
        return "utf-8-sig" if enc == "utf-8" else enc

    @abc.abstractmethod
    def records(self, skip: int = 0) -> Iterator[Record]:
        """Yield all records in a deterministic order, skipping the first `skip`."""

    def close(self) -> None:  # optional cleanup
        pass
