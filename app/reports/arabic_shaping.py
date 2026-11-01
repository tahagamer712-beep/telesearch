"""Arabic joining + simplified bidi for visual-order PDF text (no external libs)."""
from __future__ import annotations

import re
import unicodedata

_FORMS: dict[tuple[str, str], str] = {}
for _cp in list(range(0xFB50, 0xFDFF)) + list(range(0xFE70, 0xFEFF)):
    _d = unicodedata.decomposition(chr(_cp))
    _m = re.match(r"<(isolated|initial|medial|final)> ([0-9A-F]{4})$", _d)
    if _m:
        _FORMS.setdefault((chr(int(_m.group(2), 16)), _m.group(1)), chr(_cp))

_LAM_ALEF = {}
for _cp in range(0xFEF5, 0xFEFD):
    _d = unicodedata.decomposition(chr(_cp))
    _m = re.match(r"<(isolated|final)> 0644 (06[0-9A-F]{2})$", _d)
    if _m:
        _LAM_ALEF[(chr(int(_m.group(2), 16)), _m.group(1))] = chr(_cp)

# Lam-alef ligatures OFF (2026-10-26): see shape() note below.
_USE_LAM_ALEF = False

_RIGHT_ONLY = set("ءآأؤإاٱةدذرزوژڑڈۆۈۋە")
_TASHKEEL = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_MIRROR = str.maketrans("()[]{}<>", ")(][}{><")


def _is_arabic_letter(c: str) -> bool:
    return ("\u0620" <= c <= "\u064A") or ("\u066E" <= c <= "\u06D3") or c in "ۥۦۮۯۺۻۼۿ" or ("\u0750" <= c <= "\u077F")


def _joins_right_side(c: str) -> bool:  # يتصل بالحرف التالي (dual-joining)
    return _is_arabic_letter(c) and c not in _RIGHT_ONLY and c != "ء"


def shape(text: str) -> str:
    """Logical -> shaped (presentation forms), still in logical order. Diacritics are dropped."""
    text = _TASHKEEL.sub("", text).replace("ـ", "")
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if not _is_arabic_letter(c):
            out.append(c)
            i += 1
            continue
        prev = text[i - 1] if i > 0 else ""
        join_prev = bool(prev) and _joins_right_side(prev)
        # Ligatures disabled: a ligature glyph expands to two chars in
        # ToUnicode, scrambling extraction around it (2026-10-26).
        if _USE_LAM_ALEF and c == "ل" and i + 1 < n and text[i + 1] in "اأإآ":
            lig = _LAM_ALEF.get((text[i + 1], "final" if join_prev else "isolated"))
            if lig:
                out.append(lig)
                i += 2
                continue
        nxt = text[i + 1] if i + 1 < n else ""
        join_next = _joins_right_side(c) and bool(nxt) and _is_arabic_letter(nxt)
        form = "medial" if join_prev and join_next else "final" if join_prev else "initial" if join_next else "isolated"
        out.append(_FORMS.get((c, form)) or _FORMS.get((c, "isolated")) or c)
        i += 1
    return "".join(out)


def _dir(c: str) -> str:
    b = unicodedata.bidirectional(c)
    if b in ("R", "AL"):
        return "R"
    if b in ("L", "EN", "AN", "ES", "ET", "CS") and (c.isalnum()):
        return "L"
    return "N"


def visual(text: str) -> tuple[str, bool]:
    """Reorder a single line (already wrapped) into visual left-to-right order.
    Returns (visual_text, is_rtl_paragraph)."""
    shaped = shape(text)
    types = [_dir(c) for c in shaped]
    first = next((t for t in types if t != "N"), "L")
    base = first
    if "R" not in types:
        return shaped, False
    res = list(types)
    for i, t in enumerate(res):
        if t == "N":
            l = next((res[j] for j in range(i - 1, -1, -1) if types[j] != "N"), base)
            r = next((types[j] for j in range(i + 1, len(types)) if types[j] != "N"), base)
            res[i] = l if l == r else base
    runs: list[list] = []
    for ch, d in zip(shaped, res):
        if runs and runs[-1][0] == d:
            runs[-1][1].append(ch)
        else:
            runs.append([d, [ch]])
    parts = []
    for d, chars in runs:
        s = "".join(chars)
        parts.append(s[::-1].translate(_MIRROR) if d == "R" else s)
    if base == "R":
        parts.reverse()
    return "".join(parts), base == "R"
