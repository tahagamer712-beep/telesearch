"""Field role detection + per-role value normalization."""
from __future__ import annotations

import re
from collections import Counter

from .arabic import normalize_text, to_ascii_digits
from .dates import ISO_RE, parse_date
from .phone import canonical_phone, digits_only, looks_like_phone

ROLES = ("name", "phone", "email", "id", "date", "number", "address", "gender", "text")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_NAME_TOKENS = {"name", "fullname", "firstname", "lastname", "patient", "doctor", "اسم", "الاسم", "مريض", "المريض", "طبيب", "الطبيب", "اسم_المريض"}
_PHONE_TOKENS = {"phone", "mobile", "tel", "telephone", "cell", "gsm", "هاتف", "الهاتف", "موبايل", "جوال", "تلفون", "الموبايل"}
_EMAIL_TOKENS = {"email", "mail", "بريد", "ايميل", "الايميل", "البريد"}
_DATE_TOKENS = {"date", "dob", "birth", "birthdate", "birthday", "تاريخ", "ميلاد", "مواليد", "تولد", "التولد", "الميلاد"}
_ADDRESS_TOKENS = {"address", "addr", "city", "street", "district", "عنوان", "العنوان", "منطقه", "المنطقه", "محافظه", "المحافظه", "مدينه", "المدينه", "سكن"}
_GENDER_TOKENS = {"gender", "sex", "جنس", "الجنس", "نوع"}
_ID_TOKENS = {"id", "uid", "code", "no", "num", "number", "nid", "ssn", "رقم", "هويه", "الهويه", "كود", "سجل"}


def field_tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    return set(normalize_text(spaced).split())


def detect_role(name: str, samples: list[str]) -> str:
    """Decide the semantic role of a field from its name and sample values."""
    vals = [v for v in samples if v not in (None, "")]
    toks = field_tokens(name)

    def frac(pred) -> float:
        return sum(1 for v in vals if pred(str(v))) / len(vals) if vals else 0.0

    if toks & _EMAIL_TOKENS or (vals and frac(lambda v: bool(_EMAIL.match(v.strip()))) >= 0.8):
        return "email"
    if toks & _PHONE_TOKENS or (vals and frac(looks_like_phone) >= 0.8 and frac(lambda v: canonical_phone(v) is not None) >= 0.5):
        return "phone"
    if toks & _DATE_TOKENS or (vals and frac(lambda v: parse_date(v) is not None) >= 0.8):
        return "date"
    if toks & _NAME_TOKENS:
        return "name"
    if toks & _ADDRESS_TOKENS:
        return "address"
    if toks & _GENDER_TOKENS:
        return "gender"
    if toks & _ID_TOKENS:
        return "id"
    if vals and frac(lambda v: bool(re.fullmatch(r"[-+]?\d+([.,]\d+)?", to_ascii_digits(v.strip())))) >= 0.9:
        return "number"
    return "text"


def normalize_value(role: str, value) -> str | None:
    """Normalized (search) form of a value. The original is never modified."""
    if value is None:
        return None
    s = str(value)
    if not s.strip():
        return None
    if role == "phone":
        return canonical_phone(s) or digits_only(s) or normalize_text(s)
    if role == "email":
        return s.strip().lower()
    if role == "date":
        return parse_date(s) or normalize_text(s)
    if role in ("id", "number"):
        return re.sub(r"[\s\-_]+", "", to_ascii_digits(s.strip())).casefold()
    return normalize_text(s)


def is_iso_date(s: str) -> bool:
    return bool(ISO_RE.match(s))


def top_role(roles: list[str]) -> str:
    return Counter(roles).most_common(1)[0][0] if roles else "text"
