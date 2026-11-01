"""Parse one OCR line from a scanned table into structured columns.

Pure stdlib. Never invents a value: a cell OCR destroyed stays empty.
Returns (kind, fields):
  'header'   -> title / subtitle / footer / column-header line
  'data'     -> a table row; fields has only the non-empty Arabic columns
  'fragment' -> a shard of a split row, or garbage (caller keeps raw text)
"""
from __future__ import annotations

import re

DIACR = re.compile(r"[\u064b-\u0652\u0670\u0640]")
AR2W = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

SPECIALTIES = ["أنف وأذن", "أنف وآذن", "أنف وآأذن", "أنف وآدن",
               "باطنية", "قلبية", "عامة", "أطفال", "أسنان",
               "نسائية", "نشضائية", "نشائية", "جلدية", "عظام", "عيون"]
STATUS_MAP = [("موقوف", "موقوف"), ("موقو", "موقوف"),
              ("فعّال", "فعال"), ("فعال", "فعال"), ("فكال", "فعال")]
PHARM_RE = r"(?:صيدلية|صيدليه|سيدلية|ضيدلية|صردلية|صيقلية)"
SKIP_LINE = ("جدول توزيع", "بيانات وهمية", "صفحة", "اسم الطبيب",
             "اسم لطبي", "اس القيين", "اشم")


def _clean(t: str) -> str:
    t = DIACR.sub("", t or "")
    return re.sub(r"\s+", " ", t).strip()


def _digits(t: str) -> str:
    return t.translate(AR2W)


def _edge(s: str) -> str:
    return s.strip(" .|[]()'\"“”’><").strip()


def _has_arabic(s: str) -> bool:
    import re as _re
    return bool(_re.search(r"[\u0600-\u06ff]", s))


def parse_ocr_line(raw: str) -> tuple[str, dict[str, str]]:
    t = _clean(raw)
    d = _digits(t)
    if any(k in d for k in SKIP_LINE):
        return "header", {}
    out: dict[str, str] = {}
    for form, norm in STATUS_MAP:
        if re.search(r"(?:^|\s)" + re.escape(form) + r"(?:\s|$)", d):
            out["الحالة"] = norm
            break
    if "الحالة" not in out and re.search(r"\sفال\s*$", d):
        out["الحالة"] = "فعال"
    m = re.search(r"[صض]\s*-?\s*(\d{4,5})\b", d)
    if m:
        out["رقم الترخيص"] = m.group(1)
    else:
        m2 = re.search(r"(?<!\d)(\d{5})(?!\d)", d)
        if m2:
            out["رقم الترخيص"] = m2.group(1)
        else:
            m3 = re.search(r"(?<!\d)(1[0-5]\d{3})\d(?!\d)", d)
            if m3:
                out["رقم الترخيص"] = m3.group(1)
    lic = out.get("رقم الترخيص", "")
    work = d.replace(lic, " ", 1) if lic else d
    m = re.match(r"\s*(?:\|\s*)?(\d{1,3})\b", work)
    if m:
        out["التسلسل"] = m.group(1)
    else:
        m = re.search(r"\|\s*(\d{1,3})\b", work)
        if m:
            out["التسلسل"] = m.group(1)
        else:
            m = re.search(r"(?<!\d)(\d{1,3})(?!\d)", work)
            if m:
                out["التسلسل"] = m.group(1)
    for sp in SPECIALTIES:
        if sp in d:
            out["التخصص"] = "أنف وأذن" if sp.startswith("أنف") else (
                "نسائية" if sp in ("نشضائية", "نشائية") else sp)
            break
    m = re.search(PHARM_RE + r"\s*([^|]*?)(?=\s*(?:\|\s*[.\s]*)?(?:حي\s|حى\s|خي\s|خى\s|المنطقة|المنطقه|ص-|ض-|فعال|فعّال|فكال|موقوف|موقو|\||$))", d)
    if m:
        name = m.group(1)
        name = re.sub(r"[صض]\s*-?\s*\d.*$", "", name)
        name = re.sub(r"\s+(فعال|فعّال|فكال|موقوف|موقو)\s*$", "", name)
        name = re.sub(r"^(?:[A-Za-z]\s+)+", "", name)
        name = re.sub(r"\s+ال\s*$", "", name)
        name = _edge(name)
        if name and _has_arabic(name):
            out["الصيدلية"] = name
    m = re.search(r"(?:حي|حى|خي|خى)\s+([^\s|]+)", d)
    if m:
        out["المنطقة الفرعية"] = "حي " + _edge(m.group(1))
    m = re.search(r"(?:المنطقة|المنطقه)\s*([^\s|]+)?", d)
    if m and m.group(1):
        out["المنطقة الرئيسية"] = "المنطقة " + _edge(m.group(1))
    elif "المنطقة" in d or "المنطقه" in d:
        out["المنطقة الرئيسية"] = "المنطقة"
    m = re.search(r"د[.:]\s*([^|]+?)(?=\s*(?:صيدلية|سيدلية|ضيدلية|صردلية|صيقلية|حي\s|حى\s|خي\s|خى\s|المنطقة|المنطقه|أنف|باطنية|قلبية|عامة|أطفال|أسنان|نسائية|نشضائية|نشائية|جلدية|عظام|عيون|ص-|ض-|\||$))", d)
    if m:
        name = re.sub(r"\[.*?\]", "", m.group(1))
        name = re.sub(r"(\s*\d+\s*)+$", "", name)
        name = re.sub(r"^(?:[A-Za-z]\s+)+", "", name)
        name = _edge(name)
        if name and _has_arabic(name):
            out["الطبيب"] = name
    if "الطبيب" not in out:
        for seg in d.split("|"):
            seg = seg.strip()
            if not seg or re.search(r"^\d+\s*$", seg):
                continue
            for kw in ("صيدلية", "سيدلية", "ضيدلية", "صردلية", "صيقلية",
                       "حي ", "حى ", "خي ", "خى ", "المنطقة", "المنطقه"):
                if kw in seg:
                    seg = seg.split(kw)[0]
                    break
            for sp in ("أنف", "باطنية", "قلبية", "عامة", "أطفال", "أسنان",
                       "نسائية", "نشضائية", "نشائية", "جلدية", "عظام", "عيون"):
                if sp in seg:
                    seg = seg.split(sp)[0]
                    break
            words = re.findall(r"[\u0600-\u06ff]{2,}", seg)
            if len(words) >= 2:
                name = re.sub(r"\[.*?\]", "", seg)
                name = re.sub(r"^\d+\s*", "", name)
                name = re.sub(r"(\s*\d+\s*)+$", "", name)
                name = re.sub(r"^(?:[A-Za-z]\s+)+", "", name)
                name = _edge(name)
                if name and _has_arabic(name):
                    out["الطبيب"] = name
                break
    if not out:
        return "fragment", {}
    return "data", out
