"""Build the normalized identity layer (er_records) + blocking keys from raw records.
Raw data is never modified; everything here is derived and rebuildable."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from ..norm.arabic import normalize_text
from ..norm.dates import parse_date
from ..norm.phone import canonical_phone, digits_only
from .matcher import PLACEHOLDERS
from .schema_map import PERSON_FIELDS, active_mapping_sql

_SEX = {"m": "M", "male": "M", "ذكر": "M", "ذ": "M", "رجل": "M", "f": "F", "female": "F", "انثي": "F", "انثى": "F", "ث": "F", "امراه": "F", "أنثى": "F"}
IDENTITY_KEYS = ("patient_id", "national_id", "full_name", "date_of_birth", "phone")

log = logging.getLogger("pdb.er")


def _build_cap(conn: sqlite3.Connection) -> int:
    """Live-adjustable build cap (settings key er_build_cap, TOTAL er_records).
    0/missing/unparsable = unlimited. Re-read per chunk so it can change
    without a restart."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='er_build_cap'").fetchone()
        return int(row[0]) if row and row[0] else 0
    except (ValueError, TypeError):
        return 0


def norm_id(v) -> str | None:
    if v is None:
        return None
    s = "".join(ch for ch in normalize_text(str(v)) if ch.isalnum())
    return None if s in PLACEHOLDERS or len(s) < 3 else s


def norm_sex(v) -> str | None:
    return _SEX.get(normalize_text(str(v))) if v else None


def name_core(v) -> tuple[str | None, str | None]:
    n = normalize_text(v) if v else ""
    toks = [t for t in n.split() if len(t) > 1]  # حرف واحد (اختصار اسم) يُهمل
    return (n or None), (" ".join(toks) or None)


def _fp(items) -> str:
    return hashlib.sha1(json.dumps(sorted(items), ensure_ascii=False).encode()).hexdigest()


def build_records(conn: sqlite3.Connection, chunk: int = 1000, stopping=lambda: False) -> tuple[int, bool]:  # short lock windows: build must not starve button writes (2026-10-26)
    """Create er_records for records of files that have identity fields mapped (HIGH/MEDIUM/confirmed).
    Incremental: only files whose identity layer is complete are skipped; a partial file rebuilds
    (INSERT OR IGNORE makes rebuilds idempotent). Returns (rows added, capped).
    When settings er_build_cap is set and total er_records reaches it, building
    stops early and the caller proceeds to matching over the built subset."""
    q = ",".join("?" * len(IDENTITY_KEYS))
    files = [r[0] for r in conn.execute(
        "SELECT DISTINCT f.id FROM files f JOIN fields fl ON fl.file_id=f.id JOIN field_mappings fm ON fm.field_id=fl.id "
        f"WHERE f.status IN ('indexed','partial') AND {active_mapping_sql('fm')} AND fm.standard_field IN ({q})", IDENTITY_KEYS)]
    added = 0
    capped = False
    for fid in files:
        # v2: لا تُتخطى طبقة الهوية إلا إذا اكتملت فعلاً — ملف جزئي (قُتل بناؤه سابقاً) كان يُتخطى
        # كاملاً فيُفقد باقي سجلاته من المطابقة بصمت. إعادة البناء آمنة: INSERT OR IGNORE.
        active_ids = [r[0] for r in conn.execute(
            f"SELECT fm.field_id FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id WHERE fl.file_id=? AND {active_mapping_sql('fm')} AND fm.standard_field IN ({q})", (fid, *IDENTITY_KEYS))]
        if not active_ids:
            continue
        iq = ",".join("?" * len(active_ids))
        expected = conn.execute(f"SELECT COUNT(DISTINCT record_id) FROM record_values WHERE field_id IN ({iq}) AND original_value IS NOT NULL AND original_value != ''", active_ids).fetchone()[0]
        actual = conn.execute("SELECT COUNT(*) FROM er_records WHERE file_id=?", (fid,)).fetchone()[0]
        if expected > 0 and actual >= expected:
            continue
        active = {r["field_id"]: r["standard_field"] for r in conn.execute(
            f"SELECT fm.field_id, fm.standard_field FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id WHERE fl.file_id=? AND {active_mapping_sql('fm')}", (fid,))}
        lo, hi_ = conn.execute("SELECT COALESCE(MIN(id),0), COALESCE(MAX(id),0) FROM records WHERE file_id=?", (fid,)).fetchone()
        cur = lo
        while cur <= hi_:
            if stopping():
                return added, capped
            rows = conn.execute(
                "SELECT rv.record_id, rv.field_id, fl.name AS fname, rv.original_value AS ov, rv.normalized_value AS nv FROM record_values rv "
                "JOIN fields fl ON fl.id=rv.field_id WHERE rv.record_id>=? AND rv.record_id<? AND fl.file_id=? ORDER BY rv.record_id", (cur, cur + chunk, fid)).fetchall()
            conn.execute("BEGIN IMMEDIATE")
            try:
                added += _flush(conn, fid, rows, active)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            cur += chunk
            cap = _build_cap(conn)
            if cap and conn.execute("SELECT COUNT(*) FROM er_records").fetchone()[0] >= cap:
                log.info("ER build capped at %s er_records (settings er_build_cap)", cap)
                capped = True
                break
        if capped:
            break
    # Audit fix (Phase 7/18): same ANALYZE discipline at ER-build completion
    # (covers manual er runs over data ingested before the pipeline hook, and
    # any path that builds without an ingest job). Never fail the build.
    if added >= 1000:
        try:
            conn.execute("ANALYZE")
        except Exception as exc:
            log.warning("ER build ANALYZE skipped: %s", exc)
    return added, capped


def _flush(conn, fid: int, rows, active: dict[int, str]) -> int:
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r["record_id"], []).append(r)
    out, blocks = [], []
    for rid, vals in by.items():
        ident: dict[str, str] = {}
        exact_items, norm_items = [], []
        for v in vals:
            sf = active.get(v["field_id"])
            exact_items.append((v["fname"], v["ov"]))
            norm_items.append((sf or v["fname"], v["nv"]))
            if sf in IDENTITY_KEYS and v["ov"] not in (None, ""):
                ident.setdefault(sf, v["ov"])
        if not ident:
            continue
        nm, core = name_core(ident.get("full_name"))
        ph = None
        if ident.get("phone"):
            ph = canonical_phone(ident["phone"]) or (digits_only(ident["phone"]) if len(digits_only(ident["phone"])) >= 7 else None)
        sx = next((v["ov"] for v in vals if active.get(v["field_id"]) == "sex" and v["ov"]), None)
        # v2: اسم الام هوية شخصية (لا يدخل IDENTITY_KEYS لان الام وحدها ليست هوية) + حقول مساندة للمراجعة فقط
        mo = next((v["ov"] for v in vals if active.get(v["field_id"]) == "mother_name" and v["ov"]), None)
        mother = normalize_text(mo) if mo else None
        if mother in PLACEHOLDERS:
            mother = None
        mother = mother or None
        support = {}
        for v in vals:
            sf = active.get(v["field_id"])
            if sf in ("department", "governorate", "address") and v["ov"]:
                support.setdefault(sf, normalize_text(v["ov"]))
        support_json = json.dumps(support, ensure_ascii=False) if support else None
        rec = dict(record_id=rid, file_id=fid, patient_id=norm_id(ident.get("patient_id")), national_id=norm_id(ident.get("national_id")), name_norm=nm, name_core=core,
                   dob=parse_date(ident.get("date_of_birth")), sex=norm_sex(sx), phone=ph, fp_exact=_fp(exact_items), fp_norm=_fp(norm_items),
                   mother_name=mother, support_json=support_json)
        out.append(rec)
        blocks.extend((k, rid) for k in block_keys(rec))
    conn.executemany("INSERT OR IGNORE INTO er_records(record_id, file_id, patient_id, national_id, name_norm, name_core, dob, sex, phone, fp_exact, fp_norm, mother_name, support_json) "
                     "VALUES (:record_id,:file_id,:patient_id,:national_id,:name_norm,:name_core,:dob,:sex,:phone,:fp_exact,:fp_norm,:mother_name,:support_json)", out)
    conn.executemany("INSERT OR IGNORE INTO er_blocks(block_key, record_id) VALUES (?,?)", blocks)
    ids = ",".join(str(r["record_id"]) for r in out) or "NULL"
    # تكرار تام (نفس القيم الاصلية في نفس الحقول): اول سجل هو الاصل
    conn.execute(f"UPDATE er_records SET dup_of=(SELECT MIN(e2.record_id) FROM er_records e2 WHERE e2.fp_exact=er_records.fp_exact AND e2.file_id=er_records.file_id) "
                 f"WHERE record_id IN ({ids}) AND record_id != (SELECT MIN(e2.record_id) FROM er_records e2 WHERE e2.fp_exact=er_records.fp_exact AND e2.file_id=er_records.file_id)")
    return len(out)


def block_keys(r: dict) -> list[str]:
    """Blocking keys. Multiple independent keys so a typo in one field does not hide a match."""
    keys = []
    if r.get("patient_id"): keys.append("pid:" + r["patient_id"])
    if r.get("national_id"): keys.append("nid:" + r["national_id"])
    if r.get("phone"): keys.append("ph:" + r["phone"])
    toks = (r.get("name_core") or "").split()
    if r.get("dob"):
        for t in toks:
            keys.append(f"dn:{r['dob']}|{t if len(t) < 5 else t[:4]}")
        if not toks:
            keys.append("dob:" + r["dob"])
    big = sorted(toks, key=lambda t: (-len(t), t))[:2]
    if len(big) == 2:
        keys.append("nt:" + "|".join(sorted(t[:4] for t in big)))
    elif len(big) == 1:
        keys.append("n1:" + big[0][:6])
    # v2: كتلة اسم+ام (انتقائية: تصل مباشرة لمرشحي قاعدة الاسم+الام دون المرور بكتل الاسماء الشائعة)
    if r.get("mother_name"):
        mtoks = r["mother_name"].split()
        if toks and mtoks:
            parts = [toks[0][:4]] + ([toks[1][:4]] if len(toks) > 1 else []) + [mtoks[0][:4]]
            keys.append("mn:" + "|".join(parts))
    return keys
