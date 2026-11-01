"""Person-fused unified export: ONE row per person, complete fused info.

Owner order (2026-10-29 04:45): the Excel merge button must merge PERSONS,
not concatenate records. The raw Master (master_excel) lists every record
(so one person appears 20 times in 20 rows) — exactly what the owner
rejects. This module is the new capability; it does NOT change any
existing button (see the button-mapping proposal in the parent report).

Strict fusion rules (applied at export time, on top of ER entities):
* Base units are the active ER entities (entity_view, light). Entities
  already fuse records via the matcher; this export fuses ENTITIES further
  only under the owner-agreed key: EXACT normalized full name AND EXACT
  normalized mother name (sorted-token equality, i.e. name_similarity 1.0).
  Name alone never fuses (mother missing on either side -> held separate).
* Guards that can NEVER be overridden by this export:
  - strong IDs: a fused person carries at most ONE distinct patient_id and
    ONE distinct national_id. Merging two components whose union would
    hold >1 distinct strong ID of either kind is BLOCKED (that is a
    CONFLICT determination, not a review item).
  - human NEVER_MATCH veto (match_decisions) between any cross records
    blocks the fusion.
  - a stored CONFLICT entity_match (state CONFLICT or decision conflict)
    between any cross records blocks the fusion.
* Transitive fusion uses union-find with the same guards re-checked at
  every union, so a chain cannot smuggle a blocked pair in through a
  third entity.
* Nothing is hidden: entities held separate despite sharing a name are
  listed on the "Held Separate" sheet with the reason; blocked fusions
  are listed on the "Conflicts" sheet. A fused row contains EVERY
  distinct value from its member records (preferred first, alternatives
  joined with " | "), so no information is dropped by fusing.

AI columns (owner explicitly ordered AI column naming/placement):
* When the bot config has an OpenRouter key AND model (cfg.ai_enabled),
  column NAMES are inferred by the existing app/ai/organize.py
  infer_schema() from a small sample (<=25 persons, field labels only in
  substance). organize.py masks phones/IDs/emails before anything leaves
  the device (names stay visible by design — extraction cannot name a
  column for a field it cannot see). That masking is exactly why PLACEMENT
  cannot be pure LLM here: an LLM that never saw a raw phone/ID value
  cannot return it verbatim. So placement is a deterministic field->AI
  column mapping (normalized-name matching against the AI names), and the
  output metadata labels this honestly: column_names=AI, placement=
  rule-based mapping. Fused fields with no AI column are APPENDED as
  rule-based columns, so AI naming never loses data.
* When no key/model is configured, NO LLM call is made at all and the
  export uses rule-based columns, labeled plainly as such
  (ai_status="rule_based_ai_unavailable"). This is BLOCKED_EXTERNAL_
  DEPENDENCY for the AI-naming feature only — the fused file itself
  still builds. The key the owner must set is OPENROUTER_API_KEY plus
  OPENROUTER_MODEL in the bot .env (same keys the bot's AI provider
  uses); there is no separate person-export key.
* If the LLM call fails (network, bad key, bad JSON), the export falls
  back to rule-based columns (ai_status="ai_failed:<reason>") instead of
  failing the whole file. Re-running after fixing the key retries AI.

Streaming: rows are written via XlsxBook (temp XML, RAM-flat), with a
stopping() check every 200 persons. The export is deterministic and
re-runnable (a new file per run; no database writes except the caller's
reports row).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from ..norm.arabic import normalize_text
from . import entities
from .matcher import name_similarity
from .schema_map import BUILTIN, STANDARD_FIELDS, norm_header
from .xlsx_writer import XlsxBook

IDENTITY_ORDER = ("full_name", "mother_name", "alias", "date_of_birth", "sex",
                  "phone", "email", "national_id", "patient_id",
                  "registration_date", "file_status", "address")
FIELD_AR = entities.FIELD_AR
PLACEHOLDERS = {"", "0", "00", "000", "n/a", "na", "null", "none", "unknown",
                "-", "--", "غير معروف", "لايوجد", "لا يوجد"}


def _key(norm: str | None) -> str | None:
    """Sorted-token key: equality here == name_similarity 1.0 for normalized names."""
    if not norm:
        return None
    toks = sorted(normalize_text(norm).split())
    return " ".join(toks) if toks else None


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v not in PLACEHOLDERS else None


def _entity_facts(conn: sqlite3.Connection, eid: int) -> dict | None:
    """Entity view reduced to what fusion needs (never raw record contents beyond the view)."""
    v = entities.entity_view(conn, eid, light=True)
    if v is None or not v["record_ids"]:
        return None
    fields = v["fields"]
    def known_norms(sf):
        out = set()
        for item in fields.get(sf, {}).get("known", []):
            k = _key(item.get("norm") or item.get("value"))
            if k:
                out.add(k)
        return out
    def known_values(sf):
        out = []
        for item in fields.get(sf, {}).get("known", []):
            val = item.get("value")
            if val not in (None, ""):
                out.append(str(val))
        return out
    name_keys = known_norms("full_name")
    mother_keys = known_norms("mother_name")
    pids = {x for x in known_values("patient_id") if _clean(x)}
    nids = {x for x in known_values("national_id") if _clean(x)}
    return {"eid": eid, "public_id": v["public_id"], "view": v,
            "name_keys": name_keys, "mother_keys": mother_keys,
            "pids": pids, "nids": nids, "record_ids": set(v["record_ids"]),
            "n_files": v["n_files"]}


def _load_guards(conn: sqlite3.Connection):
    """Veto + conflict record pairs (sorted tuples) for fusion blocking."""
    veto = set()
    for r in conn.execute("SELECT record_a, record_b FROM match_decisions WHERE decision='never_match'"):
        veto.add((r[0], r[1]) if r[0] < r[1] else (r[1], r[0]))
    conflict = set()
    for r in conn.execute("SELECT record_a, record_b FROM entity_matches WHERE state='CONFLICT' OR decision='conflict'"):
        conflict.add((r[0], r[1]) if r[0] < r[1] else (r[1], r[0]))
    veto_partners: dict[int, set[int]] = defaultdict(set)
    conflict_partners: dict[int, set[int]] = defaultdict(set)
    for a, b in veto:
        veto_partners[a].add(b); veto_partners[b].add(a)
    for a, b in conflict:
        conflict_partners[a].add(b); conflict_partners[b].add(a)
    return veto_partners, conflict_partners


class _UF:
    def __init__(self):
        self.parent: dict[int, int] = {}
        self.members: dict[int, list[int]] = {}
        self.pids: dict[int, set] = {}
        self.nids: dict[int, set] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        self.members.setdefault(x, [x])
        self.pids.setdefault(x, set())
        self.nids.setdefault(x, set())
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b, pids_a, nids_a, pids_b, nids_b):
        """Union two ENTITY ids; returns False if strong IDs would conflict."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if len(self.pids[ra] | self.pids[rb]) > 1 or len(self.nids[ra] | self.nids[rb]) > 1:
            return False
        small, big = (ra, rb) if len(self.members[ra]) < len(self.members[rb]) else (rb, ra)
        self.parent[small] = big
        self.members[big].extend(self.members.pop(small))
        self.pids[big] |= self.pids.pop(small)
        self.nids[big] |= self.nids.pop(small)
        return True


def fuse_persons(conn: sqlite3.Connection):
    """Return (persons, held, blocked, stats).

    persons: list of dicts {entity_ids, public_ids, views, record_ids,
    n_files, basis}. held: per-entity rows for name collisions kept
    separate. blocked: pairwise fusions refused by a guard.
    """
    ids = [r[0] for r in conn.execute("SELECT id FROM entities WHERE status='active' ORDER BY id")]
    facts = {}
    for eid in ids:
        f = _entity_facts(conn, eid)
        if f is not None:
            facts[eid] = f
    veto_partners, conflict_partners = _load_guards(conn)
    uf = _UF()
    for eid, f in facts.items():
        uf.find(eid)
        uf.pids[eid] |= f["pids"]
        uf.nids[eid] |= f["nids"]
    # Candidate buckets: exact (name_key, mother_key) pairs only.
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for eid, f in facts.items():
        for nk in f["name_keys"]:
            for mk in f["mother_keys"]:
                buckets[(nk, mk)].append(eid)
    blocked: list[dict] = []
    blocked_pairs: set[tuple[int, int]] = set()
    for bucket in buckets.values():
        bucket = sorted(set(bucket))
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if uf.find(a) == uf.find(b):
                    continue
                fa, fb = facts[a], facts[b]
                # Guard 1: veto between any cross records?
                rec_a, rec_b = fa["record_ids"], fb["record_ids"]
                veto_hit = any(veto_partners.get(r, set()) & rec_b for r in rec_a)
                conf_hit = any(conflict_partners.get(r, set()) & rec_b for r in rec_a)
                if veto_hit or conf_hit:
                    reason = "never_match veto" if veto_hit else "CONFLICT match"
                    key = (a, b)
                    if key not in blocked_pairs:
                        blocked_pairs.add(key)
                        blocked.append({"entity_a": a, "entity_b": b, "reason": reason,
                                        "name": fa["view"]["fields"].get("full_name", {}).get("preferred", ""),
                                        "mother": fa["view"]["fields"].get("mother_name", {}).get("preferred", "")})
                    continue
                # Guard 2: strong IDs (checked inside union, component-wide).
                if not uf.union(a, b, fa["pids"], fa["nids"], fb["pids"], fb["nids"]):
                    key = (a, b)
                    if key not in blocked_pairs:
                        blocked_pairs.add(key)
                        blocked.append({"entity_a": a, "entity_b": b, "reason": "strong-ID conflict",
                                        "name": fa["view"]["fields"].get("full_name", {}).get("preferred", ""),
                                        "mother": fa["view"]["fields"].get("mother_name", {}).get("preferred", "")})
    # Components -> persons.
    comp: dict[int, list[int]] = defaultdict(list)
    for eid in facts:
        comp[uf.find(eid)].append(eid)
    persons = []
    for root in sorted(comp, key=lambda r: min(comp[r])):
        eids = sorted(comp[root])
        recs: set[int] = set()
        n_files = 0
        for eid in eids:
            recs |= facts[eid]["record_ids"]
            n_files = max(n_files, facts[eid]["n_files"])
        # n_files is a max, not a sum: file overlap across entities is
        # unknown from views alone; recompute exactly from records below.
        files = {r[0] for r in conn.execute(
            "SELECT DISTINCT file_id FROM records WHERE id IN (%s)" % ",".join("?" * len(recs)), sorted(recs))} if recs else set()
        persons.append({"entity_ids": eids,
                        "public_ids": [facts[e]["public_id"] for e in eids],
                        "views": [facts[e]["view"] for e in eids],
                        "record_ids": recs, "n_files": len(files),
                        "basis": "name+mother" if len(eids) > 1 else "single entity"})
    # Held-separate: entities sharing a normalized name with another entity
    # but in a different component, with the dominant reason.
    name_to_entities: dict[str, list[int]] = defaultdict(list)
    for eid, f in facts.items():
        for nk in f["name_keys"]:
            name_to_entities[nk].append(eid)
    comp_of = {eid: uf.find(eid) for eid in facts}
    held: list[dict] = []
    for eid in sorted(facts):
        f = facts[eid]
        others = set()
        for nk in f["name_keys"]:
            others.update(name_to_entities[nk])
        others.discard(eid)
        others = {o for o in others if comp_of[o] != comp_of[eid]}
        if not others:
            continue
        reasons = set()
        for o in sorted(others):
            fo = facts[o]
            if not f["mother_keys"] or not fo["mother_keys"]:
                reasons.add("mother missing (not fused by name alone)")
            elif not (f["mother_keys"] & fo["mother_keys"]):
                reasons.add("mother different")
            elif (eid, o) in blocked_pairs or (o, eid) in blocked_pairs:
                br = next((b["reason"] for b in blocked if {b["entity_a"], b["entity_b"]} == {eid, o}), "guard blocked")
                reasons.add(br)
            else:
                reasons.add("held separate")
        held.append({"entity": eid, "public_id": f["public_id"],
                     "name": f["view"]["fields"].get("full_name", {}).get("preferred", ""),
                     "mother": f["view"]["fields"].get("mother_name", {}).get("preferred", ""),
                     "n_name_collisions": len(others),
                     "other_entities": sorted(others)[:5],
                     "reason": "; ".join(sorted(reasons))})
    stats = {"entities_in": len(facts), "persons": len(persons),
             "fused_groups": sum(1 for p in persons if len(p["entity_ids"]) > 1),
             "single_source_persons": sum(1 for p in persons if len(p["entity_ids"]) == 1),
             "held_separate": len(held), "blocked_pairs": len(blocked)}
    return persons, held, blocked, stats


def _fused_field_values(person: dict):
    """sf -> ordered distinct values across member entities.

    Identity fields: preferred value first, then alternatives. Other
    fields: frequency desc, then first appearance. Values come from the
    entity views' _values (all stored values, extras included).
    """
    per_sf: dict[str, list[str]] = {}
    counts: dict[str, Counter] = defaultdict(Counter)
    order: dict[str, list[str]] = defaultdict(list)
    preferred: dict[str, str] = {}
    for v in person["views"]:
        for sf, f in v["fields"].items():
            if f.get("preferred"):
                preferred.setdefault(sf, f["preferred"])
        for row in v["_values"]:
            ov = row["ov"]
            if ov in (None, ""):
                continue
            ov = str(ov)
            if ov not in order[row["sf"]]:
                order[row["sf"]].append(ov)
            counts[row["sf"]][ov] += 1
    for sf, vals in order.items():
        if sf in preferred and preferred[sf] in vals:
            rest = sorted((x for x in vals if x != preferred[sf]),
                          key=lambda x: (-counts[sf][x], vals.index(x)))
            per_sf[sf] = [preferred[sf]] + rest
        else:
            per_sf[sf] = sorted(vals, key=lambda x: (-counts[sf][x], vals.index(x)))
    return per_sf


def _rule_columns(all_sfs: set[str]):
    """Deterministic rule-based column order: identity first, then other
    standard fields in schema order, then extras sorted."""
    cols = [sf for sf in IDENTITY_ORDER if sf in all_sfs]
    cols += [sf for sf in STANDARD_FIELDS if sf in all_sfs and sf not in cols]
    cols += sorted(sf[6:] if sf.startswith("extra:") else sf for sf in all_sfs
                   if sf not in cols and (sf.startswith("extra:") or sf not in STANDARD_FIELDS))
    # Normalize extra columns back to their fused-field key form.
    out = []
    for c in cols:
        if c in all_sfs:
            out.append(c)
        elif ("extra:" + c) in all_sfs:
            out.append("extra:" + c)
    return out


def _ai_map_columns(ai_columns: list[str], fused_keys: set[str]):
    """Map AI column names to fused-field keys by normalized matching.

    Returns (mapping ai_col -> fused_key or None, appended rule keys).
    Matching: exact normalized equality, then token-set equality, then
    synonym lists (BUILTIN) — never by position. Unmapped AI columns stay
    as empty columns (the AI schema is honored); unmapped fused fields
    are appended as rule columns so no data is lost.
    """
    def norm(s):
        return norm_header(s)
    # Candidate names per fused key: rule label + synonyms.
    candidates: dict[str, list[str]] = {}
    for k in fused_keys:
        names = []
        if k in FIELD_AR:
            names.append(FIELD_AR[k])
        if k.startswith("extra:"):
            names.append(k[6:])
        else:
            names.append(k)
        for syn in BUILTIN.get(k, []):
            names.append(syn)
        candidates[k] = [norm(n) for n in names if norm(n)]
    mapping: dict[str, str | None] = {}
    used: set[str] = set()
    for ai in ai_columns:
        na = norm(ai)
        hit = None
        for k, names in candidates.items():
            if k in used:
                continue
            if na in names:
                hit = k; break
        if hit is None:
            for k, names in candidates.items():
                if k in used:
                    continue
                if any(set(na.split()) == set(n.split()) and n for n in names):
                    hit = k; break
        if hit is not None:
            used.add(hit)
        mapping[ai] = hit
    appended = [k for k in _rule_columns(fused_keys) if k not in used]
    return mapping, appended


def _ai_columns_for_export(fused_by_person: list[dict], cfg, allow_ai: bool):
    """Decide display columns. Returns dict with keys:
    display: list of (header, fused_key or None) in order,
    ai_status, ai_columns, appended_rule_columns, unmapped_ai_columns.
    """
    all_sfs: set[str] = set()
    for fv in fused_by_person:
        all_sfs.update(fv.keys())
    rule_keys = _rule_columns(all_sfs)
    if not allow_ai or not getattr(cfg, "ai_enabled", False):
        return {"display_keys": rule_keys, "ai_status": "rule_based_ai_unavailable",
                "ai_columns": [], "mapping": {}, "appended_rule_columns": [],
                "unmapped_ai_columns": []}
    # Build sample lines (<=25 persons, values truncated: naming only).
    from ..ai import organize
    sample_lines = []
    for fv in fused_by_person[:25]:
        parts = []
        for sf in rule_keys:
            if sf in fv:
                label = FIELD_AR.get(sf, sf[6:] if sf.startswith("extra:") else sf)
                vals = " | ".join(str(x)[:60] for x in fv[sf][:3])
                parts.append(f"{label}: {vals}")
        sample_lines.append(" | ".join(parts))
    try:
        ai_names, _data, _usage = organize.infer_schema(
            sample_lines, cfg.openrouter_model, cfg.openrouter_api_key, cfg.openrouter_base_url)
    except Exception as exc:  # network / bad key / bad JSON -> honest fallback
        return {"display_keys": rule_keys, "ai_status": f"ai_failed:{type(exc).__name__}",
                "ai_columns": [], "mapping": {}, "appended_rule_columns": [],
                "unmapped_ai_columns": []}
    ai_names = [str(c).strip() for c in ai_names if str(c).strip()]
    # Deduplicate preserving order.
    seen, uniq = set(), []
    for c in ai_names:
        if c not in seen:
            seen.add(c); uniq.append(c)
    mapping, appended = _ai_map_columns(uniq, all_sfs)
    unmapped = [c for c in uniq if mapping[c] is None]
    return {"display_keys": None, "ai_status": "ai",
            "ai_columns": uniq, "mapping": mapping, "appended_rule_columns": appended,
            "unmapped_ai_columns": unmapped}


def export_persons(conn: sqlite3.Connection, path: Path, cfg=None, allow_ai: bool = True,
                   stopping=lambda: False) -> dict:
    """Write the person-fused workbook. Returns stats incl. ai_status.

    cfg may be None (tests without AI config) -> rule-based columns.
    """
    persons, held, blocked, stats = fuse_persons(conn)
    fused_by_person = [_fused_field_values(p) for p in persons]
    if cfg is None:
        class _C:  # minimal stand-in: AI disabled
            ai_enabled = False
        cfg = _C()
    dec = _ai_columns_for_export(fused_by_person, cfg, allow_ai)
    book = XlsxBook(Path(path))
    # Main sheet.
    if dec["ai_status"] == "ai":
        headers = ["Person ID"] + dec["ai_columns"]
        # Appended rule columns get human labels.
        headers += [FIELD_AR.get(k, k[6:] if k.startswith("extra:") else k) for k in dec["appended_rule_columns"]]
        headers += ["Source Files", "Source Records", "Merged Entities", "Match Basis"]
        main = book.sheet("Persons", headers, [12] + [24] * (len(headers) - 5) + [10, 10, 28, 16])
        for idx, (p, fv) in enumerate(zip(persons, fused_by_person)):
            if idx % 200 == 0 and stopping():
                from ..errors import JobInterrupted
                raise JobInterrupted("stop")
            row = [f"P-{idx + 1:06d}"]
            for ai in dec["ai_columns"]:
                k = dec["mapping"][ai]
                row.append(" | ".join(fv[k]) if k and k in fv else "")
            for k in dec["appended_rule_columns"]:
                row.append(" | ".join(fv[k]) if k in fv else "")
            row += [p["n_files"], len(p["record_ids"]), ", ".join(p["public_ids"]), p["basis"]]
            main.write(row)
    else:
        keys = dec["display_keys"]
        headers = ["Person ID"] + [FIELD_AR.get(k, k[6:] if k.startswith("extra:") else k) for k in keys]
        headers += ["Source Files", "Source Records", "Merged Entities", "Match Basis"]
        main = book.sheet("Persons", headers, [12] + [24] * (len(headers) - 5) + [10, 10, 28, 16])
        for idx, (p, fv) in enumerate(zip(persons, fused_by_person)):
            if idx % 200 == 0 and stopping():
                from ..errors import JobInterrupted
                raise JobInterrupted("stop")
            row = [f"P-{idx + 1:06d}"]
            for k in keys:
                row.append(" | ".join(fv[k]) if k in fv else "")
            row += [p["n_files"], len(p["record_ids"]), ", ".join(p["public_ids"]), p["basis"]]
            main.write(row)
    hs = book.sheet("Held Separate", ["Entity", "Name", "Mother", "Name Collisions", "Other Entities", "Reason"],
                    [12, 28, 28, 12, 28, 60])
    for h in held:
        hs.write([h["public_id"], h["name"], h["mother"], h["n_name_collisions"],
                  ", ".join(f"U-{e:06d}" for e in h["other_entities"]), h["reason"]])
    cf = book.sheet("Conflicts", ["Entity A", "Entity B", "Name", "Mother", "Reason"],
                    [12, 12, 28, 28, 30])
    for b in blocked:
        cf.write([f"U-{b['entity_a']:06d}", f"U-{b['entity_b']:06d}", b["name"], b["mother"], b["reason"]])
    info = book.sheet("Export Info", ["Item", "Value"], [40, 80])
    info.write(["Generated", datetime.now().isoformat(timespec="seconds")])
    info.write(["Persons (one row each)", stats["persons"]])
    info.write(["Entities in", stats["entities_in"]])
    info.write(["Fused groups (2+ entities)", stats["fused_groups"]])
    info.write(["Single-entity persons", stats["single_source_persons"]])
    info.write(["Held separate entities", stats["held_separate"]])
    info.write(["Blocked fusions", stats["blocked_pairs"]])
    info.write(["Column naming", "AI (OpenRouter via app/ai/organize.py infer_schema)" if dec["ai_status"] == "ai" else
                ("rule-based (AI unavailable: set OPENROUTER_API_KEY + OPENROUTER_MODEL in the bot .env)" if dec["ai_status"] == "rule_based_ai_unavailable" else f"rule-based fallback ({dec['ai_status']})")])
    info.write(["Column placement", "rule-based field->AI-column mapping (LLM masks phones/IDs/emails, so it cannot place raw identifiers verbatim)" if dec["ai_status"] == "ai" else "rule-based"])
    if dec["ai_status"] == "ai":
        info.write(["AI columns", ", ".join(dec["ai_columns"])])
        info.write(["Unmapped AI columns (kept empty)", ", ".join(dec["unmapped_ai_columns"]) or "(none)"])
        info.write(["Appended rule columns (no AI column matched)", ", ".join(dec["appended_rule_columns"]) or "(none)"])
    info.write(["Matching rule", "exact normalized name + exact normalized mother; strong-ID conflict / NEVER_MATCH veto / CONFLICT match never merge"])
    info.write(["Accuracy note", "UNVERIFIED on real data: strict rules minimize false merges but cannot eliminate them (two different people can share a name and a mother name); CONFLICT/veto rows never merge"])
    book.close()
    return {**stats, "ai_status": dec["ai_status"],
            "ai_columns": dec["ai_columns"], "appended_rule_columns": dec["appended_rule_columns"],
            "unmapped_ai_columns": dec["unmapped_ai_columns"], "path": str(path)}
