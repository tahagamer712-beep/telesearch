"""CSV and plain-text parsers (streaming)."""
from __future__ import annotations

import csv
import io
import re
from typing import Iterator

from ..norm.dates import parse_date
from ..norm.phone import looks_like_phone
from .base import Parser, Record
from .registry import register

csv.field_size_limit(2 ** 27)
_NUMLIKE = re.compile(r"^[-+]?[\d.,]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _cellish_data(c: str) -> bool:
    c = c.strip()
    return bool(_NUMLIKE.match(c) or parse_date(c) or _EMAIL.match(c) or looks_like_phone(c))


def _make_header(row: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, c in enumerate(row):
        name = (c or "").strip() or f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


_YEARISH = re.compile(r"^\d{1,4}$")


def sheet_header(cells: dict) -> tuple[dict, bool]:
    """Header detection for spreadsheet rows (xlsx/xls), same idea as CSV:
    a first row with data-like cells (phone/date/email/long number) is DATA, not a header.
    Returns ({col_index: unique_name}, True) or ({}, False) when the row is data."""
    vals = [str(v).strip() for v in cells.values() if v not in (None, "")]
    if any(_cellish_data(v) for v in vals if not _YEARISH.match(v)):
        return {}, False
    seen: dict[str, int] = {}
    out: dict[int, str] = {}
    for c in sorted(cells):
        name = (cells[c] or "").strip() or f"col_{c + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out[c] = name
    return out, True


@register
class CSVParser(Parser):
    name = "csv"
    kinds = ("csv",)

    def records(self, skip: int = 0) -> Iterator[Record]:
        raw = open(self.path, "rb")
        text = io.TextIOWrapper(raw, encoding=self.text_encoding, errors="replace", newline="")
        try:
            _d = self.info.delimiter or ","
            if _d == "\\t":
                _d = "\t"
            if len(_d) != 1:
                _d = ","
            reader = csv.reader(text, delimiter=_d)
            first = next(reader, None)
            if first is None:
                return
            has_header = any(c.strip() for c in first) and not any(_cellish_data(c) for c in first if c.strip())
            header = _make_header(first) if has_header else [f"col_{i + 1}" for i in range(len(first))]
            if not has_header:
                self.warnings.append("لم يُكتشف صف عناوين؛ استُخدمت اسماء col_N")
            pending = None if has_header else first
            n = 0
            while True:
                if pending is not None:
                    row, pending = pending, None
                    line = 1
                else:
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        self.error_count += 1
                        self.warnings.append(f"CSV سطر {reader.line_num}: {exc}")
                        # لا نزيد n: الصف التالف لا يُنتج سجلاً، وn يجب أن يطابق ordinal الفهرس عند الاستئناف
                        continue
                    line = reader.line_num
                n += 1
                if n <= skip:
                    continue
                vals: dict[str, str | None] = {}
                for i, cell in enumerate(row):
                    key = header[i] if i < len(header) else f"col_extra_{i + 1}"
                    vals[key] = cell if cell != "" else None
                yield Record("main", f"row={n};line={line}", vals)
        finally:
            text.close()


@register
class TXTParser(Parser):
    name = "txt"
    kinds = ("txt",)

    def records(self, skip: int = 0) -> Iterator[Record]:
        n = 0
        with open(self.path, "r", encoding=self.text_encoding, errors="replace", newline=None) as f:
            for lineno, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                n += 1
                if n <= skip:
                    continue
                yield Record("main", f"line={lineno}", {"line": line})
