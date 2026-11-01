"""SQLite source files, opened strictly read-only."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from ..errors import ParserError
from .base import Parser, Record, to_text
from .registry import register


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


@register
class SQLiteParser(Parser):
    name = "sqlite"
    kinds = ("sqlite",)
    multi_dataset = True

    def records(self, skip: int = 0) -> Iterator[Record]:
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")

            def _decode(b: bytes) -> str:
                try:
                    return b.decode("utf-8")
                except UnicodeDecodeError:
                    if not getattr(self, "_nonutf8", False):
                        self._nonutf8 = True
                        self.warnings.append("نصوص غير UTF-8 داخل SQLite حُوّلت من cp1256")
                    try:
                        return b.decode("cp1256")
                    except UnicodeDecodeError:
                        return b.decode("utf-8", "replace")

            conn.text_factory = _decode
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        except sqlite3.Error as exc:
            raise ParserError(f"تعذر فتح SQLite: {exc}") from exc
        n = 0
        try:
            for t in tables:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(t)})")]
                try:
                    cur = conn.execute(f"SELECT rowid, * FROM {_q(t)} ORDER BY rowid")
                    has_rowid = True
                except sqlite3.Error:
                    cur = conn.execute(f"SELECT * FROM {_q(t)}")
                    has_rowid = False
                for i, row in enumerate(cur, 1):
                    n += 1
                    if n <= skip:
                        continue
                    rid, data = (row[0], row[1:]) if has_rowid else (i, row)
                    yield Record(t, f"table={t};rowid={rid}", {c: to_text(v) for c, v in zip(cols, data)})
        except sqlite3.Error as exc:
            raise ParserError(f"خطأ اثناء قراءة SQLite: {exc}") from exc
        finally:
            conn.close()
