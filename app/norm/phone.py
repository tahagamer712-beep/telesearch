"""Phone canonicalization (Iraq-aware). Returns None when not reliable."""
from __future__ import annotations

import re

from .arabic import to_ascii_digits

_NONDIGIT = re.compile(r"\D+")


def digits_only(s: str) -> str:
    return _NONDIGIT.sub("", to_ascii_digits(str(s)))


def canonical_phone(s: str | None, default_cc: str = "964") -> str | None:
    """Canonical form = country code + national number, digits only (no '+').
    Only returns a value when the format is recognizable; never guesses."""
    if not s:
        return None
    raw = to_ascii_digits(str(s)).strip()
    plus = raw.startswith("+")
    d = digits_only(raw)
    if not d:
        return None
    if d.startswith("00"):
        d, plus = d[2:], True
    if d.startswith(default_cc) and len(d) == len(default_cc) + 10 and d[len(default_cc)] == "7":
        return d
    if not plus and d.startswith("0") and len(d) == 11 and d[1] == "7":
        return default_cc + d[1:]
    if not plus and d.startswith("7") and len(d) == 10:
        return default_cc + d
    if plus and 8 <= len(d) <= 15:
        return d
    return None


def looks_like_phone(s: str) -> bool:
    d = digits_only(s)
    return 7 <= len(d) <= 15 and bool(re.fullmatch(r"[\d+\-\s()٠-٩]+", str(s).strip()))
