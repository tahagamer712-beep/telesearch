"""ER auto-accept: resolve review-queue matches without human approval.

Owner order (2026-10-29): the review queue (PROBABLE_MATCH / POSSIBLE_MATCH /
NEEDS_REVIEW, ~285k held items) cannot be human-reviewed, so when the
reversible setting ``er_auto_accept`` is ON, review matches resolve
automatically and the already-held rows are applied by a chunked, resumable
backfill job (``er_backfill``) that never monopolizes the single worker:
each invocation dispositions at most one chunk, commits, reports progress,
and chains the next chunk to the BACK of the queue, so ingests submitted
meanwhile run first.

REFINED OWNER POLICY (2026-10-29 05:53, overrides the original build):
the manual-approval gate is CANCELLED. PROBABLE_MATCH and POSSIBLE_MATCH
still auto-merge; NEEDS_REVIEW does NOT merge and is NOT held for manual
review — it is resolved as SEPARATE (two different people) with a terminal
decision and an audit note, never merged, never queued for approval. The
review screen itself stays in code for manual correction/override only;
it is no longer a mandatory step before any resolution.

GUARD POLICY (explicit, and the price of automation):
* PROBABLE_MATCH / POSSIBLE_MATCH merge when ON, including mother-typo,
  ID-mother-diff, ID-sex-diff, ID-name-different, name-only and other
  low-confidence rules. Wrong merges WILL happen. Confidence here is a
  fixed rule lookup, NOT a calibrated accuracy, so no accuracy figure
  is promised.
* NEEDS_REVIEW NEVER merges when ON: it is resolved as SEPARATE people
  (decision ``auto_accepted_skipped`` + a kind=separated audit note, see
  ``separate_needs_review``). Separation is pairwise only: it does not
  veto other edges involving the same records, and it never claims a
  partner in ambiguity handling.
* CONFLICT states (different national IDs, exact name + totally different
  mother, conflicting ID+DOB) NEVER merge, even when ON: they are not "held
  for review" at all, they are determinations that the pair are DIFFERENT
  people. The matcher itself says two different identifiers => never merge.
  (Unchanged from the original build: CONFLICT rows stay untouched NULL;
  "stays separate" is via singleton entities, as before.)
* Ambiguity (a record without any strong ID matching two or more people
  with different strong IDs): the record merges with the BEST partner only
  (confidence DESC, match id ASC). The losing edges are terminally marked
  ``auto_accepted_skipped``, NOT merged: one record can belong to only one
  entity, and merging both partners would fuse two different-ID people
  into one entity with certainty, not probability.
* Transitive guard: a merge that would combine components with DIFFERENT
  strong identifiers (patient_id or national_id) is BLOCKED and the edge
  is terminally marked ``auto_accepted_skipped``. The guard is RELAXED for
  dob and mother_name under auto-accept (typos in both are common in Iraqi
  data, and the matcher already treats dob-typo as review, not conflict).
* A human NEVER_MATCH veto (match_decisions) is ALWAYS respected, even
  when ON: an admin veto is never overridden by automation.
* Turning the setting OFF does not unmerge anything already merged, and
  does not re-merge anything already separated. The remedy for a wrong
  merge is the existing admin path (never_match + recluster / unmerge).
  At 285k scale, per-item unmerge is infeasible; that practical
  irreversibility is part of the price.

Separation value (why ``auto_accepted_skipped`` + a kind=separated note):
migration 011 is preserved as-is, so no new decision value exists; a new
value would need a migration change. ``rejected`` would misreport admin
counts (the reason 011 added honest values in the first place). Flipping
state to NO_MATCH is NOT terminal: run_matching's ON CONFLICT clause
overwrites NULL-decision rows, so a later re-match could resurrect the
pair, and it would destroy the matcher's NEEDS_REVIEW output. The
decision + note is terminal, distinguishable from ambiguity/transitive
skips (note kind), and countable as separated (state NEEDS_REVIEW +
decision auto_accepted_skipped + kind=separated note; backfill
checkpoints also carry a separated total).

Live behaviour when ON: recluster edge selection becomes decision IN
(confirmed, auto_accepted) OR (decision IS NULL AND state IN AUTO+MERGE
states). Merged NULL PROBABLE/POSSIBLE edges are marked auto_accepted,
blocked ones auto_accepted_skipped, NULL NEEDS_REVIEW rows are separated
terminally before edge selection, so the review queue empties;
AUTO-state NULL edges stay NULL exactly as today (still counted as
exact/high). When OFF, the edge set is identical to today (confirmed +
NULL+AUTO), except that already-accepted AND already-separated rows
remain decided (they stand, OFF neither unmerges nor re-merges).

Schema note: ``entity_matches.decision`` is CHECK-constrained, so this
feature adds migration 011 with two honest decision values
(``auto_accepted``, ``auto_accepted_skipped``) instead of overloading
``confirmed``/``rejected`` (which would misreport admin counts).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from .matcher import AUTO_STATES, MATCHER_VERSION, REVIEW_STATES

SETTING_KEY = "er_auto_accept"
DECISION_ACCEPTED = "auto_accepted"
DECISION_SKIPPED = "auto_accepted_skipped"
DEFAULT_CHUNK = 1000
MAX_CHUNK = 5000
REVIEW_IN = "('PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW')"
# Refined owner policy (2026-10-29): only PROBABLE/POSSIBLE merge.
# NEEDS_REVIEW never merges under auto-accept: it is resolved as SEPARATE
# (two different people) with a terminal decision + audit note, never held
# for manual approval. The manual-approval gate is cancelled; the review
# screen stays in code for manual correction/override only.
MERGE_REVIEW_STATES = ("PROBABLE_MATCH", "POSSIBLE_MATCH")
SEPARATE_REVIEW_STATES = ("NEEDS_REVIEW",)
SEPARATED_NOTE_DETAIL = ("owner rule 2026-10-29: NEEDS_REVIEW resolved as separate people "
                         "under auto-accept; no manual approval, never merged")


def is_enabled(conn: sqlite3.Connection) -> bool:
    """Missing key == OFF (safe default: review behavior unchanged)."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
    return bool(row and row[0] == "1")


def set_enabled(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SETTING_KEY, "1" if on else "0"))


def mergable_states(enabled: bool) -> tuple[str, ...]:
    """States whose decision-NULL rows count as merge edges in recluster.

    When ON, only PROBABLE/POSSIBLE review rows are edges. NEEDS_REVIEW
    rows are separated terminally (see separate_needs_review) and are
    never edges, so they can never merge, directly or transitively.
    """
    return tuple(AUTO_STATES) + tuple(MERGE_REVIEW_STATES) if enabled else tuple(AUTO_STATES)


def separate_needs_review(conn: sqlite3.Connection, match_id: int) -> None:
    """Terminally resolve one NEEDS_REVIEW row as SEPARATE people.

    Decision value: the frozen migration-011 value 'auto_accepted_skipped'
    (terminally dispositioned without merging) + a kind=separated audit
    note, so the row is distinguishable from ambiguity/transitive skips
    and countable as separated. A new decision value would need a
    migration change (forbidden: 011 is preserved as-is); 'rejected'
    would misreport admin counts; flipping state to NO_MATCH is not
    terminal (a later re-match overwrites NULL-decision rows) and would
    destroy the matcher's NEEDS_REVIEW output. Separation is pairwise
    only: it does not veto other edges involving the same records, and
    it creates no entity membership (records stay separate; recluster's
    singletons give each its own entity).
    """
    _mark(conn, match_id, DECISION_SKIPPED,
          {"field": "auto_accept", "kind": "separated", "detail": SEPARATED_NOTE_DETAIL})


def _append_note(conn: sqlite3.Connection, match_id: int, note: dict) -> None:
    row = conn.execute("SELECT conflicts FROM entity_matches WHERE id=?", (match_id,)).fetchone()
    try:
        items = json.loads(row[0]) if row and row[0] else []
    except Exception:
        items = []
    if not isinstance(items, list):
        items = []
    items.append(note)
    conn.execute("UPDATE entity_matches SET conflicts=? WHERE id=?",
                 (json.dumps(items, ensure_ascii=False), match_id))


def _mark(conn: sqlite3.Connection, match_id: int, decision: str, note: dict | None = None) -> None:
    conn.execute(
        "UPDATE entity_matches SET decision=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
        (decision, match_id))
    if note is not None:
        _append_note(conn, match_id, note)


def _chunks(seq, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _load_idents(conn: sqlite3.Connection, record_ids) -> dict[int, tuple]:
    """record_id -> (patient_id, national_id, dob, mother_name)."""
    out: dict[int, tuple] = {}
    for part in _chunks(record_ids, 500):
        ph = ",".join("?" * len(part))
        for r in conn.execute(
                f"SELECT record_id, patient_id, national_id, dob, mother_name FROM er_records WHERE record_id IN ({ph})",
                part):
            out[r["record_id"]] = (r["patient_id"], r["national_id"], r["dob"], r["mother_name"])
    return out


def _load_membership(conn: sqlite3.Connection, record_ids) -> dict[int, int]:
    """record_id -> entity_id for active memberships only."""
    out: dict[int, int] = {}
    for part in _chunks(record_ids, 500):
        ph = ",".join("?" * len(part))
        for r in conn.execute(
                f"SELECT record_id, entity_id FROM entity_records WHERE active=1 AND record_id IN ({ph})",
                part):
            out[r["record_id"]] = r["entity_id"]
    return out


def _load_never(conn: sqlite3.Connection, record_ids) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for part in _chunks(record_ids, 500):
        ph = ",".join("?" * len(part))
        for r in conn.execute(
                f"SELECT record_a, record_b FROM match_decisions WHERE decision='never_match' AND (record_a IN ({ph}) OR record_b IN ({ph}))",
                part * 2):
            a, b = r["record_a"], r["record_b"]
            out.add((a, b) if a < b else (b, a))
    return out


def _entity_strong_ids(conn: sqlite3.Connection, entity_id: int) -> tuple[set, set]:
    """Distinct non-empty strong IDs among an entity's active members."""
    pids, nids = set(), set()
    for r in conn.execute(
            "SELECT DISTINCT er.patient_id, er.national_id FROM er_records er "
            "JOIN entity_records en ON en.record_id=er.record_id "
            "WHERE en.entity_id=? AND en.active=1", (entity_id,)):
        if r["patient_id"]:
            pids.add(r["patient_id"])
        if r["national_id"]:
            nids.add(r["national_id"])
    return pids, nids


class _UF:
    """Union-find over chunk records + their current entities.

    A component key is an existing entity id (int) or ('new', record_id).
    Only strong identifiers (patient_id, national_id) are hard: merging two
    components with different strong IDs is refused by the caller, which
    marks the edge skipped instead. dob/mother conflicts are tolerated
    (auto-accept policy: typos, not identity).
    """

    def __init__(self) -> None:
        self.parent: dict = {}
        self.members: dict = {}
        self.pids: dict = {}
        self.nids: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        self.members.setdefault(x, [x])
        self.pids.setdefault(x, set())
        self.nids.setdefault(x, set())
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def strong_sets(self, x) -> tuple[set, set]:
        r = self.find(x)
        return self.pids[r], self.nids[r]

    def seed_entity(self, entity_id: int, pids: set, nids: set) -> None:
        self.find(entity_id)
        self.pids[entity_id] |= set(pids)
        self.nids[entity_id] |= set(nids)

    def seed_record(self, record_id: int, ident: tuple) -> None:
        key = ("new", record_id)
        self.find(key)
        if ident[0]:
            self.pids[key].add(ident[0])
        if ident[1]:
            self.nids[key].add(ident[1])

    def union(self, a, b) -> bool:
        """Merge components; returns False if strong IDs would conflict."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        pa, na = self.pids[ra], self.nids[ra]
        pb, nb = self.pids[rb], self.nids[rb]
        if len(pa | pb) > 1 or len(na | nb) > 1:
            return False
        small, big = (ra, rb) if len(self.members[ra]) < len(self.members[rb]) else (rb, ra)
        self.parent[small] = big
        self.members[big].extend(self.members.pop(small))
        self.pids[big] |= self.pids.pop(small)
        self.nids[big] |= self.nids.pop(small)
        return True


def apply_backfill_chunk(conn: sqlite3.Connection, chunk_size: int, last_id: int,
                         stopping: Callable[[], bool] = lambda: False) -> dict:
    """Disposition one chunk of held review matches. Atomic per chunk.

    Fetches held rows (decision NULL, REVIEW states, id > last_id) in id
    order. Refined policy: PROBABLE/POSSIBLE rows are processed
    best-first (confidence DESC, id ASC) so an ambiguous record claims
    its best partner, entity membership changes are applied for the
    resulting components, and merged rows are marked accepted (blocked
    ones skipped). NEEDS_REVIEW rows are NOT merged and never enter the
    union-find at all: each is marked separated terminally (see
    separate_needs_review). Every fetched row ends terminally (accepted,
    skipped, or separated). Returns stats including the new ``last_id``
    checkpoint; ``done`` is True when the fetch returned fewer rows than
    chunk_size (no held rows remained above last_id at fetch time). A
    full final chunk reports done False; the caller re-checks the
    remaining count before chaining, so an exact-multiple total costs no
    extra no-op job.
    Raises JobInterrupted (via stopping) without committing a partial chunk.
    """
    from ..errors import JobInterrupted
    chunk_size = max(1, min(int(chunk_size or DEFAULT_CHUNK), MAX_CHUNK))
    rows = conn.execute(
        f"SELECT id, record_a, record_b, state, confidence FROM entity_matches "
        f"WHERE decision IS NULL AND state IN {REVIEW_IN} AND id > ? ORDER BY id LIMIT ?",
        (last_id, chunk_size)).fetchall()
    if not rows:
        return {"processed": 0, "merged": 0, "skipped": 0, "separated": 0, "last_id": last_id, "done": True}
    new_last = max(r["id"] for r in rows)
    if stopping():
        raise JobInterrupted("stop")
    # Refined split: NEEDS_REVIEW rows never touch the merge path (no
    # idents/membership/never lookups, no ambiguity claiming, no union).
    # They are dispositioned as separated in the same chunk transaction.
    separate_rows = [r for r in rows if r["state"] == "NEEDS_REVIEW"]
    merge_rows = [r for r in rows if r["state"] != "NEEDS_REVIEW"]
    endpoints: set[int] = set()
    for r in merge_rows:
        endpoints.add(r["record_a"])
        endpoints.add(r["record_b"])
    idents = _load_idents(conn, endpoints)
    membership = _load_membership(conn, endpoints)
    never = _load_never(conn, endpoints)
    uf = _UF()
    # Seed: every endpoint maps to its current entity (or a fresh component).
    key_of: dict[int, object] = {}
    touched_entities: set[int] = set()
    for rid in endpoints:
        ent = membership.get(rid)
        if ent is not None:
            key_of[rid] = ent
            touched_entities.add(ent)
        else:
            key_of[rid] = ("new", rid)
    for ent in touched_entities:
        pids, nids = _entity_strong_ids(conn, ent)
        uf.seed_entity(ent, pids, nids)
    for rid in endpoints:
        if rid not in membership:
            uf.seed_record(rid, idents.get(rid, (None, None, None, None)))
    # Ambiguity is per record: a record without any strong ID whose chunk
    # partners carry >1 distinct strong ID. Best-first processing + a
    # "claimed" set implements best-partner-wins.
    partner_strong: dict[int, tuple[set, set]] = {}
    for r in merge_rows:
        for rid, other in ((r["record_a"], r["record_b"]), (r["record_b"], r["record_a"])):
            ident = idents.get(rid, (None, None, None, None))
            if ident[0] or ident[1]:
                continue
            oident = idents.get(other, (None, None, None, None))
            p, n = partner_strong.setdefault(rid, (set(), set()))
            if oident[0]:
                p.add(oident[0])
            if oident[1]:
                n.add(oident[1])
    ambiguous = {rid for rid, (p, n) in partner_strong.items() if len(p) > 1 or len(n) > 1}
    claimed: set[int] = set()
    accepted: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for r in sorted(merge_rows, key=lambda x: (-x["confidence"], x["id"])):
        a, b = r["record_a"], r["record_b"]
        if a == b:
            skipped.append((r, "self_pair"))
            continue
        pair = (a, b) if a < b else (b, a)
        if pair in never:
            skipped.append((r, "never_match veto"))
            continue
        if a not in idents or b not in idents:
            skipped.append((r, "endpoint missing from er_records"))
            continue
        if (a in ambiguous and a in claimed) or (b in ambiguous and b in claimed):
            skipped.append((r, "ambiguity: record already merged with its best partner"))
            continue
        if not uf.union(key_of[a], key_of[b]):
            skipped.append((r, "transitive strong-ID conflict"))
            continue
        # Only ambiguous records are "claimed": claiming both endpoints
        # would wrongly block legitimate chaining through a non-ambiguous
        # partner (e.g. B merging onward to C with the same strong ID).
        if a in ambiguous:
            claimed.add(a)
        if b in ambiguous:
            claimed.add(b)
        accepted.append(r)
    # Group accepted edges by FINAL component root. Roots shift as later
    # unions merge components, so grouping must happen after all unions
    # (grouping by the root observed at union time would go stale).
    comp_edges: dict[object, list[dict]] = {}
    for r in accepted:
        comp_edges.setdefault(uf.find(key_of[r["record_a"]]), []).append(r)
    # Apply component membership changes (scoped _reconcile, no singletons).
    conn.execute("BEGIN IMMEDIATE")
    try:
        for root, edges in comp_edges.items():
            # Component records = edge endpoints PLUS the pre-existing
            # active members of every touched entity in the component.
            # (UF member lists track keys, not an entity's stored members,
            # so deriving records from UF members alone would strand new
            # records in a fresh entity instead of joining the existing one.)
            member_keys = uf.members[root]
            touched = {m for m in member_keys if isinstance(m, int)}
            recs_set: set[int] = set()
            for e in edges:
                recs_set.add(e["record_a"])
                recs_set.add(e["record_b"])
            for ent in touched:
                for row in conn.execute(
                        "SELECT record_id FROM entity_records WHERE entity_id=? AND active=1", (ent,)):
                    recs_set.add(row["record_id"])
            recs = sorted(recs_set)
            if len(recs) < 2:
                continue
            current = _load_membership(conn, recs)
            ents = sorted({current[rid] for rid in recs if rid in current})
            if len(ents) == 1:
                keep = ents[0]
                alive = conn.execute(
                    "SELECT COUNT(*) FROM entity_records WHERE entity_id=? AND active=1", (keep,)).fetchone()[0]
                if alive == len(recs) and set(current.keys()) == set(recs):
                    continue  # already merged exactly as computed: no-op
            else:
                keep = None
            if keep is None:
                keep = ents[0] if ents else None
            min_conf = min(e["confidence"] for e in edges)
            if keep is None:
                keep = conn.execute(
                    "INSERT INTO entities(matcher_version, state, confidence) VALUES (?, 'AUTO_ACCEPTED', ?)",
                    (MATCHER_VERSION, min_conf)).lastrowid
                action = "create"
            else:
                conn.execute(
                    "UPDATE entities SET state='AUTO_ACCEPTED', confidence=?, matcher_version=?, status='active', "
                    "merged_into=NULL, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (min_conf, MATCHER_VERSION, keep))
                action = "merge"
            moved = [rid for rid in recs if current.get(rid) != keep]
            for part in _chunks(moved, 500):
                ph = ",".join("?" * len(part))
                conn.execute(f"UPDATE entity_records SET active=0 WHERE active=1 AND record_id IN ({ph})", part)
            conn.executemany(
                "INSERT INTO entity_records(entity_id, record_id, active) VALUES (?,?,1) "
                "ON CONFLICT(entity_id, record_id) DO UPDATE SET active=1",
                [(keep, rid) for rid in moved])
            conn.execute(
                "INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (?,?,?,?,?)",
                (keep, "auto_accept", json.dumps(
                    {"records": sorted(recs)[:200], "from_entities": ents,
                     "match_ids": [e["id"] for e in edges], "min_confidence": min_conf},
                    ensure_ascii=False), MATCHER_VERSION, "auto_accept"))
            for e in ents:
                if e == keep:
                    continue
                alive = conn.execute(
                    "SELECT COUNT(*) FROM entity_records WHERE entity_id=? AND active=1", (e,)).fetchone()[0]
                if alive == 0:
                    tgt = {keep}
                    conn.execute(
                        "UPDATE entities SET status='merged_into', merged_into=? WHERE id=?", (keep, e))
                    conn.execute(
                        "INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (?,?,?,?,?)",
                        (e, "retire", json.dumps({"into": [keep], "auto_accept": True}, ensure_ascii=False),
                         MATCHER_VERSION, "auto_accept"))
        for r in accepted:
            _mark(conn, r["id"], DECISION_ACCEPTED)
        for r, reason in skipped:
            _mark(conn, r["id"], DECISION_SKIPPED,
                  {"field": "auto_accept", "kind": "skipped", "detail": reason})
        for r in separate_rows:
            separate_needs_review(conn, r["id"])
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"processed": len(rows), "merged": len(accepted), "skipped": len(skipped),
            "separated": len(separate_rows),
            "last_id": new_last, "done": len(rows) < chunk_size}
