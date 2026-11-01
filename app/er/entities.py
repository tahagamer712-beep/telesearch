"""Unified entity views, admin decisions (confirm / reject / conflict), unmerge, statistics.
The unified record is a VIEW over raw values (nothing is copied or lost); every value keeps its source."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from .. import audit
from . import engine
from .matcher import AUTO_STATES, MATCHER_VERSION
from .schema_map import PERSON_FIELDS, active_mapping_sql

IDENTITY_SINGLE = ("full_name", "mother_name", "alias", "date_of_birth", "sex", "phone", "email", "national_id", "patient_id", "registration_date", "file_status", "address")
FIELD_AR = {"full_name": "الاسم", "mother_name": "اسم الام", "alias": "الكنية/اللقب", "date_of_birth": "تاريخ الميلاد", "sex": "الجنس", "phone": "الهاتف", "email": "الايميل", "national_id": "الهوية الوطنية", "patient_id": "معرف المريض",
            "registration_date": "تاريخ التسجيل", "file_status": "حالة الملف", "address": "العنوان"}
STATE_LABEL = {"EXACT_MATCH": "🟢 EXACT", "HIGH_CONFIDENCE_MATCH": "🟢 HIGH CONFIDENCE", "PROBABLE_MATCH": "🟡 PROBABLE", "POSSIBLE_MATCH": "🟠 POSSIBLE",
               "NEEDS_REVIEW": "🔶 NEEDS REVIEW", "CONFLICT": "🔴 CONFLICT", "NO_MATCH": "⚪ NO MATCH", "SINGLE": "▫️ SINGLE SOURCE"}


import re as _re
_ISO = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _merge_name_variants(slots: dict) -> dict:
    from .matcher import name_similarity
    merged: list[dict] = []
    for key, s in sorted(slots.items(), key=lambda kv: -len(kv[0])):
        for m in merged:
            if name_similarity(key, m["norm"]) == 1.0:
                m["value"].update(s["value"]); m["files"] |= s["files"]
                for src in s["sources"]:
                    if not any(x["record_id"] == src["record_id"] for x in m["sources"]):
                        m["sources"].append(src)
                break
        else:
            merged.append({"value": Counter(s["value"]), "norm": key, "sources": list(s["sources"]), "files": set(s["files"])})
    return {m["norm"]: m for m in merged}


def public_id(eid: int) -> str:
    return f"U-{eid:06d}"


def source_of(row) -> dict:
    return {"record_id": row["record_id"], "file": row["fname"], "file_id": row["file_id"], "archive": row["pname"], "locator": row["locator"], "column": row["col"], "sha256": row["sha256"], "imported_at": row["imp"]}


def entity_values(conn: sqlite3.Connection, entity_id: int):
    """All mapped + unmapped values of an entity with full provenance (one row per stored value)."""
    return conn.execute(
        "SELECT rv.record_id, rv.original_value ov, rv.normalized_value nv, fl.name col, COALESCE(CASE WHEN "
        + active_mapping_sql("fm") + " THEN fm.standard_field END, 'extra:' || fl.name) AS sf, "
        "f.id file_id, f.original_name fname, f.sha256, f.received_at imp, p.original_name pname, r.locator, er.dup_of "
        "FROM entity_records en JOIN record_values rv ON rv.record_id=en.record_id JOIN fields fl ON fl.id=rv.field_id "
        "LEFT JOIN field_mappings fm ON fm.field_id=fl.id JOIN records r ON r.id=rv.record_id JOIN files f ON f.id=r.file_id "
        "LEFT JOIN files p ON p.id=f.parent_file_id JOIN er_records er ON er.record_id=rv.record_id "
        "WHERE en.entity_id=? AND en.active=1 ORDER BY rv.record_id, fl.position", (entity_id,)).fetchall()


def entity_view(conn: sqlite3.Connection, entity_id: int, light: bool = False) -> dict | None:
    ent = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if ent is None:
        return None
    vals = entity_values(conn, entity_id)
    known: dict[str, dict[str, dict]] = defaultdict(dict)
    events: dict[int, dict] = {}
    extras: list[dict] = []
    recs, files = set(), set()
    originals: dict[int, dict] = {}
    for v in vals:
        recs.add(v["record_id"]); files.add(v["file_id"])
        if v["record_id"] not in originals:
            originals[v["record_id"]] = {**source_of(v), "duplicate_of": v["dup_of"]}
        if v["ov"] in (None, ""):
            continue
        sf = v["sf"]
        src = source_of(v)
        if sf in IDENTITY_SINGLE:
            key = v["nv"] or v["ov"]
            slot = known[sf].setdefault(key, {"value": Counter(), "norm": v["nv"], "sources": [], "files": set()})
            slot["value"][v["ov"]] += 1
            if not any(s["record_id"] == src["record_id"] for s in slot["sources"]):
                slot["sources"].append(src)
            slot["files"].add(v["file_id"])
        elif sf.startswith("extra:"):
            extras.append({"field": sf[6:], "value": v["ov"], "source": src})
        else:
            ev = events.setdefault(v["record_id"], {"source": src, "dup_of": v["dup_of"], "fields": {}})
            ev["fields"][sf] = v["ov"]
    fields = {}
    for sf, slots in known.items():
        if sf in ("full_name", "alias"):  # صيغ متوافقة للاسم (همزة/تشكيل/اختصار الاسم الاوسط) = قيمة واحدة بصيغ متعددة، لا تعارض
            slots = _merge_name_variants(slots)
        items = sorted(slots.values(), key=lambda s: (-len(s["files"]), -len(s["sources"]), -max((len([t for t in k[0].split() if len(t) > 1]) for k in s["value"].items()), default=0), s["value"].most_common(1)[0][0]))
        def shown(s, sf=sf):
            top = max(s["value"].items(), key=lambda kv: (len([t for t in kv[0].split() if len(t) > 1]), kv[1]))[0] if sf in ("full_name", "alias") else s["value"].most_common(1)[0][0]
            return s["norm"] if sf in ("date_of_birth", "registration_date") and s["norm"] and _ISO.match(s["norm"]) else top
        fields[sf] = {"preferred": shown(items[0]), "conflict": len(items) > 1,
                      "known": [{"value": shown(s), "variants": sorted(s["value"]), "norm": s["norm"], "n_sources": len(s["sources"]), "n_files": len(s["files"]), "sources": s["sources"]} for s in items]}
    seen, uniq_events = set(), []
    for rid, ev in sorted(events.items()):
        if ev["dup_of"]:  # تكرار تام لصف سابق في نفس الملف: لا يُعدّ حدثاً جديداً (يبقى محفوظاً كسجل)
            continue
        uniq_events.append(ev)
    if light:
        return {"id": entity_id, "public_id": public_id(entity_id), "state": ent["state"], "confidence": ent["confidence"], "matcher_version": ent["matcher_version"], "status": ent["status"],
                "fields": fields, "events": uniq_events, "extras": extras, "record_ids": sorted(recs), "n_files": len(files), "edges": [], "open_issues": {}, "_values": vals, "originals": [originals[r] for r in sorted(originals)]}
    edges = conn.execute(
        "SELECT * FROM entity_matches WHERE record_a IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1) AND record_b IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1)",
        (entity_id, entity_id)).fetchall()
    open_issues = conn.execute(
        "SELECT state, COUNT(*) c FROM entity_matches WHERE decision IS NULL AND state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW','CONFLICT') AND "
        "(record_a IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1) OR record_b IN (SELECT record_id FROM entity_records WHERE entity_id=? AND active=1)) GROUP BY state", (entity_id, entity_id)).fetchall()
    return {"id": entity_id, "public_id": public_id(entity_id), "state": ent["state"], "confidence": ent["confidence"], "matcher_version": ent["matcher_version"], "status": ent["status"],
            "fields": fields, "events": uniq_events, "extras": extras, "record_ids": sorted(recs), "n_files": len(files), "originals": [originals[r] for r in sorted(originals)], "edges": [dict(e) for e in edges],
            "open_issues": {r["state"]: r["c"] for r in open_issues}}


def why(conn, entity_id: int) -> str:
    """Deterministic, local explanation from stored evidence only (never sent to AI)."""
    v = entity_view(conn, entity_id)
    if v is None:
        return "غير موجود"
    lines = [f"سبب التطابق ({v['public_id']}):"]
    if not v["edges"]:
        lines.append("• كيان من مصدر واحد (لا دمج).")
    for e in v["edges"]:
        ev = json.loads(e["evidence"])
        lines.append(f"• سجلان #{e['record_a']} و#{e['record_b']} → {e['state']} ({e['confidence']:.2f}) | قاعدة {ev.get('rule')}")
        for it in ev.get("items", []):
            lines.append(f"   ✓ {it['field']}: {it['kind']} {it.get('detail', '')}".rstrip())
        for c in json.loads(e["conflicts"] or "[]"):
            lines.append(f"   ✗ {c['field']}: {c.get('detail', '')}")
    if v["open_issues"]:
        lines.append("⚠️ مسائل مفتوحة: " + ", ".join(f"{k}={n}" for k, n in v["open_issues"].items()))
    return "\n".join(lines)


# ------------------------------------------------------------------ decisions
def _decide(conn, match_id: int, decision: str, actor: int) -> dict:
    m = conn.execute("SELECT * FROM entity_matches WHERE id=?", (match_id,)).fetchone()
    if m is None:
        raise ValueError("match غير موجود")
    a, b = m["record_a"], m["record_b"]
    conn.execute("UPDATE entity_matches SET decision=?, decided_by=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (decision, actor, match_id))
    if decision == "confirmed":
        conn.execute("INSERT OR REPLACE INTO match_decisions(record_a, record_b, decision, actor) VALUES (?,?,'confirmed',?)", (a, b, actor))
    elif decision == "rejected":
        conn.execute("INSERT OR REPLACE INTO match_decisions(record_a, record_b, decision, actor) VALUES (?,?,'never_match',?)", (a, b, actor))
    else:
        conn.execute("DELETE FROM match_decisions WHERE record_a=? AND record_b=?", (a, b))
    audit.log(conn, actor, audit.INDEX, "match", match_id, {"decision": decision, "a": a, "b": b})
    conn.execute("INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (NULL, ?, ?, ?, ?)",
                 (f"decision:{decision}", json.dumps({"match_id": match_id, "a": a, "b": b}), MATCHER_VERSION, f"admin:{actor}"))
    return engine.recluster(conn, actor=f"admin:{actor}")


def confirm_match(conn, match_id: int, actor: int) -> dict:
    return _decide(conn, match_id, "confirmed", actor)


def reject_match(conn, match_id: int, actor: int) -> dict:
    return _decide(conn, match_id, "rejected", actor)


def mark_conflict(conn, match_id: int, actor: int) -> dict:
    return _decide(conn, match_id, "conflict", actor)


def unmerge(conn, entity_id: int, actor: int, record_ids: list[int] | None = None) -> dict:
    """Split an entity. Adds NEVER MATCH between each split record and the rest, then re-clusters.
    Raw records are untouched, so nothing is lost."""
    mem = [r[0] for r in conn.execute("SELECT record_id FROM entity_records WHERE entity_id=? AND active=1 ORDER BY record_id", (entity_id,))]
    if len(mem) < 2:
        raise ValueError("الكيان من سجل واحد")
    targets = record_ids or mem
    if record_ids is None and len(mem) > 60:
        raise ValueError("الكيان كبير؛ حدّد السجلات المراد فصلها")
    others = lambda t: [m for m in mem if m != t]
    pairs = {tuple(sorted((t, o))) for t in targets for o in (others(t) if record_ids else [m for m in mem if m != t])}
    for a, b in pairs:
        conn.execute("INSERT OR REPLACE INTO match_decisions(record_a, record_b, decision, actor) VALUES (?,?,'never_match',?)", (a, b, actor))
        conn.execute("UPDATE entity_matches SET decision='rejected', decided_by=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE record_a=? AND record_b=?", (actor, a, b))
    conn.execute("INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (?,?,?,?,?)",
                 (entity_id, "unmerge", json.dumps({"records": targets[:200]}), MATCHER_VERSION, f"admin:{actor}"))
    audit.log(conn, actor, audit.INDEX, "entity", entity_id, {"unmerge": len(targets)})
    return engine.recluster(conn, actor=f"admin:{actor}")


# ------------------------------------------------------------------ queries
def review_queue(conn, states=("PROBABLE_MATCH", "POSSIBLE_MATCH", "NEEDS_REVIEW"), limit=8, offset=0):
    q = ",".join("?" * len(states))
    return conn.execute(f"SELECT * FROM entity_matches WHERE decision IS NULL AND state IN ({q}) ORDER BY (state='NEEDS_REVIEW') DESC, confidence DESC, id LIMIT ? OFFSET ?", (*states, limit, offset)).fetchall()


def conflicts(conn, limit=8, offset=0):
    return conn.execute("SELECT * FROM entity_matches WHERE state='CONFLICT' AND (decision IS NULL OR decision='conflict') ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()


def match_detail(conn, match_id: int) -> dict | None:
    m = conn.execute("SELECT * FROM entity_matches WHERE id=?", (match_id,)).fetchone()
    if m is None:
        return None
    def rec(rid):
        r = conn.execute("SELECT er.*, f.original_name fname, p.original_name pname, rc.locator FROM er_records er JOIN files f ON f.id=er.file_id LEFT JOIN files p ON p.id=f.parent_file_id "
                         "JOIN records rc ON rc.id=er.record_id WHERE er.record_id=?", (rid,)).fetchone()
        return dict(r) if r else {}
    return {"match": dict(m), "a": rec(m["record_a"]), "b": rec(m["record_b"]), "evidence": json.loads(m["evidence"]), "conflicts": json.loads(m["conflicts"] or "[]")}


def history(conn, limit=10, offset=0):
    return conn.execute("SELECT * FROM entity_merge_history ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()


def statistics(conn) -> dict:
    """Every number comes from the database (nothing invented)."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    st = {r["state"]: r["c"] for r in conn.execute("SELECT state, COUNT(*) c FROM entity_matches WHERE decision IS NULL GROUP BY state")}
    last = conn.execute("SELECT value FROM settings WHERE key='er_last_run'").fetchone()
    lr = json.loads(last[0]) if last else {}
    total_recs = one("SELECT COUNT(*) FROM records")
    er_recs = one("SELECT COUNT(*) FROM er_records")
    ents = one("SELECT COUNT(*) FROM entities WHERE status='active'")
    return {
        "records_total": total_recs, "records_in_er": er_recs, "records_not_person_data": total_recs - er_recs,
        "exact_duplicate_records": one("SELECT COUNT(*) FROM er_records WHERE dup_of IS NOT NULL"),
        "normalized_duplicate_records": one("SELECT COALESCE(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM er_records GROUP BY fp_norm HAVING COUNT(*)>1)") - one("SELECT COUNT(*) FROM er_records WHERE dup_of IS NOT NULL"),
        "entities": ents, "multi_record_entities": one("SELECT COUNT(*) FROM (SELECT entity_id FROM entity_records WHERE active=1 GROUP BY entity_id HAVING COUNT(*)>1)"),
        "single_record_entities": one("SELECT COUNT(*) FROM (SELECT entity_id FROM entity_records WHERE active=1 GROUP BY entity_id HAVING COUNT(*)=1)"),
        "candidates_compared": lr.get("compared", 0), "skipped_oversized_blocks": lr.get("skipped_blocks", 0),
        "exact": st.get("EXACT_MATCH", 0), "high_confidence": st.get("HIGH_CONFIDENCE_MATCH", 0), "probable": st.get("PROBABLE_MATCH", 0), "possible": st.get("POSSIBLE_MATCH", 0),
        "needs_review": st.get("NEEDS_REVIEW", 0), "conflicts": st.get("CONFLICT", 0),
        "confirmed_by_admin": one("SELECT COUNT(*) FROM entity_matches WHERE decision='confirmed'"), "rejected_by_admin": one("SELECT COUNT(*) FROM entity_matches WHERE decision='rejected'"),
        "auto_accepted": one("SELECT COUNT(*) FROM entity_matches WHERE decision='auto_accepted'"),
        "auto_accepted_skipped": one("SELECT COUNT(*) FROM entity_matches WHERE decision='auto_accepted_skipped'"),
        "fields_needing_review": one("SELECT COUNT(*) FROM field_mappings WHERE confidence IN ('NEEDS_REVIEW','LOW') AND status='auto'"),
        "matcher_version": MATCHER_VERSION, "last_run": lr,
    }
