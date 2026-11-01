"""Text normalization for search (never applied to stored originals)."""
from __future__ import annotations

import re
import unicodedata

_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u08D3-\u08FF]")
_LETTER_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ی": "ي", "ې": "ي",
    "ؤ": "و", "ة": "ه", "ک": "ك", "ـ": None,
})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def to_ascii_digits(s: str) -> str:
    return s.translate(_DIGITS)


def normalize_text(s: str | None) -> str:
    """NFKC, strip diacritics/tatweel, unify Arabic letter variants and digits,
    casefold, punctuation -> space, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _DIACRITICS.sub("", s)
    s = s.translate(_LETTER_MAP).translate(_DIGITS)
    s = _NON_WORD.sub(" ", s.casefold())
    return " ".join(s.split())


def tokens(s: str) -> list[str]:
    return normalize_text(s).split()
