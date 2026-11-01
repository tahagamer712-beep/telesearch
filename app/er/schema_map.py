"""Column classification -> standard fields, with confidence + recorded reason.
No silent risky mapping: only HIGH/MEDIUM (or admin-confirmed) mappings are used for matching."""
from __future__ import annotations

import re
import sqlite3

from ..norm.arabic import normalize_text
from ..norm.dates import parse_date
from ..norm.phone import canonical_phone, looks_like_phone

# اي كلمة من هذه تُحذف من اسم العمود قبل المطابقة ("المريض التجريبي" == "المريض")
_NOISE = {"التجريبي", "التجريبيه", "تجريبي", "تجريبيه", "الوهمي", "الوهميه", "وهمي", "وهميه", "الاصطناعي", "اصطناعي", "synthetic", "test", "of", "the"}

IDENTITY_FIELDS = ("patient_id", "national_id", "full_name", "date_of_birth", "sex", "phone")
PERSON_FIELDS = IDENTITY_FIELDS + ("alias", "registration_date", "file_status", "address")

BUILTIN: dict[str, list[str]] = {
    "patient_id": ["patient id", "patientid", "mrn", "patient no", "patient number", "hospital number", "hospital no", "medical record number", "file number", "file no",
                   "معرف المريض", "رقم المريض", "رقم الملف", "رقم السجل الطبي", "رقم الملف الطبي"],
    "national_id": ["national id", "nationalid", "nid", "national no", "الرقم الوطني", "رقم الهويه", "رقم الهوية الوطنية", "الهويه", "رقم البطاقه الوطنيه"],
    "full_name": ["name", "full name", "fullname", "patient name", "name of patient", "الاسم", "اسم", "الاسم الكامل", "اسم المريض", "اسم المراجع"],
    "mother_name": ["mother name", "mother", "mother's name", "mothers name", "اسم الام", "اسم الأم", "اسم الوالده", "اسم الوالدة", "اسم والده", "اسم والدة"],
    "governorate": ["governorate", "province", "المحافظه", "المحافظة", "محافظه", "محافظة"],
    "alias": ["alias", "nickname", "الكنيه", "اللقب", "كنيه", "لقب"],
    "date_of_birth": ["dob", "birth date", "birthdate", "date of birth", "dateofbirth", "تاريخ الميلاد", "الميلاد", "المواليد", "مواليد"],
    "sex": ["sex", "gender", "الجنس", "جنس"],
    "phone": ["phone", "phone number", "mobile", "tel", "telephone", "cell", "هاتف", "الهاتف", "رقم الهاتف", "موبايل", "جوال", "رقم الموبايل"],
    "email": ["email", "mail", "e mail", "البريد", "البريد الالكتروني", "ايميل", "الايميل"],
    "address": ["address", "العنوان", "عنوان", "المنطقه"],
    "registration_date": ["reg date", "registration date", "تاريخ التسجيل"],
    "file_status": ["file status", "حاله الملف"],
    "visit_id": ["visit id", "encounter id", "معرف الزياره", "رقم الزياره"],
    "visit_date": ["visit date", "encounter date", "date of visit", "تاريخ الزياره"],
    "visit_time": ["visit time", "الوقت"],
    "department": ["department", "clinic", "ward", "dept", "القسم", "العياده", "الجناح"],
    "visit_type": ["visit type", "نوع الزياره"],
    "diagnosis_code": ["diagnosis", "diagnosis code", "رمز تشخيص", "رمز التشخيص"],
    "physician_code": ["doctor id", "physician", "physician code", "مقدم الرعايه", "رمز الطبيب"],
    "lab_id": ["lab id", "معرف التحليل"],
    "test_date": ["sample date", "test date", "تاريخ اخذ العينه", "تاريخ التحليل"],
    "test_code": ["lab test", "test code", "رمز الفحص"],
    "result_value": ["result", "result value", "النتيجه", "النتيجه الوهميه"],
    "result_unit": ["unit", "result unit", "الوحده"],
    "admission_date": ["admit date", "admission date", "تاريخ الدخول"],
    "discharge_date": ["discharge date", "تاريخ الخروج"],
    "admission_type": ["admission type", "نوع الدخول"],
    "discharge_status": ["discharge status", "حاله الخروج"],
    "medicine_id": ["item id", "drug code", "sku", "medicine id", "معرف الصنف"],
    "item_name": ["item name", "drug name", "اسم الصنف"],
    "supplier_id": ["supplier id", "معرف المجهز"],
    "supplier_name": ["supplier name", "اسم المجهز"],
    "dispense_id": ["dispense id", "معرف عمليه الصرف"],
    "dispense_date": ["dispense date", "تاريخ الصرف"],
    "quantity": ["quantity", "qty", "الكميه"],
    "source_file": ["source file", "filename", "file name", "مصدر السجل"],
    "notes": ["notes", "note", "remarks", "ملاحظات", "ملاحظه"],
    "status": ["status", "الحاله"],
}
# مرادفات ضعيفة (ثقة MEDIUM فقط)
WEAK: dict[str, list[str]] = {"patient_id": ["id", "identifier", "code", "no", "رقم", "معرف"], "full_name": ["الاسم الاول"], "date_of_birth": ["birth", "age date"]}
STANDARD_FIELDS = tuple(BUILTIN)
_CAMEL = re.compile(r"([a-z])([A-Z])")


def norm_header(h: str) -> str:
    """Header -> comparable key: split camelCase/underscores, Arabic-normalize, drop noise words."""
    toks = normalize_text(_CAMEL.sub(r"\1 \2", str(h))).split()
    return " ".join(t for t in toks if t not in _NOISE)


def seed_synonyms(conn: sqlite3.Connection) -> None:
    for std, syns in BUILTIN.items():
        for s in syns:
            conn.execute("INSERT OR IGNORE INTO schema_synonyms(standard_field, synonym, origin) VALUES (?,?,'builtin')", (std, norm_header(s)))
    conn.execute("INSERT OR IGNORE INTO matcher_versions(version, description) VALUES (1, 'rules-v1: identifier-first, multi-field evidence, conflict-aware (docs/MATCHING.md)')")
    conn.execute("INSERT OR IGNORE INTO matcher_versions(version, description) VALUES (2, 'rules-v2: mother_name identity + mother vetoes + support-field evidence + block sub-splitting + token gate')")


def load_synonyms(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["synonym"]: r["standard_field"] for r in conn.execute("SELECT synonym, standard_field FROM schema_synonyms")}


def add_synonym(conn: sqlite3.Connection, synonym: str, standard_field: str) -> None:
    if standard_field not in STANDARD_FIELDS:
        raise ValueError(f"حقل قياسي غير معروف: {standard_field}")
    key = norm_header(synonym)
    if not key:
        raise ValueError("مرادف فارغ")
    conn.execute("INSERT INTO schema_synonyms(standard_field, synonym, origin) VALUES (?,?,'admin') ON CONFLICT(synonym) DO UPDATE SET standard_field=excluded.standard_field, origin='admin'", (standard_field, key))


def _frac(vals, pred) -> float:
    vals = [v for v in vals if v not in (None, "")]
    return sum(1 for v in vals if pred(str(v))) / len(vals) if vals else 0.0


_DATE_FIELDS = {"date_of_birth", "registration_date", "visit_date", "test_date", "admission_date", "discharge_date", "dispense_date"}


def classify_field(header: str, samples: list[str], syn: dict[str, str]) -> tuple[str | None, str, str]:
    """Return (standard_field, confidence, reason)."""
    key = norm_header(header)
    weak = {norm_header(s): f for f, ss in WEAK.items() for s in ss}
    if key in syn:
        std = syn[key]
        if std in _DATE_FIELDS and samples and _frac(samples, lambda v: parse_date(v) is not None) < 0.5:
            return std, "MEDIUM", f"اسم العمود '{header}' يطابق {std} لكن اغلب القيم ليست تواريخ واضحة"
        if std == "phone" and samples and _frac(samples, looks_like_phone) < 0.5:
            return std, "MEDIUM", f"اسم العمود '{header}' يطابق phone لكن القيم لا تشبه ارقام هواتف"
        return std, "HIGH", f"اسم العمود '{header}' يطابق مرادفاً معروفاً لـ{std}"
    if key in weak:
        return weak[key], "MEDIUM", f"مرادف ضعيف '{header}' → {weak[key]} (قد يكون غير دقيق)"
    toks = set(key.split())
    best, best_n = None, 0
    for syn_key, std in syn.items():
        st = set(syn_key.split())
        if len(st) >= 2 and st <= toks and len(st) > best_n:
            best, best_n = std, len(st)
    if best:
        return best, "NEEDS_REVIEW", f"'{header}' يحتوي كلمات مرادف {best} (تطابق جزئي): يحتاج مراجعة"
    if samples:
        if _frac(samples, lambda v: canonical_phone(v) is not None) >= 0.9:
            return "phone", "MEDIUM", "القيم تشبه ارقام هواتف (بدون اسم عمود واضح)"
        if _frac(samples, lambda v: parse_date(v) is not None) >= 0.9:
            return None, "NEEDS_REVIEW", "القيم تواريخ لكن نوع التاريخ (ميلاد/زيارة/...) غير معروف"
    return None, "NONE", "لا مطابقة؛ العمود محفوظ كما هو كحقل اضافي"


def map_file(conn: sqlite3.Connection, file_id: int) -> int:
    """Classify all not-yet-mapped fields of a file. Returns number of fields mapped."""
    syn = load_synonyms(conn)
    parser = conn.execute("SELECT parser FROM files WHERE id=?", (file_id,)).fetchone()
    multi = bool(parser and parser["parser"] in ("xlsx", "xls", "sqlite", "sql"))
    n = 0
    for f in conn.execute("SELECT id, name FROM fields WHERE file_id=? AND id NOT IN (SELECT field_id FROM field_mappings)", (file_id,)).fetchall():
        header = f["name"].partition(".")[2] if multi and "." in f["name"] else f["name"]
        samples = [r[0] for r in conn.execute("SELECT original_value FROM record_values WHERE field_id=? LIMIT 200", (f["id"],))]
        std, conf, why = classify_field(header, samples, syn)
        conn.execute("INSERT OR REPLACE INTO field_mappings(field_id, standard_field, confidence, reason, status) VALUES (?,?,?,?, 'auto')", (f["id"], std, conf, why))
        n += 1
    return n


def active_mapping_sql(alias: str = "fm") -> str:
    """SQL predicate selecting mappings that may be used for matching."""
    return f"{alias}.standard_field IS NOT NULL AND {alias}.status != 'rejected' AND ({alias}.confidence IN ('HIGH','MEDIUM') OR {alias}.status = 'confirmed')"


def apply_admin_mapping(conn: sqlite3.Connection, field_id: int, standard_field: str | None) -> int:
    """Admin decision for one column. Invalidates the derived identity layer of that file only
    (raw data untouched) so that Run Matching rebuilds it. Returns number of er_records dropped."""
    if standard_field is not None and standard_field not in STANDARD_FIELDS:
        raise ValueError("حقل قياسي غير معروف")
    status = "confirmed" if standard_field else "rejected"
    conn.execute("UPDATE field_mappings SET standard_field=?, status=?, confidence=CASE WHEN ? IS NULL THEN 'NONE' ELSE confidence END, reason=COALESCE(reason,'') || ' | قرار الادمن', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE field_id=?",
                 (standard_field, status, standard_field, field_id))
    fid = conn.execute("SELECT file_id FROM fields WHERE id=?", (field_id,)).fetchone()[0]
    ids = [r[0] for r in conn.execute("SELECT record_id FROM er_records WHERE file_id=?", (fid,))]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        conn.execute(f"DELETE FROM entity_matches WHERE record_a IN ({q}) OR record_b IN ({q})", part + part)
        conn.execute(f"DELETE FROM er_blocks WHERE record_id IN ({q})", part)
        conn.execute(f"DELETE FROM er_records WHERE record_id IN ({q})", part)
    conn.execute("UPDATE entities SET status='retired' WHERE status='active' AND id NOT IN (SELECT entity_id FROM entity_records WHERE active=1)")
    conn.execute("UPDATE er_records SET matched_ver=0")
    return len(ids)
