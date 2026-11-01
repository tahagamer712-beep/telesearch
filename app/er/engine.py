"""Entity Resolution engine: map -> build identity layer -> blocking -> compare -> cluster (guarded) -> entities.
Resumable (checkpoint per block), pausable (stopping()), versioned (matcher_version)."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from typing import Callable

from .. import audit
from . import schema_map
from .build import build_records
from .matcher import AUTO_STATES, MATCHER_VERSION, REVIEW_STATES, compare, names_maybe_equal

log = logging.getLogger("pdb.er")
MAX_BLOCK = 300


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def prepare(conn: sqlite3.Connection, stopping=lambda: False) -> dict:
    schema_map.seed_synonyms(conn)
    mapped = 0
    for (fid,) in conn.execute("SELECT id FROM files WHERE status IN ('indexed','partial') AND record_count>0 AND id IN (SELECT file_id FROM fields)").fetchall():
        mapped += schema_map.map_file(conn, fid)
    built, capped = build_records(conn, stopping=stopping)
    return {"fields_mapped": mapped, "er_records_added": built, "capped": capped}


def run_matching(conn: sqlite3.Connection, checkpoint: dict | None = None, rebuild: bool = False, stopping: Callable[[], bool] = lambda: False,
                 on_checkpoint: Callable[[dict], None] | None = None) -> dict:
    """Compare candidate pairs and cluster. Safe to call repeatedly (incremental)."""
    t0 = time.time()
    cp = checkpoint or {}
    stats = {"prepared": prepare(conn, stopping), "compared": cp.get("compared", 0), "stored": cp.get("stored", 0), "skipped_blocks": cp.get("skipped_blocks", 0), "blocks": cp.get("blocks", 0)}
    stats["build_capped"] = bool(stats["prepared"].get("capped"))
    if rebuild and not cp:
        conn.execute("UPDATE er_records SET matched_ver=0")
        conn.execute("DELETE FROM entity_matches WHERE decision IS NULL")
    never = {(r[0], r[1]) for r in conn.execute("SELECT record_a, record_b FROM match_decisions WHERE decision='never_match'")}
    last_key = cp.get("last_key", "")
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT b.block_key FROM er_blocks b JOIN er_records r ON r.record_id=b.record_id WHERE r.matched_ver < ? AND b.block_key > ? ORDER BY b.block_key", (MATCHER_VERSION, last_key))]
    stats["total_blocks"] = len(keys)
    seen: set[tuple[int, int]] = set()
    # lockfix-v3 (2026-10-30): القراءة + حلقة compare الثقيلة صارت خارج المعاملة؛
    # BEGIN IMMEDIATE يُفتح فقط لحظة كتابة نتائج الكتلة (ملّي-ثواني بدل دقائق).
    try:
        for key in keys:
            if stopping():
                if on_checkpoint:
                    on_checkpoint({**stats, "last_key": last_key})
                from ..errors import JobInterrupted
                raise JobInterrupted("stop")
            members = [dict(r) for r in conn.execute("SELECT r.* FROM er_blocks b JOIN er_records r ON r.record_id=b.record_id WHERE b.block_key=?", (key,))]
            stats["blocks"] += 1
            if len(members) > MAX_BLOCK:
                # v2: تقسيم فرعي بالاسم الكامل بدل تخطي الكتلة كلها (التخطي كان يخفي تطابقات).
                # الازواج عبر المجموعات لا تضيع: اسماء مختلفة بلا معرّف مشترك لا تطابق اي قاعدة،
                # وازواج المعرّفات/الهواتف/الميلاد المشتركة تلتقي في كتلها الخاصة.
                subs: dict[str, list] = defaultdict(list)
                for m in members:
                    if m.get("name_core"):
                        subs[m["name_core"]].append(m)
                groups = [g for g in subs.values() if 1 < len(g) <= MAX_BLOCK]
                stats["skipped_blocks"] += sum(1 for g in subs.values() if len(g) > MAX_BLOCK)
                stats["skipped_pairs"] = stats.get("skipped_pairs", 0) + sum(len(g) * (len(g) - 1) // 2 for g in subs.values() if len(g) > MAX_BLOCK)
                stats["skipped_nameless"] = stats.get("skipped_nameless", 0) + sum(1 for m in members if not m.get("name_core"))
            else:
                groups = [members]
            name_block = key.startswith(("nt:", "n1:", "mn:"))
            out_rows: list[tuple] = []
            for members in groups:
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        a, b = members[i], members[j]
                        if name_block and not names_maybe_equal(a.get("name_norm") or a.get("name_core"), b.get("name_norm") or b.get("name_core")):
                            # v2 بوابة: بلا اسم مطابق محتمل وبلا معرّف/هاتف/ميلاد مشترك لا توجد قاعدة
                            # يمكن ان تطلق — فيُتخطى قبل المقارنة، وقبل seen حتى تلتقيه الكتل الاخرى
                            if not any(a.get(f_) and a.get(f_) == b.get(f_) for f_ in ("patient_id", "national_id", "phone", "dob")):
                                continue
                        if a["matched_ver"] >= MATCHER_VERSION and b["matched_ver"] >= MATCHER_VERSION:
                            continue
                        pr = _pair(a["record_id"], b["record_id"])
                        if pr in seen or pr in never:
                            continue
                        seen.add(pr)
                        if len(seen) > 200_000:  # حد الذاكرة (Nokia)
                            seen.clear()
                        stats["compared"] += 1
                        r = compare(a, b) if a["record_id"] < b["record_id"] else compare(b, a)
                        if r.state == "NO_MATCH":
                            continue
                        out_rows.append((pr[0], pr[1], r.state, r.confidence, MATCHER_VERSION, json.dumps({"rule": r.rule, "items": r.evidence}, ensure_ascii=False), json.dumps(r.conflicts, ensure_ascii=False)))
                        stats["stored"] += 1
            last_key = key
            # كتابة قصيرة: معاملة واحدة لكل كتلة (الكتل مستقلة، وإعادة الإدخال آمنة بـON CONFLICT)
            conn.execute("BEGIN IMMEDIATE")
            if out_rows:
                conn.executemany(
                    "INSERT INTO entity_matches(record_a, record_b, state, confidence, matcher_version, evidence, conflicts) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(record_a, record_b) DO UPDATE SET state=excluded.state, confidence=excluded.confidence, matcher_version=excluded.matcher_version, "
                    "evidence=excluded.evidence, conflicts=excluded.conflicts WHERE decision IS NULL", out_rows)
            conn.execute("COMMIT")
            if on_checkpoint:
                on_checkpoint({**stats, "last_key": last_key})
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE er_records SET matched_ver=?", (MATCHER_VERSION,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    stats["cluster"] = recluster(conn, actor="auto")
    stats["seconds"] = round(time.time() - t0, 2)
    conn.execute("INSERT INTO settings(key,value) VALUES('er_last_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(stats),))
    return stats


# ---------------------------------------------------------------- clustering
class _UF:
    def __init__(self, ident: dict[int, tuple], never_partners: dict[int, set[int]]) -> None:
        self.p: dict[int, int] = {}
        self.members: dict[int, list[int]] = {}
        self.ids: dict[int, tuple[set, set, set]] = {}
        self.ident, self.never = ident, never_partners
        self.edges: dict[int, list[tuple[float, str]]] = defaultdict(list)

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        self.members.setdefault(x, [x])
        if x not in self.ids:
            pid, nid, dob, mo = self.ident.get(x, (None, None, None, None))
            self.ids[x] = ({pid} if pid else set(), {nid} if nid else set(), {dob} if dob else set(), {mo} if mo else set())
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int, forced: bool, conf: float, state: str, hard_indexes: tuple[int, ...] = (0, 1, 2, 3)) -> str | None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        if not forced:
            for k in hard_indexes:
                if len(self.ids[ra][k] | self.ids[rb][k]) > 1:
                    return "الدمج سيجمع معرّفات/تواريخ ميلاد/اسماء ام متعارضة (تعارض متعدٍّ)"
        small, big = (ra, rb) if len(self.members[ra]) < len(self.members[rb]) else (rb, ra)
        bigset = set(self.members[big])
        for m in self.members[small]:
            if self.never.get(m, set()) & bigset:
                return "قرار سابق: NEVER MATCH بين سجلين في المجموعتين"
        self.p[small] = big
        self.members[big].extend(self.members.pop(small))
        s = self.ids.pop(small)
        for k in range(4):
            self.ids[big][k].update(s[k])
        self.edges[big].extend(self.edges.pop(small, []))
        self.edges[big].append((conf, state))
        return None


def recluster(conn: sqlite3.Connection, actor: str = "auto", auto_accept: bool | None = None) -> dict:
    """Rebuild entity membership from stored matches + admin decisions. Raw records are never touched.
    Deterministic: edges processed by (confidence desc, id).
    auto_accept None reads the reversible er_auto_accept setting. When ON,
    decision-NULL PROBABLE/POSSIBLE rows are edges too (live auto-merge);
    decision-NULL NEEDS_REVIEW rows are separated terminally before edge
    selection (refined owner policy 2026-10-29: no merge, no manual
    approval, review screen stays for manual correction/override only).
    Terminal dispositions (confirmed, auto_accepted) always stand and are
    never ambiguity candidates or transitive-blocked. When ON, merged NULL
    PROBABLE/POSSIBLE edges are marked auto_accepted (blocked ones
    auto_accepted_skipped), so the review queue empties; AUTO-state NULL
    edges stay NULL as today."""
    from . import autoaccept
    if auto_accept is None:
        auto_accept = autoaccept.is_enabled(conn)
    states = autoaccept.mergable_states(auto_accept)
    if auto_accept:
        # Refined owner policy: NEEDS_REVIEW never merges and is never
        # held for manual approval. Separate every held NEEDS_REVIEW row
        # FIRST, in its own atomic transaction, so none of them can become
        # an edge below (directly or transitively). Per-row helper keeps
        # one definition of the marking; at bulk scale the operator runs
        # the chunked backfill first (see the >50k warning below), so a
        # routine recluster finds zero such rows here.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for (mid,) in conn.execute(
                    "SELECT id FROM entity_matches WHERE decision IS NULL AND state='NEEDS_REVIEW'").fetchall():
                autoaccept.separate_needs_review(conn, mid)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    never: dict[int, set[int]] = defaultdict(set)
    for a, b in conn.execute("SELECT record_a, record_b FROM match_decisions WHERE decision='never_match'"):
        never[a].add(b); never[b].add(a)
    _ph = ",".join("?" * len(states))
    edges = conn.execute(
        f"SELECT id, record_a, record_b, state, confidence, decision FROM entity_matches WHERE decision IN ('confirmed','auto_accepted') OR (decision IS NULL AND state IN ({_ph})) "
        f"ORDER BY (decision IN ('confirmed','auto_accepted')) DESC, confidence DESC, id", states).fetchall()
    if auto_accept:
        _n_review = sum(1 for e in edges if e["decision"] is None and e["state"] in autoaccept.MERGE_REVIEW_STATES)
        if _n_review > 50000:
            log.warning("recluster auto-accept: %d review edges in one run (run the chunked backfill first)", _n_review)
    # Audit fix 2026-10-27 (F1 defense in depth): skip edges whose endpoints no
    # longer exist in records (DBs wedged before the reindex purge existed).
    # Without this, _reconcile's INSERT INTO entity_records fails FK and the
    # whole recluster rolls back, while autocommit decision UPDATEs persist.
    if edges:
        _all = sorted({r["record_a"] for r in edges} | {r["record_b"] for r in edges})
        _live = set()
        for _i in range(0, len(_all), 500):
            _part = _all[_i:_i + 500]
            _live.update(r[0] for r in conn.execute(f"SELECT id FROM records WHERE id IN ({','.join('?' * len(_part))})", _part))
        _dead = [e for e in edges if e["record_a"] not in _live or e["record_b"] not in _live]
        if _dead:
            log.warning("recluster: skipping %d edges referencing deleted records", len(_dead))
            edges = [e for e in edges if e["record_a"] in _live and e["record_b"] in _live]
    rids = {r["record_a"] for r in edges} | {r["record_b"] for r in edges}
    ident = {}
    ids = list(rids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(f"SELECT record_id, patient_id, national_id, dob, mother_name FROM er_records WHERE record_id IN ({','.join('?' * len(part))})", part):
            ident[r["record_id"]] = (r["patient_id"], r["national_id"], r["dob"], r["mother_name"])
    uf = _UF(ident, never)
    blocked = []
    # حارس الغموض: سجل بلا معرّف يطابق شخصين بمعرّفين مختلفين.
    # Terminal dispositions (confirmed, auto_accepted) stand and are never
    # ambiguity candidates: only decision-NULL edges participate.
    partners: dict[int, list] = defaultdict(list)
    for e in edges:
        if e["decision"] is None:
            partners[e["record_a"]].append((e["id"], e["record_b"], e["confidence"]))
            partners[e["record_b"]].append((e["id"], e["record_a"], e["confidence"]))
    ambiguous_edges: set[int] = set()
    ambiguous_skipped: set[int] = set()
    for r, plist in partners.items():
        if len(plist) < 2:
            continue
        mine = ident.get(r, (None, None, None))
        if mine[0] or mine[1]:
            continue
        pids = {ident.get(o, (None,))[0] for _, o, _ in plist} - {None}
        nids = {ident.get(o, (None, None))[1] for _, o, _ in plist} - {None}
        if len(pids) > 1 or len(nids) > 1:
            if auto_accept:
                # Best partner only (confidence DESC, id ASC): the record
                # merges once; the losing edges are terminally skipped,
                # never fused into one entity with two different IDs.
                best = sorted(plist, key=lambda x: (-x[2], x[0]))[0][0]
                ambiguous_skipped.update(i for i, _, _ in plist if i != best)
                ambiguous_edges.update(i for i, _, _ in plist if i != best)
            else:
                ambiguous_edges.update(i for i, _, _ in plist)
    edges = [e for e in edges if e["id"] not in ambiguous_edges]
    if ambiguous_edges:
        conn.execute("BEGIN IMMEDIATE")
        if auto_accept:
            for i in ambiguous_skipped:
                conn.execute("UPDATE entity_matches SET decision='auto_accepted_skipped', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), conflicts=json_insert(COALESCE(conflicts,'[]'),'$[#]',json(?)) WHERE id=? AND decision IS NULL",
                             (json.dumps({"field": "auto_accept", "kind": "skipped", "detail": "ambiguity: merged with best partner only"}, ensure_ascii=False), i))
        else:
            for i in ambiguous_edges:
                conn.execute("UPDATE entity_matches SET state='NEEDS_REVIEW', conflicts=json_insert(COALESCE(conflicts,'[]'),'$[#]',json(?)) WHERE id=? AND decision IS NULL",
                             (json.dumps({"field": "ambiguity", "kind": "conflict", "detail": "السجل بلا معرّف ويطابق اكثر من شخص بمعرّفات مختلفة: لا دمج تلقائي"}, ensure_ascii=False), i))
        conn.execute("COMMIT")
    for e in edges:
        why = uf.union(e["record_a"], e["record_b"], e["decision"] in ("confirmed", "auto_accepted"), e["confidence"], e["state"],
                       hard_indexes=(0, 1) if auto_accept else (0, 1, 2, 3))
        if why:
            blocked.append((e["id"], why))
    comps = [sorted(m) for r, m in uf.members.items() if len(m) > 1]
    comp_edges = {min(m): uf.edges.get(r, []) for r, m in uf.members.items() if len(m) > 1}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for mid, why in blocked:
            if auto_accept:
                conn.execute("UPDATE entity_matches SET decision='auto_accepted_skipped', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), conflicts=json_insert(COALESCE(conflicts,'[]'),'$[#]',json(?)) WHERE id=? AND decision IS NULL",
                             (json.dumps({"field": "auto_accept", "kind": "skipped", "detail": why}, ensure_ascii=False), mid))
            else:
                conn.execute("UPDATE entity_matches SET state='NEEDS_REVIEW', conflicts=json_insert(COALESCE(conflicts,'[]'),'$[#]',json(?)) WHERE id=? AND decision IS NULL",
                             (json.dumps({"field": "cluster", "kind": "conflict", "detail": why}, ensure_ascii=False), mid))
        if auto_accept:
            # Every NULL PROBABLE/POSSIBLE edge ends terminally in an ON
            # recluster: merged (same final component) -> auto_accepted,
            # so the review queue actually empties; blocked/skipped rows
            # above are already dispositioned and the WHERE clause leaves
            # them alone. NEEDS_REVIEW rows were separated before edge
            # selection and never reach this block. AUTO-state NULL edges
            # keep today's behaviour (still counted as exact/high).
            for e in edges:
                if e["decision"] is None and e["state"] in autoaccept.MERGE_REVIEW_STATES and uf.find(e["record_a"]) == uf.find(e["record_b"]):
                    conn.execute("UPDATE entity_matches SET decision='auto_accepted', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND decision IS NULL", (e["id"],))
        out = _reconcile(conn, comps, comp_edges, actor)
        out["blocked_edges"] = len(blocked)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    out["singletons_created"] = _singletons(conn)
    return out


def _reconcile(conn, comps: list[list[int]], comp_edges: dict, actor: str) -> dict:
    cur_of: dict[int, int] = {}
    cur_members: dict[int, set[int]] = defaultdict(set)
    touched: set[int] = set()
    comp_recs = {r for c in comps for r in c}
    for rows in _chunks(sorted(comp_recs), 500):
        for r in conn.execute(f"SELECT entity_id, record_id FROM entity_records WHERE active=1 AND record_id IN ({','.join('?' * len(rows))})", rows):
            touched.add(r["entity_id"])
    for eid in list(touched):
        for r in conn.execute("SELECT record_id FROM entity_records WHERE entity_id=? AND active=1", (eid,)):
            cur_of[r[0]] = eid
            cur_members[eid].add(r[0])
    groups = [(set(c), comp_edges.get(min(c), [])) for c in comps]
    for eid in touched:  # بقية اعضاء كيان تغيّر تصبح مجموعات مستقلة (او تنتمي لمكوّنات اخرى)
        for r in cur_members[eid]:
            if r not in comp_recs:
                groups.append(({r}, []))
    # ايضاً: سجلات مفردة كانت مدمجة سابقاً ولم تعد في اي مكوّن => تُعالج اعلاه (لانها ضمن entity touched)
    # كيانات لم تُلمس لكن اعضاؤها لم يعودوا مترابطين تُكتشف هنا:
    for eid in _entities_losing_edges(conn, comp_recs):
        if eid in touched:
            continue
        touched.add(eid)
        for r in conn.execute("SELECT record_id FROM entity_records WHERE entity_id=? AND active=1", (eid,)):
            cur_of[r[0]] = eid
            cur_members[eid].add(r[0])
            if r[0] not in comp_recs:
                groups.append(({r[0]}, []))
    groups.sort(key=lambda g: min(g[0]))
    claimed: set[int] = set()
    made = merged = split = 0
    now_empty_candidates: set[int] = set()
    dest: dict[int, int] = {}
    for G, es in groups:
        ents = {cur_of[r] for r in G if r in cur_of}
        if len(ents) == 1 and cur_members[next(iter(ents))] == G and next(iter(ents)) not in claimed:
            claimed.add(next(iter(ents)))
            continue
        keep = next((e for e in sorted(ents) if e not in claimed), None)
        state = "SINGLE" if not es else ("EXACT_MATCH" if all(s == "EXACT_MATCH" for _, s in es) else "HIGH_CONFIDENCE_MATCH")
        conf = min((c for c, _ in es), default=1.0)
        if keep is None:
            keep = conn.execute("INSERT INTO entities(matcher_version, state, confidence) VALUES (?,?,?)", (MATCHER_VERSION, state, conf)).lastrowid
            made += 1
            action = "create"
        else:
            conn.execute("UPDATE entities SET state=?, confidence=?, matcher_version=?, status='active', merged_into=NULL, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (state, conf, MATCHER_VERSION, keep))
            action = "merge" if len(ents) > 1 or any(r not in cur_of for r in G) else "update"
            merged += action == "merge"
        claimed.add(keep)
        moved = [r for r in G if cur_of.get(r) != keep]
        for chunk in _chunks(moved, 500):
            conn.execute(f"UPDATE entity_records SET active=0 WHERE active=1 AND record_id IN ({','.join('?' * len(chunk))})", chunk)
        conn.executemany("INSERT INTO entity_records(entity_id, record_id, active) VALUES (?,?,1) ON CONFLICT(entity_id, record_id) DO UPDATE SET active=1", [(keep, r) for r in moved])
        for r in G:
            dest[r] = keep
        for e in ents - {keep}:
            now_empty_candidates.add(e)
        detail = {"records": sorted(G)[:200], "from_entities": sorted(ents), "edges": len(es), "min_confidence": conf}
        conn.execute("INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (?,?,?,?,?)", (keep, action, json.dumps(detail), MATCHER_VERSION, actor))
    for e in now_empty_candidates:
        alive = conn.execute("SELECT COUNT(*) FROM entity_records WHERE entity_id=? AND active=1", (e,)).fetchone()[0]
        if alive == 0:
            tgt = {dest.get(r) for r in cur_members[e]} - {None}
            conn.execute("UPDATE entities SET status=?, merged_into=? WHERE id=?", ("merged_into" if len(tgt) == 1 else "retired", next(iter(tgt)) if len(tgt) == 1 else None, e))
            conn.execute("INSERT INTO entity_merge_history(entity_id, action, detail, matcher_version, actor) VALUES (?,?,?,?,?)",
                         (e, "retire", json.dumps({"into": sorted(tgt)}), MATCHER_VERSION, actor))
        else:
            split += 1
    return {"entities_created": made, "merges": merged, "splits": split, "components": len(comps)}


def _entities_losing_edges(conn, comp_recs: set[int]) -> list[int]:
    """Entities with >1 active members that are NOT fully covered by current components (e.g. an edge was rejected)."""
    out = []
    for r in conn.execute("SELECT entity_id FROM entity_records WHERE active=1 GROUP BY entity_id HAVING COUNT(*) > 1"):
        eid = r[0]
        mem = [x[0] for x in conn.execute("SELECT record_id FROM entity_records WHERE entity_id=? AND active=1", (eid,))]
        if not all(m in comp_recs for m in mem):
            out.append(eid)
    return out


def _singletons(conn, batch: int = 5000) -> int:
    total = 0
    while True:
        rows = [r[0] for r in conn.execute("SELECT record_id FROM er_records WHERE record_id NOT IN (SELECT record_id FROM entity_records WHERE active=1) ORDER BY record_id LIMIT ?", (batch,))]
        if not rows:
            return total
        conn.execute("BEGIN IMMEDIATE")
        try:
            base = conn.execute("SELECT COALESCE(MAX(id),0) FROM entities").fetchone()[0]
            conn.executemany("INSERT INTO entities(id, matcher_version, state, confidence) VALUES (?,?, 'SINGLE', 1.0)", [(base + i + 1, MATCHER_VERSION) for i in range(len(rows))])
            conn.executemany("INSERT INTO entity_records(entity_id, record_id, active) VALUES (?,?,1) ON CONFLICT(entity_id, record_id) DO UPDATE SET active=1", [(base + i + 1, r) for i, r in enumerate(rows)])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        total += len(rows)


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
