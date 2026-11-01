"""Pair comparison -> (state, confidence, evidence, conflicts). Pure functions, versioned.

MATCHER_VERSION 2 rules (documented in docs/MATCHING.md). Principles:
 * a strong identifier (patient_id / national_id) is the primary evidence; names alone never merge.
 * mother_name (v2, Iraqi data): exact name + exact mother => auto-merge; exact name + different
   mother => different people (CONFLICT); mother typo => human review; a shared identifier with a
   conflicting mother => human review, never an automatic merge.
 * two different identifiers => CONFLICT/NO_MATCH, never a merge.
 * automatic merge only for EXACT_MATCH / HIGH_CONFIDENCE_MATCH; everything else stays separate for review.
 * support fields (department/governorate/address) are review evidence only: they never merge alone
   and never veto a strong match (people transfer).
 * confidence is a documented lookup (RULE_CONF) plus bounded bonuses, not a random score."""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field

MATCHER_VERSION = 2
AUTO_STATES = ("EXACT_MATCH", "HIGH_CONFIDENCE_MATCH")
REVIEW_STATES = ("PROBABLE_MATCH", "POSSIBLE_MATCH", "NEEDS_REVIEW")
PLACEHOLDERS = {"", "0", "00", "000", "n/a", "na", "null", "none", "unknown", "-", "--", "غير معروف", "لايوجد", "لا يوجد"}

# rule id -> (state, base confidence). Bonuses are added (max +0.03) for extra agreeing independent fields.
RULE_CONF = {
    "R_ID_ALL_EQUAL": ("EXACT_MATCH", 0.99),
    "R_ID_ONLY": ("HIGH_CONFIDENCE_MATCH", 0.95),
    "R_ID_NAME_HIGH": ("HIGH_CONFIDENCE_MATCH", 0.96),
    "R_ID_NAME_PARTIAL": ("PROBABLE_MATCH", 0.75),
    "R_ID_NAME_DIFFERENT": ("NEEDS_REVIEW", 0.45),
    "R_ID_SEX_DIFF": ("NEEDS_REVIEW", 0.50),
    "R_NAME_DOB_PHONE": ("HIGH_CONFIDENCE_MATCH", 0.94),
    "R_NAME3_DOB_SEX": ("HIGH_CONFIDENCE_MATCH", 0.90),
    "R_NAME_DOB": ("PROBABLE_MATCH", 0.80),
    "R_NEAR_DOB_PHONE": ("PROBABLE_MATCH", 0.78),
    "R_NAME_PHONE": ("PROBABLE_MATCH", 0.72),
    "R_DOB_PHONE": ("PROBABLE_MATCH", 0.70),
    "R_NAME_ONLY": ("POSSIBLE_MATCH", 0.50),
    "R_NEAR_DOB": ("POSSIBLE_MATCH", 0.55),
    "R_NAME_DOBTYPO": ("NEEDS_REVIEW", 0.40),
    "R_NAME_MOTHER_EXACT": ("HIGH_CONFIDENCE_MATCH", 0.93),
    "R_NAME_MOTHER_TYPO": ("NEEDS_REVIEW", 0.55),
    "R_CONFLICT_MOTHER": ("CONFLICT", 0.0),
    "R_ID_MOTHER_DIFF": ("NEEDS_REVIEW", 0.50),
    "R_CONFLICT_NID": ("CONFLICT", 0.0),
    "R_CONFLICT_ID_DOB": ("CONFLICT", 0.0),
    "R_CONFLICT_PID": ("CONFLICT", 0.0),
    "R_NO_MATCH": ("NO_MATCH", 0.0),
}


@dataclass
class MatchResult:
    state: str
    confidence: float
    rule: str
    evidence: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)


def _expand_initials(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    """'جاسم ف الكعبي' vs 'جاسم فاضل الكعبي': a single-letter token (initial) is expanded to the
    matching unmatched token of the other name; a lone initial with no counterpart is ignored."""
    fa, fb = [t for t in a if len(t) > 1], [t for t in b if len(t) > 1]
    ia, ib = [t for t in a if len(t) == 1], [t for t in b if len(t) == 1]
    left_b = list(fb)
    for t in fa:
        if t in left_b:
            left_b.remove(t)
    left_a = list(fa)
    for t in fb:
        if t in left_a:
            left_a.remove(t)
    ea, eb = list(fa), list(fb)
    for i in ia:
        m = next((t for t in left_b if t.startswith(i)), None)
        if m:
            ea.append(m); left_b.remove(m)
    for i in ib:
        m = next((t for t in left_a if t.startswith(i)), None)
        if m:
            eb.append(m); left_a.remove(m)
    return ea, eb


def name_similarity(a_norm: str | None, b_norm: str | None) -> float | None:
    """a_norm/b_norm: normalized names (initials kept)."""
    if not a_norm or not b_norm:
        return None
    ta, tb = _expand_initials(a_norm.split(), b_norm.split())
    if not ta or not tb:
        return None
    a_core, b_core = " ".join(ta), " ".join(tb)
    if sorted(ta) == sorted(tb):
        return 1.0
    sa, sb = " ".join(sorted(ta)), " ".join(sorted(tb))
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:   # حد اعلى رخيص: لا داعي للحساب الكامل
        return round(sm.quick_ratio(), 4)
    tsort = sm.ratio()
    plain = difflib.SequenceMatcher(None, a_core, b_core, autojunk=False).ratio()
    sim = max(plain, tsort)
    if set(ta) < set(tb) or set(tb) < set(ta):  # اسم جزئي داخل اسم اطول: لا يتجاوز "قريب" ابداً
        sim = min(max(sim, 0.8), 0.84)
    return round(sim, 4)


def names_maybe_equal(a_norm: str | None, b_norm: str | None) -> bool:
    """Cheap gate for name blocks: True only when two names could be exactly equal after initial
    expansion. Pairs failing this gate (and sharing no identifier/phone/dob) cannot match any rule,
    so the engine may skip comparing them."""
    if not a_norm or not b_norm:
        return False
    ea, eb = _expand_initials(a_norm.split(), b_norm.split())
    return sorted(ea) == sorted(eb)


def _eq(a, b):
    """True/False when both present, else None."""
    if a in PLACEHOLDERS or b in PLACEHOLDERS or a is None or b is None:
        return None
    return a == b


def _default_dob(d: str | None) -> bool:
    """01-01 هو تاريخ افتراضي شائع في السجلات: لا يُعتمد عليه كدليل قوي."""
    return bool(d) and d[5:] == "01-01"


def compare(a: dict, b: dict) -> MatchResult:
    """a, b: er_records-like dicts (patient_id, national_id, name_core, dob, sex, phone, mother_name, support_json)."""
    ev: list[dict] = []
    cf: list[dict] = []
    pid, nid, dob, sex, ph = (_eq(a.get(k), b.get(k)) for k in ("patient_id", "national_id", "dob", "sex", "phone"))
    s = name_similarity(a.get("name_norm") or a.get("name_core"), b.get("name_norm") or b.get("name_core"))
    n_exact = s is not None and s >= 0.999          # تطابق تام بعد التطبيع فقط (لا دمج تلقائي بتشابه تقريبي)
    n_fuzzy = s is not None and 0.85 <= s < 0.999

    def add(field_, kind, av, bv, detail=""):
        (cf if kind == "conflict" else ev).append({"field": field_, "kind": kind, "a": av, "b": bv, "detail": detail})
    if pid is True: add("patient_id", "exact", a["patient_id"], b["patient_id"])
    if pid is False: add("patient_id", "conflict", a["patient_id"], b["patient_id"], "معرفان مختلفان")
    if nid is True: add("national_id", "exact", a["national_id"], b["national_id"])
    if nid is False: add("national_id", "conflict", a["national_id"], b["national_id"], "هويتان مختلفتان")
    if s is not None:
        add("full_name", "exact" if s >= 0.999 else "similar", a.get("name_core"), b.get("name_core"), f"similarity={s:.2f}") if s >= 0.6 else add("full_name", "conflict", a.get("name_core"), b.get("name_core"), f"similarity={s:.2f} (اسمان مختلفان)")
    if dob is True: add("date_of_birth", "exact", a["dob"], b["dob"])
    if dob is False: add("date_of_birth", "conflict", a["dob"], b["dob"], "تاريخا ميلاد مختلفان")
    if sex is True: add("sex", "exact", a["sex"], b["sex"])
    if sex is False: add("sex", "conflict", a["sex"], b["sex"], "جنسان مختلفان")
    if ph is True: add("phone", "exact", a["phone"], b["phone"], "قد يكون هاتفاً مشتركاً (عائلة)")
    # v2: حقول مساندة = ادلة للمراجعة فقط (لا تغيّر الحالة ولا تدمج وحدها ابداً)
    def _support(x):
        v = x.get("support_json")
        if isinstance(v, str):
            try: return json.loads(v) or {}
            except Exception: return {}
        return v or {}
    sa, sb = _support(a), _support(b)
    for f_ in ("department", "governorate", "address"):
        if sa.get(f_) and sa.get(f_) == sb.get(f_):
            add(f_, "support", sa[f_], sb[f_], "حقل مساند متفق (لا يدمج وحده)")

    def res(rule, bonus=0.0):
        st, base = RULE_CONF[rule]
        return MatchResult(st, round(min(base + min(bonus, 0.03), 0.995 if st != "EXACT_MATCH" else 1.0), 4) if st != "CONFLICT" else 0.0, rule, ev, cf)

    if dob is False and pid is not True and nid is not True and ph is not True:
        return res("R_NO_MATCH")  # تاريخا ميلاد مختلفان بلا معرّف/هاتف مشترك: اشخاص مختلفون (رفض سريع)
    # --- conflicts (hard) ---
    if nid is False:
        return res("R_CONFLICT_NID") if (s is None or s >= 0.6 or dob is True) else res("R_NO_MATCH")
    id_eq = pid is True or nid is True
    if id_eq and dob is False:
        return res("R_CONFLICT_ID_DOB")
    if pid is False and nid is not True:
        # معرفان مختلفان: CONFLICT فقط اذا بدا الشخص نفسه (اسم مشابه + نفس تاريخ الميلاد)؛ غير ذلك اشخاص مختلفون
        return res("R_CONFLICT_PID") if (s is not None and s >= 0.85 and dob is True) else res("R_NO_MATCH")
    # v2: اسم الام — يُحسب فقط عند اسم مطابق او معرّف مشترك (غير ذلك لا يؤثر على اي قاعدة)
    ms = name_similarity(a.get("mother_name"), b.get("mother_name")) if (n_exact or id_eq) else None
    m_exact = ms is not None and ms >= 0.999
    m_fuzzy = ms is not None and 0.85 <= ms < 0.999
    m_partial = ms is not None and 0.6 <= ms < 0.85
    m_conflict = ms is not None and ms < 0.6
    if ms is not None:
        add("mother_name", "exact" if ms >= 0.999 else "similar", a.get("mother_name"), b.get("mother_name"), f"similarity={ms:.2f}") if ms >= 0.6 else add("mother_name", "conflict", a.get("mother_name"), b.get("mother_name"), f"similarity={ms:.2f} (اسما ام مختلفان)")
    # --- identifier agreement ---
    if m_conflict and id_eq:
        return res("R_ID_MOTHER_DIFF")  # معرّف مشترك لكن الام مختلفة: خطأ بيانات محتمل — يقرره بشر
    if (m_fuzzy or m_partial) and id_eq:
        return res("R_NAME_MOTHER_TYPO")  # الام قريبة لكن ليست مطابقة: مراجعة لا دمج تلقائي
    if id_eq:
        if sex is False:
            return res("R_ID_SEX_DIFF")
        if s is not None and s < 0.6:
            return res("R_ID_NAME_DIFFERENT")
        if s is not None and s < 0.85:
            return res("R_ID_NAME_PARTIAL")
        comparable_equal = [x for x in (s is not None and s >= 0.999 or None, dob, sex, ph) if x is True]
        if s is None and not comparable_equal:
            return res("R_ID_ONLY")
        if s is not None and s >= 0.999 and dob is not False and sex is not False and len(comparable_equal) >= 2:
            return res("R_ID_ALL_EQUAL", 0.0)
        return res("R_ID_NAME_HIGH" if s is not None else "R_ID_ONLY", 0.01 * len(comparable_equal))
    # --- no usable identifier agreement: multi-field evidence only ---
    if dob is False:
        return res("R_NAME_DOBTYPO") if ((n_exact or n_fuzzy) and ph is True) else res("R_NO_MATCH")
    if m_conflict:
        return res("R_CONFLICT_MOTHER")  # اسم مطابق + ام مختلفة تماماً = شخصان مختلفان (هنا ms موجود فقط عند n_exact)
    if m_fuzzy or m_partial:
        return res("R_NAME_MOTHER_TYPO")
    toks = len((a.get("name_core") or "").split())
    ambiguous_dob = _default_dob(a.get("dob")) or _default_dob(b.get("dob"))
    if m_exact:
        return res("R_NAME_MOTHER_EXACT")  # قاعدة الدمج الاساسية للبيانات العراقية: اسم مطابق + ام مطابقة
    if n_exact and dob is True and ph is True:
        return res("R_NAME_DOB_PHONE")
    if n_exact and dob is True and sex is True and toks >= 3 and not ambiguous_dob:
        return res("R_NAME3_DOB_SEX")
    if n_exact and dob is True:
        return res("R_NAME_DOB")
    if n_fuzzy and dob is True and ph is True:
        return res("R_NEAR_DOB_PHONE")
    if n_exact and ph is True:
        return res("R_NAME_PHONE")
    if s is None and dob is True and ph is True:
        return res("R_DOB_PHONE")
    if n_fuzzy and dob is True:
        return res("R_NEAR_DOB")
    if n_exact and dob is None:
        return res("R_NAME_ONLY")
    return res("R_NO_MATCH")
