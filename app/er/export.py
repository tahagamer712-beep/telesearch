"""Unified Excel export: one logical row per unified entity + full provenance sheets.
Streaming (RAM-flat). Conflicts / review items are shown, never hidden."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import entities
from .schema_map import STANDARD_FIELDS
from .xlsx_writer import XlsxBook

EVENT_KINDS = {"Visits": "visit_date", "Lab Tests": "test_date", "Admissions": "admission_date", "Dispenses": "dispense_date"}


def _src(s: dict) -> str:
    return f"{s['archive'] + ' → ' if s['archive'] else ''}{s['file']} / {s['locator']}"


def export_unified(conn, path: Path, stopping=lambda: False, on_progress=None) -> dict:
    book = XlsxBook(path)
    ur = book.sheet("Unified Records", ["Unified ID", "Match State", "Min Confidence", "Preferred Name", "Alternative Names", "Alias", "Patient ID(s)", "National ID(s)", "Preferred DOB", "Alternative DOBs", "Sex",
                                        "Phone(s)", "Registration Date", "Source Files", "Source Records", "Visits", "Lab Tests", "Admissions", "Dispenses", "Has Conflicting Values", "Open Review Items"],
                    [12, 22, 10, 28, 36, 16, 20, 18, 12, 24, 6, 22, 14, 8, 8, 8, 8, 10, 10, 12, 30])
    sv = book.sheet("Source Values", ["Unified ID", "Field", "Original Value", "Normalized Value", "File", "Archive / Parent", "Sheet / Row (locator)", "Column", "Record ID", "Exact Duplicate Row"], [12, 20, 30, 30, 28, 24, 30, 28, 10, 10])
    pm = book.sheet("Possible Matches", ["Match ID", "State", "Confidence", "Rule", "Record A", "Source A", "Name A", "Record B", "Source B", "Name B", "Matching Fields", "Conflicting Fields", "Matcher Version"], [9, 22, 10, 18, 9, 36, 26, 9, 36, 26, 30, 30, 8])
    cf = book.sheet("Conflicts", ["Type", "Ref", "Detail", "Record A", "Source A", "Record B", "Source B", "Unified ID"], [18, 10, 50, 9, 36, 9, 36, 12])
    nr = book.sheet("Needs Review", ["Type", "Ref", "State / Confidence", "Detail", "Source A", "Source B"], [18, 10, 24, 60, 36, 36])
    n_ent = n_conf = 0
    issues = {}
    for r in conn.execute("SELECT em.state, ea.entity_id eid FROM entity_matches em JOIN entity_records ea ON ea.record_id=em.record_a AND ea.active=1 "
                          "WHERE em.decision IS NULL AND em.state IN ('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW','CONFLICT')"):
        issues.setdefault(r["eid"], {}).setdefault(r["state"], 0)
        issues[r["eid"]][r["state"]] += 1
    ids = [r[0] for r in conn.execute("SELECT id FROM entities WHERE status='active' ORDER BY id")]
    for k, eid in enumerate(ids):
        if k % 200 == 0:
            if stopping():
                from ..errors import JobInterrupted
                raise JobInterrupted("stop")
            if on_progress is not None:
                on_progress(k, len(ids))
        v = entities.entity_view(conn, eid, light=True)
        if v is None or not v["record_ids"]:
            continue
        n_ent += 1
        f = v["fields"]
        alts = lambda sf: " | ".join(x["value"] for x in f.get(sf, {}).get("known", [])[1:])
        allv = lambda sf: " | ".join(x["value"] for x in f.get(sf, {}).get("known", []))
        conflicting = [FIELD for FIELD in f if f[FIELD]["conflict"]]
        counts = {name: sum(1 for ev in v["events"] if sf in ev["fields"]) for name, sf in EVENT_KINDS.items()}
        iss = issues.get(eid, {})
        pref = lambda sf: f.get(sf, {}).get("preferred", "")
        ur.write([v["public_id"], v["state"], v["confidence"], pref("full_name"), alts("full_name"), allv("alias"), allv("patient_id"), allv("national_id"), pref("date_of_birth"), alts("date_of_birth"),
                  pref("sex"), allv("phone"), pref("registration_date"), v["n_files"], len(v["record_ids"]), counts["Visits"], counts["Lab Tests"], counts["Admissions"], counts["Dispenses"],
                  ", ".join(conflicting), ", ".join(f"{s}={c}" for s, c in iss.items())])
        for fld in conflicting:
            n_conf += 1
            cf.write(["VALUE_CONFLICT", v["public_id"], f"{fld}: " + " ≠ ".join(f"{x['value']} ({x['n_sources']} src)" for x in f[fld]["known"]), "", "", "", "", v["public_id"]])
        for row in v["_values"]:
            if row["ov"] in (None, ""):
                continue
            sv.write([v["public_id"], row["sf"], row["ov"], row["nv"], row["fname"], row["pname"] or "", row["locator"], row["col"], row["record_id"], "yes" if row["dup_of"] else ""])
    def rec_info(rid):
        r = conn.execute("SELECT er.name_norm, f.original_name fname, p.original_name pname, rc.locator FROM er_records er JOIN files f ON f.id=er.file_id LEFT JOIN files p ON p.id=f.parent_file_id JOIN records rc ON rc.id=er.record_id WHERE er.record_id=?", (rid,)).fetchone()
        return (f"{r['pname'] + ' → ' if r['pname'] else ''}{r['fname']} / {r['locator']}", r["name_norm"] or "") if r else ("", "")
    for m in conn.execute("SELECT * FROM entity_matches WHERE decision IS NULL OR decision='conflict' ORDER BY id"):
        sa, na = rec_info(m["record_a"]); sb, nb = rec_info(m["record_b"])
        ev = json.loads(m["evidence"]); cfl = json.loads(m["conflicts"] or "[]")
        if m["state"] in ("PROBABLE_MATCH", "POSSIBLE_MATCH"):
            pm.write([m["id"], m["state"], m["confidence"], ev.get("rule"), m["record_a"], sa, na, m["record_b"], sb, nb, ", ".join(i["field"] for i in ev.get("items", [])), ", ".join(c["field"] for c in cfl), m["matcher_version"]])
        elif m["state"] == "CONFLICT" or m["decision"] == "conflict":
            n_conf += 1
            cf.write(["MATCH_CONFLICT", m["id"], "; ".join(f"{c['field']}: {c.get('detail', '')} ({c.get('a')} vs {c.get('b')})" for c in cfl), m["record_a"], sa, m["record_b"], sb, ""])
        elif m["state"] == "NEEDS_REVIEW":
            nr.write(["MATCH_REVIEW", m["id"], f"{m['state']} / {m['confidence']:.2f}", "; ".join(f"{c['field']}: {c.get('detail', '')}" for c in cfl) or ev.get("rule", ""), sa, sb])
    for fm in conn.execute("SELECT fl.name, f.original_name fname, fm.reason, fm.confidence FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id JOIN files f ON f.id=fl.file_id WHERE fm.confidence IN ('NEEDS_REVIEW','LOW') AND fm.status='auto'"):
        nr.write(["FIELD_MAPPING", "", fm["confidence"], f"{fm['name']}: {fm['reason']}", fm["fname"], ""])
    st = entities.statistics(conn)
    ms = book.sheet("Match Statistics", ["Metric", "Value"], [40, 24])
    ms.write(["Generated", datetime.now().isoformat(timespec="seconds")])
    for k, v in st.items():
        if k != "last_run":
            ms.write([k, v])
    ms.write(["REAL-WORLD MATCHING ACCURACY", "UNVERIFIED (measured only on synthetic data; see docs/MATCHING.md)"])
    sm = book.sheet("Schema Mapping", ["File", "Field", "Standard Field", "Confidence", "Status", "Reason"], [28, 36, 20, 14, 10, 60])
    for r in conn.execute("SELECT f.original_name fname, fl.name, fm.standard_field, fm.confidence, fm.status, fm.reason FROM field_mappings fm JOIN fields fl ON fl.id=fm.field_id JOIN files f ON f.id=fl.file_id ORDER BY f.id, fl.position"):
        sm.write([r["fname"], r["name"], r["standard_field"] or "(preserved as extra field)", r["confidence"], r["status"], r["reason"]])
    book.close()
    return {"entities": n_ent, "conflicts": n_conf, "path": str(path), "needs_review_items": st["needs_review"], "possible": st["possible"] + st["probable"]}
