"""Search DSL: the ONLY thing AI / users can produce. Validated before the engine sees it."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field

from ..errors import InvalidQuery
from ..norm.arabic import to_ascii_digits
from ..norm.dates import parse_date
from ..norm.phone import canonical_phone, digits_only

FIELD_ROLES = {"name", "phone", "email", "id", "date", "number", "address", "gender", "text"}
TERM_MODES = {"auto", "exact", "prefix", "contains", "fuzzy"}
OPS = {"equals", "prefix", "contains", "year"}
AI_FIELD_TYPES = {"name", "phone", "email", "id", "date", "birth_year", "address", "gender", "any"}
OP_ALIASES = {"=": "equals", "==": "equals", "equals": "equals", "eq": "equals", "starts_with": "prefix", "startswith": "prefix", "prefix": "prefix", "contains": "contains", "like": "contains", "year": "year"}
MAX_TERMS, MAX_FILTERS, MAX_LEN = 8, 6, 120
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLE_AR = {"address": "العنوان", "gender": "الجنس", "name": "الاسم", "phone": "الهاتف", "email": "الايميل", "id": "المعرّف", "date": "التاريخ", "number": "رقم", "text": "نص", None: "أي حقل"}


@dataclass
class Term:
    text: str
    mode: str = "auto"
    role: str | None = None
    field: str | None = None  # اسم حقل محدد (مطابقة تامة لاسم العمود)


@dataclass
class Filter:
    role: str
    op: str
    value: str


@dataclass
class SearchQuery:
    terms: list[Term] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    fuzzy: bool = False
    exact: bool = False
    file_ids: list[int] | None = None
    limit: int | None = None
    offset: int = 0
    # Unified search (single input, fields detected by parse_unified):
    # ranking uses name tiers + field coverage, and partial-field matches
    # are included below full matches instead of being excluded. Legacy
    # queries (unified=False) keep the old strict AND behaviour exactly.
    # Appended last so all existing positional constructions keep working.
    unified: bool = False

    def validate(self) -> "SearchQuery":
        if not self.terms and not self.filters:
            raise InvalidQuery("الاستعلام فارغ")
        if len(self.terms) > MAX_TERMS or len(self.filters) > MAX_FILTERS:
            raise InvalidQuery("عدد الشروط اكبر من المسموح")
        for t in self.terms:
            if not isinstance(t.text, str) or not t.text.strip() or len(t.text) > MAX_LEN:
                raise InvalidQuery("نص بحث غير صالح")
            if t.field is not None and (not isinstance(t.field, str) or not t.field.strip() or len(t.field) > 120 or not t.field.isprintable()):
                raise InvalidQuery("اسم حقل غير صالح")
            if t.mode not in TERM_MODES or (t.role is not None and t.role not in FIELD_ROLES):
                raise InvalidQuery("نمط/حقل بحث غير معروف")
        for f in self.filters:
            if f.role not in FIELD_ROLES or f.op not in OPS or not str(f.value).strip() or len(str(f.value)) > MAX_LEN:
                raise InvalidQuery("فلتر غير صالح")
            if f.op == "year" and not re.fullmatch(r"(19|20)\d\d", str(f.value)):
                raise InvalidQuery("السنة لازم 4 ارقام (1900-2099)")
            if f.role == "date" and f.op == "equals" and parse_date(f.value) is None:
                raise InvalidQuery("صيغة التاريخ غير واضحة/ملتبسة؛ استخدم YYYY-MM-DD")
        if self.limit is not None and not (1 <= self.limit <= 1_000_000) or not (0 <= self.offset <= 10_000_000):
            raise InvalidQuery("limit/offset غير صالحة")
        if self.file_ids is not None and not all(isinstance(i, int) for i in self.file_ids):
            raise InvalidQuery("file_ids غير صالحة")
        return self

    def explain(self) -> list[str]:
        lines = [f"{t.field or ROLE_AR.get(t.role)} = {t.text}" + (f" ({t.mode})" if t.mode != "auto" else "") for t in self.terms]
        for f in self.filters:
            label = "سنة الميلاد/التاريخ" if f.op == "year" else ROLE_AR[f.role]
            lines.append(f"{label} {f.op} {f.value}")
        lines.append("مطابقة تقريبية: " + ("نعم" if self.fuzzy else "لا") + " | تطابق تام: " + ("نعم" if self.exact else "لا"))
        if self.unified:
            lines.append("بحث موحد: نعم")
        return lines

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "SearchQuery":
        d = json.loads(s)
        return cls([Term(**t) for t in d["terms"]], [Filter(**f) for f in d["filters"]], d.get("fuzzy", False), d.get("exact", False), d.get("file_ids"), d.get("limit"), d.get("offset", 0), d.get("unified", False))


def parse_quick(text: str) -> SearchQuery:
    """Free text -> query. Each word is an independent AND-term; a lone 4-digit year
    next to other words becomes a year filter. Fully traceable through explain()."""
    from ..norm.phone import looks_like_phone
    if looks_like_phone(text) and " " in text.strip():
        return SearchQuery(terms=[Term(text.strip()[:MAX_LEN])]).validate()
    try:
        words = shlex.split(text.replace("“", '"').replace("”", '"'))
    except ValueError:
        words = text.split()
    q = SearchQuery()
    multiple = len(words) > 1
    if len(words) == 1 and re.fullmatch(r"(19|20)\d\d", words[0]):
        q.filters.append(Filter("date", "year", words[0]))
        return q.validate()
    for w in words[:MAX_TERMS + MAX_FILTERS]:
        if multiple and re.fullmatch(r"(19|20)\d\d", w) and not any(f.op == "year" for f in q.filters):
            q.filters.append(Filter("date", "year", w))
        else:
            q.terms.append(Term(w[:MAX_LEN]))
    return q.validate()


_YMD_SPAN = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_DMY_SPAN = re.compile(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?!\d)")
_PHONE_SPAN = re.compile(r"\+?\d[\d\s\-\(\)]{6,}\d")
_YEAR_TOKEN = re.compile(r"^(19|20)\d\d$")
_DIGITS_TOKEN = re.compile(r"^\d+$")


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace each [start, end) span with a single space (spans must not overlap)."""
    if not spans:
        return text
    out, pos = [], 0
    for s, e in sorted(spans):
        out.append(text[pos:s])
        out.append(" ")
        pos = e
    out.append(text[pos:])
    return "".join(out)


def parse_unified(text: str) -> SearchQuery:
    """ONE input -> detected fields. Order-independent: a birth date, phone or
    ID may appear before, between or after the name words; position-aware
    ranking applies only WITHIN the name tokens, in the order typed.

    Detection (in this order, so a date/phone is never mistaken for a name):
    - Birth dates: ISO YYYY-MM-DD, YYYY/MM/DD, and slash DD/MM/YYYY.
      Unambiguous dates become strict date filters; ambiguous DD/MM/YYYY
      (both parts <= 12, parts differ) stays a plain date term, and the
      engine tries both interpretations (existing term_norms DMY branch).
    - Phones: digit sequences canonicalizable as Iraqi/international phones
      (canonical_phone); anything else is left for ID/year/name handling.
    - Years: a lone 4-digit 19xx/20xx token becomes a birth-year filter.
    - Long digit runs (>= 6 digits, not a phone/year) become ID terms.
    - Everything else (in typed order, quotes kept as one term) is a name term.
    Nothing is silently dropped: if the component count exceeds the DSL
    limits, validate() raises instead of discarding a field the user typed.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidQuery("الاستعلام فارغ")
    # Detection runs on ASCII digits; name terms are normalized by the engine
    # anyway, so ASCII-digit name tokens behave identically to Arabic-digit ones.
    work = to_ascii_digits(text.replace("“", '"').replace("”", '"'))
    q = SearchQuery(unified=True)
    spans: list[tuple[int, int]] = []

    # 1. Dates (year-first first, so YYYY/MM/DD is never read as DMY).
    for m in _YMD_SPAN.finditer(work):
        iso = parse_date(m.group(0))
        if iso:
            q.filters.append(Filter("date", "equals", iso))
            spans.append((m.start(), m.end()))
    rest = _remove_spans(work, spans)
    for m in _DMY_SPAN.finditer(rest):
        iso = parse_date(m.group(0))
        if iso:
            q.filters.append(Filter("date", "equals", iso))
        else:
            # Ambiguous day/month: keep as a plain term; the engine's DMY
            # branch matches either interpretation for this single term.
            q.terms.append(Term(m.group(0)[:MAX_LEN]))
    # DMY spans were found in `rest` (post-YMD-removal coordinates), so
    # remove them from `rest` directly.
    work = _remove_spans(rest, [(m.start(), m.end()) for m in _DMY_SPAN.finditer(rest)])

    # 2. Phones (only when canonicalizable; otherwise leave the digits alone).
    phone_spans: list[tuple[int, int]] = []
    for m in _PHONE_SPAN.finditer(work):
        cand = m.group(0)
        if canonical_phone(cand) is not None:
            q.terms.append(Term(cand.strip()[:MAX_LEN], "auto", "phone"))
            phone_spans.append((m.start(), m.end()))
    work = _remove_spans(work, phone_spans)

    # 3. Remaining tokens: years -> year filter; long digit runs -> ID term;
    # everything else -> name term (typed order preserved).
    try:
        words = shlex.split(work)
    except ValueError:
        words = work.split()
    for w in words:
        token = w.strip(".,;:،؛")
        if not token:
            continue
        if _YEAR_TOKEN.match(token):
            if not any(f.op == "year" for f in q.filters):
                q.filters.append(Filter("date", "year", token))
        elif _DIGITS_TOKEN.match(token) and len(token) >= 6:
            q.terms.append(Term(token[:MAX_LEN], "auto", "id"))
        else:
            q.terms.append(Term(w[:MAX_LEN]))
    return q.validate()


def from_ai(data: dict) -> SearchQuery:
    """Validate AI JSON (accepts both naming styles):
    {"terms"|"search_terms":[..], "filters":[{"field"|"field_type","operator","value"}], "fields":[..],
     "fuzzy":bool, "exact":bool, "matching":{..}, "limit":int, "offset":int}
    Unknown keys are rejected; nothing from AI reaches SQL text."""
    if not isinstance(data, dict):
        raise InvalidQuery("مخرجات AI ليست object")
    unknown = set(data) - {"terms", "search_terms", "filters", "matching", "fields", "fuzzy", "exact", "limit", "offset"}
    if unknown:
        raise InvalidQuery(f"مفاتيح غير مسموحة من AI: {sorted(unknown)}")
    terms_raw = data.get("terms", data.get("search_terms")) or []
    if not isinstance(terms_raw, list) or not all(isinstance(x, (str, int)) for x in terms_raw):
        raise InvalidQuery("terms لازم قائمة نصوص")
    matching = data.get("matching") or {}
    if not isinstance(matching, dict) or not all(isinstance(v, str) and v in {"exact", "prefix", "fuzzy", "contains"} for v in matching.values()):
        raise InvalidQuery("matching غير صالح")
    for k in ("fuzzy", "exact"):
        if k in data and not isinstance(data[k], bool):
            raise InvalidQuery(f"{k} لازم true/false")
    fields = data.get("fields") or []
    if not isinstance(fields, list) or not all(isinstance(f, str) and len(f) <= 120 for f in fields):
        raise InvalidQuery("fields غير صالحة")
    roles = {f for f in fields if f in FIELD_ROLES}
    names = [f for f in fields if f not in FIELD_ROLES]
    q = SearchQuery(fuzzy=bool(data.get("fuzzy")) or "fuzzy" in matching.values(), exact=bool(data.get("exact")) or "exact" in matching.values())
    for key, lo, hi in (("limit", 1, 1000), ("offset", 0, 10_000_000)):
        if key in data:
            if not isinstance(data[key], int) or isinstance(data[key], bool):
                raise InvalidQuery(f"{key} لازم رقم")
            setattr(q, key, max(lo, min(hi, data[key])))
    mode = "contains" if "contains" in matching.values() else "auto"
    role = next(iter(roles)) if len(roles) == 1 and not names else None
    fname = names[0] if len(names) == 1 and not roles else None
    for t in terms_raw:
        q.terms.append(Term(str(t).strip()[:MAX_LEN], mode, role, fname))
    for f in data.get("filters") or []:
        if not isinstance(f, dict) or set(f) - {"field_type", "field", "operator", "value"}:
            raise InvalidQuery("فلتر AI غير صالح")
        ft = f.get("field_type", f.get("field"))
        op = OP_ALIASES.get(str(f.get("operator", "equals")).lower())
        val = str(f.get("value", "")).strip()
        if ft not in AI_FIELD_TYPES or op is None:
            raise InvalidQuery("نوع/عملية فلتر غير مسموحة")
        if ft == "birth_year":
            q.filters.append(Filter("date", "year", val))
        elif ft == "any":
            q.terms.append(Term(val[:MAX_LEN]))
        else:
            q.filters.append(Filter(ft, op, val))
    return q.validate()
