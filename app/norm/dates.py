"""Date parsing: returns ISO YYYY-MM-DD only when unambiguous."""
from __future__ import annotations

import datetime as _dt
import re

from .arabic import to_ascii_digits

_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?$")
_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[T ].*)?$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _make(y: int, m: int, d: int) -> str | None:
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(s: str | None) -> str | None:
    """Year-first is treated as Y-M-D. Day/month-first: only if day>12 or both
    parts equal; otherwise ambiguous -> None (we do not guess)."""
    if not s:
        return None
    t = to_ascii_digits(str(s)).strip()
    m = _YMD.match(t)
    if m:
        return _make(int(m[1]), int(m[2]), int(m[3]))
    m = _DMY.match(t)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            return _make(y, b, a)
        if b > 12 and a <= 12:
            return _make(y, a, b)
        if a == b:
            return _make(y, a, b)
    return None


def excel_serial_to_iso(n: float) -> str | None:
    try:
        return (_dt.date(1899, 12, 30) + _dt.timedelta(days=int(n))).isoformat()
    except (OverflowError, ValueError):
        return None
