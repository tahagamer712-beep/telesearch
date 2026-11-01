"""One search engine for Quick / Advanced / AI. All SQL is parameterized; field roles
and match types come from fixed whitelists, never from user text."""
from __future__ import annotations

import datetime
import difflib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

from ..config import Config
from ..db.connection import has_fts5
from ..errors import SearchTimeout
from ..norm.arabic import normalize_text, to_ascii_digits
from ..norm.dates import parse_date
from ..norm.phone import canonical_phone, digits_only, looks_like_phone
from ..norm.values import normalize_value
from .dsl import Filter, SearchQuery, Term

log = logging.getLogger("pdb.search")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MATCH_TYPES = ("exact_field", "exact_token", "normalized_exact", "prefix", "fulltext", "contains", "fuzzy")
_TYPE_CASE = "CASE worst " + " ".join(f"WHEN {i} THEN '{t}'" for i, t in enumerate(MATCH_TYPES)) + " END"
_OK_STATUS = "('indexed','partial','processing')"


@dataclass
class SearchSummary:
    search_id: int
    total: int
    files: int
    seconds: float
    groups: int = 0


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def term_norms(term: Term) -> list[str]:
    """Normalized candidates for a term (a bare number can be a phone OR an id)."""
    raw = term.text.strip()
    if term.role:
        n = normalize_value(term.role, raw)
        return [n] if n else []
    out: list[str] = []
    if _EMAIL.match(raw):
        out.append(raw.lower())
    if looks_like_phone(raw):
        for c in (canonical_phone(raw), digits_only(raw)):
            if c and c not in out:
                out.append(c)
    d = parse_date(raw)
    if d:
        out.append(d)
    else:  # تاريخ ملتبس (12/05/1998): نبحث بالتفسيرين + النص المطبّع، ولا نخمّن واحداً
        m = _DMY.match(to_ascii_digits(raw))
        if m:
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            for dd, mm in ((a, b), (b, a)):
                try:
                    out.append(datetime.date(y, mm, dd).isoformat())
                except ValueError:
                    pass
    t = normalize_text(raw)
    if t and t not in out:
        out.append(t)
    return list(dict.fromkeys(out))


_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")
_DATE_LIKE = re.compile(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}")
# Normalized tokens that mark a name column as a MOTHER column (not the
# person's own name). Mother columns also carry detected_type='name', so
# without this split a mother's name would outrank the person's own name.
_MOTHER_TOKENS = {"الام", "ام", "mother", "mom"}

# New match types for name-tiered ranking (stored in search_hits.match_type;
# no CHECK constraint exists on that column, so no migration is needed).
# Legacy types (exact_field/exact_token/...) are kept verbatim whenever
# tiering does not apply (non-name queries, fuzzy queries, field-specific
# terms), so old displays/exports keep working.
NAME_MATCH_TYPES = ("exact_phrase", "name_ordered", "name_position", "partial")


def _is_name_term(term: Term) -> bool:
    """True for terms that participate in person-name ranking: unroled or
    name-role terms, excluding phones, emails and date-like tokens (those
    are separate detected fields, not name tokens)."""
    if term.role not in (None, "name") or term.field is not None:
        return False
    text = term.text.strip()
    if looks_like_phone(text) or _EMAIL.match(text) or _DATE_LIKE.search(to_ascii_digits(text)):
        return False
    return True


def _name_tokens(q: SearchQuery) -> list[str]:
    """Flattened normalized name tokens, in typed order (position-aware
    ranking compares these positions against record name tokens)."""
    out: list[str] = []
    for t in q.terms:
        if _is_name_term(t):
            out.extend(normalize_text(t.text).split())
    return out


def _is_primary_name_field(field_name: str, standard_field: str | None) -> bool:
    """True for the person's own name column. A mother column is never
    primary: explicit mapping wins, otherwise the column-name heuristic
    (a token meaning 'mother') decides."""
    if standard_field == "mother_name":
        return False
    if standard_field == "full_name":
        return True
    return not (set(normalize_text(field_name).split()) & _MOTHER_TOKENS)


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(tok == n for tok in it) for n in needle)


def _name_tier_for_values(q_tokens: list[str], values: list[str]) -> tuple[int, int, int]:
    """Best (lowest) tier for one record across its primary name values.
    Tier 0 = full query as a contiguous phrase in one name value (this
    already covers the value starting with the query tokens, so there is
    no separate prefix tier).
    Tier 1 = all tokens in one name value, in order, non-contiguous.
    Tier 2 = all tokens in one name value, out of order (position-mismatched).
    Tier 3 = only some tokens in any single name value (partial name match).
    Tier 4 = no name token in any primary name value (tokens matched only
    in non-name fields, e.g. the mother's name or an address).
    Returns (tier, covered_distinct_tokens, total_distinct_tokens)."""
    tot = len(set(q_tokens))
    if tot == 0:
        return (0, 0, 0)
    phrase = " ".join(q_tokens)
    best_tier, best_cov = 4, 0
    for v in values:
        if (" " + phrase + " ") in (" " + v + " "):
            return (0, tot, tot)
        toks = v.split()
        tset = set(toks)
        cov = len(set(q_tokens) & tset)
        best_cov = max(best_cov, cov)
        if _is_subsequence(q_tokens, toks):
            best_tier = min(best_tier, 1)
        elif set(q_tokens) <= tset:
            best_tier = min(best_tier, 2)
    if best_tier <= 2:
        return (best_tier, tot, tot)
    if best_cov > 0:
        return (3, best_cov, tot)
    return (4, 0, tot)


def filter_norm(f: Filter) -> str:
    if f.op == "year":
        return str(f.value)
    return normalize_value(f.role, f.value) or ""


def filter_candidates(f: Filter) -> list[str]:
    """Normalized candidates for a filter (phone: local 07.. and international forms)."""
    if f.op == "year":
        return [str(f.value)]
    if f.role == "phone":
        d = digits_only(f.value)
        out = [d]
        if f.op == "equals":
            c = canonical_phone(f.value)
            return [c] if c else out
        if d.startswith("00964"):
            out.append(d[2:])
        elif d.startswith("0"):
            out.append("964" + d[1:])
        elif d.startswith("7"):
            out.append("964" + d)
        return list(dict.fromkeys(out))
    n = filter_norm(f)
    return [n] if n else []


class SearchEngine:
    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn, self.cfg = conn, config
        self.fts = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='record_fts'").fetchone()) and has_fts5(conn)

    # -------------------------------------------------------------- public
    def run(self, query: SearchQuery, user_id: int, mode: str = "quick") -> SearchSummary:
        query.validate()
        t0 = time.monotonic()
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            sid = c.execute("INSERT INTO search_history(user_id, mode, query_json) VALUES (?,?,?)", (user_id, mode, query.to_json())).lastrowid
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        deadline = t0 + self.cfg.search_timeout_sec
        c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        try:
            c.execute("DROP TABLE IF EXISTS temp.cand")
            c.execute("CREATE TEMP TABLE cand(tidx INTEGER, record_id INTEGER, mrank INTEGER, score REAL, reason TEXT)")
            self._collect(query, deadline)
            c.execute("CREATE INDEX IF NOT EXISTS temp.cand_i ON cand(tidx, record_id)")
            c.execute("BEGIN DEFERRED")
            self._materialize(query, sid)
            self._groups(sid)
            total, files = c.execute(
                "SELECT COUNT(*), COUNT(DISTINCT r.file_id) FROM search_hits h JOIN records r ON r.id=h.record_id WHERE h.search_id=?", (sid,)).fetchone()  # عدد سجلات خام
            c.execute("UPDATE search_history SET result_count=? WHERE id=?", (total, sid))
            c.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if c.in_transaction:
                c.execute("ROLLBACK")
            if "interrupted" in str(exc):
                raise SearchTimeout(f"انتهت مهلة البحث ({self.cfg.search_timeout_sec:.0f}s)") from exc
            raise
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.set_progress_handler(None, 0)
            c.execute("DROP TABLE IF EXISTS temp.cand")
        groups = c.execute("SELECT COUNT(*) FROM search_groups WHERE search_id=?", (sid,)).fetchone()[0]
        return SearchSummary(sid, total, files, time.monotonic() - t0, groups)

    # -------------------------------------------------------------- candidates
    def _collect(self, q: SearchQuery, deadline: float) -> None:
        for idx, term in enumerate(q.terms):
            norms = term_norms(term)
            if not norms:
                continue
            for nv in norms:
                self._term_exact_prefix(idx, term, nv, q.exact or term.mode == "exact")
                if q.exact or term.mode == "exact":
                    continue
                if term.mode == "contains":
                    self._term_contains(idx, term, nv)
                    continue
                if term.role is None and term.field is None and self.fts:
                    self._term_fts(idx, nv)
                else:
                    if len(nv) >= 2:
                        self._term_contains(idx, term, nv)
            if (q.fuzzy or term.mode == "fuzzy") and not (q.exact or term.mode == "exact"):
                self._term_fuzzy(idx, term, norms[-1], deadline)

    def _role_join(self, term: Term) -> tuple[str, list]:
        conds, params = [], []
        if term.role:
            conds.append("f.detected_type = ?")
            params.append(term.role)
        if term.field:
            conds.append("f.name = ?")
            params.append(term.field)
        if conds:
            return " JOIN fields f ON f.id = rv.field_id AND " + " AND ".join(conds), params
        return "", []

    def _term_exact_prefix(self, idx: int, term: Term, nv: str, exact_only: bool) -> None:
        join, jp = self._role_join(term)
        if exact_only:
            where, wp = "rv.normalized_value = ?", [nv]
        else:
            where, wp = "rv.normalized_value >= ? AND rv.normalized_value < ?", [nv, nv + "\U0010ffff"]
        self.conn.execute(
            "INSERT INTO cand SELECT ?, rv.record_id, "
            "CASE WHEN rv.original_value = ? THEN 0 WHEN rv.normalized_value = ? THEN 2 ELSE 3 END, 1.0, 'value' "
            f"FROM record_values rv{join} WHERE {where}",
            [idx, term.text.strip(), nv, *jp, *wp])

    def _term_contains(self, idx: int, term: Term, nv: str) -> None:
        join, jp = self._role_join(term)
        self.conn.execute(
            f"INSERT INTO cand SELECT ?, rv.record_id, 5, 0.6, 'contains' FROM record_values rv{join} "
            "WHERE rv.normalized_value LIKE ? ESCAPE '\\'", [idx, *jp, f"%{_like_escape(nv)}%"])

    @staticmethod
    def _fts_phrase(nv: str, prefix: bool = False) -> str:
        toks = nv.split()
        if not toks:
            return ""
        q = " ".join('"' + t.replace('"', '""') + '"' for t in toks)
        return q + "*" if prefix else q

    def _term_fts(self, idx: int, nv: str) -> None:
        phrase = self._fts_phrase(nv)
        if not phrase:
            return
        self.conn.execute("INSERT INTO cand SELECT ?, rowid, 1, 0.95, 'token' FROM record_fts WHERE record_fts MATCH ?", [idx, phrase])
        if len(nv) >= 2:
            self.conn.execute("INSERT INTO cand SELECT ?, rowid, 4, 0.8, 'fulltext' FROM record_fts WHERE record_fts MATCH ?", [idx, self._fts_phrase(nv, True)])

    def _term_fuzzy(self, idx: int, term: Term, nv: str, deadline: float) -> None:
        if " " in nv or len(nv) < 3:
            return
        lo, hi = len(nv) - 2, len(nv) + 2
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(nv)
        found: list[tuple[float, str]] = []
        for (tok,) in self.conn.execute("SELECT token FROM vocab WHERE length(token) BETWEEN ? AND ?", (lo, hi)):
            if tok == nv:
                continue
            sm.set_seq1(tok)
            if sm.real_quick_ratio() < 0.72 or sm.quick_ratio() < 0.72:
                continue
            r = sm.ratio()
            if r >= 0.72:
                found.append((r, tok))
            if len(found) % 500 == 0 and time.monotonic() > deadline:
                raise SearchTimeout("انتهت مهلة البحث التقريبي")
        found.sort(reverse=True)
        for r, tok in found[:40]:
            reason = f"fuzzy:{tok}:{r:.2f}"
            if self.fts:
                self.conn.execute("INSERT INTO cand SELECT ?, rowid, 6, ?, ? FROM record_fts WHERE record_fts MATCH ?",
                                  [idx, r, reason, self._fts_phrase(tok)])
            else:
                self.conn.execute("INSERT INTO cand SELECT ?, record_id, 6, ?, ? FROM record_values WHERE normalized_value LIKE ? ESCAPE '\\'",
                                  [idx, r, reason, f"%{_like_escape(tok)}%"])

    # -------------------------------------------------------------- name tiers + filter hits
    def _compute_name_tiers(self, q: SearchQuery) -> None:
        """Populate temp.ntier(record_id, tier, cov, tot, tiered, is_fuzzy).

        Tiering applies only to name queries (at least one name term) in
        non-fuzzy mode; otherwise every row is neutral (tier 0, tiered 0,
        is_fuzzy 0) so ordering falls back to the legacy worst/score order
        exactly. Fuzzy queries are never tiered: fuzzy hits must stay a
        separate mode, not mixed into exact results by tier.
        Primary-name values only (see _is_primary_name_field); if a record
        has no primary name field, all its name values are used as fallback.
        """
        c = self.conn
        c.execute("DROP TABLE IF EXISTS temp.ntier")
        c.execute("CREATE TEMP TABLE ntier(record_id INTEGER PRIMARY KEY, tier INTEGER, cov INTEGER, tot INTEGER, tiered INTEGER, is_fuzzy INTEGER)")
        cand_ids = [r[0] for r in c.execute("SELECT DISTINCT record_id FROM cand")]
        if not cand_ids:
            return
        fuzzy_ids = {r[0] for r in c.execute("SELECT DISTINCT record_id FROM cand WHERE reason LIKE 'fuzzy:%'")}
        tiered = 0
        q_tokens: list[str] = []
        if not (q.fuzzy or any(t.mode == "fuzzy" for t in q.terms)):
            q_tokens = _name_tokens(q)
            tiered = 1 if q_tokens else 0
        if not tiered:
            c.executemany("INSERT INTO ntier VALUES (?,?,0,0,0,0)",
                          [(rid, 0) for rid in cand_ids])
            return
        # Name-field values for candidate records (batched IN lists).
        per_record: dict[int, list[tuple[str, str | None, str]]] = {rid: [] for rid in cand_ids}
        for i in range(0, len(cand_ids), 400):
            chunk = cand_ids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for rid, fname, std, nv in c.execute(
                    f"SELECT rv.record_id, fl.name, fm.standard_field, rv.normalized_value FROM record_values rv "
                    f"JOIN fields fl ON fl.id = rv.field_id LEFT JOIN field_mappings fm ON fm.field_id = fl.id "
                    f"WHERE rv.record_id IN ({ph}) AND fl.detected_type = 'name'", chunk):
                per_record.setdefault(rid, []).append((fname, std, nv))
        tot = len(set(q_tokens))
        rows = []
        for rid in cand_ids:
            vals = per_record.get(rid, [])
            primary = [nv for (fname, std, nv) in vals if _is_primary_name_field(fname, std)]
            use = primary if primary else [nv for (_f, _s, nv) in vals]
            tier, cov, _t = _name_tier_for_values(q_tokens, use) if use else (4, 0, tot)
            rows.append((rid, tier, cov, tot, 1, 1 if rid in fuzzy_ids else 0))
        c.executemany("INSERT INTO ntier VALUES (?,?,?,?,?,?)", rows)

    def _compute_filter_hits(self, q: SearchQuery) -> None:
        """Populate temp.fhit(record_id, fidx) for unified queries: which
        records satisfy each filter component. Used only for unified
        field-coverage ranking (legacy queries keep filters as AND gates)."""
        c = self.conn
        c.execute("DROP TABLE IF EXISTS temp.fhit")
        c.execute("CREATE TEMP TABLE fhit(record_id INTEGER, fidx INTEGER, PRIMARY KEY (record_id, fidx))")
        for idx, f in enumerate(q.filters):
            s, p = self._filter_sql(f)
            c.execute(f"INSERT INTO fhit SELECT record_id, ? FROM ({s}) AS _f", [idx, *p])

    # -------------------------------------------------------------- filters + final
    def _filter_sql(self, f: Filter) -> tuple[str, list]:
        base = "SELECT rv.record_id FROM record_values rv JOIN fields fl ON fl.id = rv.field_id AND fl.detected_type = ? WHERE "
        if f.op == "year":
            return base + "rv.normalized_value >= ? AND rv.normalized_value < ?", [f.role, f"{f.value}-", f"{f.value}."]
        cands = filter_candidates(f) or [""]
        if f.op == "equals":
            return base + "rv.normalized_value IN (%s)" % ",".join("?" * len(cands)), [f.role, *cands]
        if f.op == "prefix":
            return base + "(" + " OR ".join("(rv.normalized_value >= ? AND rv.normalized_value < ?)" for _ in cands) + ")", [f.role, *[x for c in cands for x in (c, c + "\U0010ffff")]]
        return base + "(" + " OR ".join("rv.normalized_value LIKE ? ESCAPE '\\'" for _ in cands) + ")", [f.role, *[f"%{_like_escape(c)}%" for c in cands]]

    def _groups(self, sid: int) -> None:
        """Group record hits by unified entity (records without an entity stay as their own group)."""
        # Idempotency guard: history ids recycle after a wipe (no AUTOINCREMENT),
        # so stale groups for this sid must go before inserting fresh ones.
        self.conn.execute("DELETE FROM search_groups WHERE search_id=?", (sid,))
        self.conn.execute(
            "INSERT INTO search_groups(search_id, rank, entity_id, record_id, match_type, score, hit_count) "
            "SELECT ?, ROW_NUMBER() OVER (ORDER BY best, rec), eid, rec, mt, sc, n FROM ("
            " SELECT en.entity_id AS eid, h.record_id AS rec, MIN(h.rank) AS best, h.match_type AS mt, h.score AS sc, COUNT(*) AS n "
            " FROM search_hits h LEFT JOIN entity_records en ON en.record_id = h.record_id AND en.active = 1 "
            " WHERE h.search_id = ? GROUP BY COALESCE(en.entity_id, -h.record_id))", (sid, sid))

    def _materialize(self, q: SearchQuery, sid: int) -> None:
        n = len(q.terms)
        self._compute_name_tiers(q)
        unified_terms = bool(q.unified and n)
        if unified_terms:
            self._compute_filter_hits(q)
        params: list = []
        if n and not unified_terms:
            src = ("SELECT record_id, MAX(best) AS worst, AVG(bscore) AS avgscore, group_concat(reason, ' | ') AS reasons FROM ("
                   "SELECT tidx, record_id, MIN(mrank) AS best, score AS bscore, reason FROM cand GROUP BY tidx, record_id) "
                   "GROUP BY record_id HAVING COUNT(*) = ?")
            params.append(n)
        elif n and unified_terms:
            # Unified: a record enters if it matches ANY term; field
            # coverage (terms + filters) decides full vs partial rank.
            src = ("SELECT record_id, MAX(best) AS worst, AVG(bscore) AS avgscore, group_concat(reason, ' | ') AS reasons, COUNT(*) AS nterms FROM ("
                   "SELECT tidx, record_id, MIN(mrank) AS best, score AS bscore, reason FROM cand GROUP BY tidx, record_id) "
                   "GROUP BY record_id HAVING COUNT(*) >= 1")
        else:
            subs, sp = [], []
            for f in q.filters:
                s, p = self._filter_sql(f)
                subs.append(s)
                sp += p
            src = "SELECT record_id, 2 AS worst, 1.0 AS avgscore, 'filter' AS reasons FROM (" + " INTERSECT ".join(subs) + ")"
            params += sp
        conds, cp = [], []
        if n and not unified_terms:
            for f in q.filters:
                s, p = self._filter_sql(f)
                conds.append(f"s.record_id IN ({s})")
                cp += p
        scope = "EXISTS (SELECT 1 FROM records r JOIN files fl ON fl.id = r.file_id WHERE r.id = s.record_id AND fl.status IN " + _OK_STATUS
        if q.file_ids:
            scope += " AND fl.id IN (" + ",".join("?" * len(q.file_ids)) + ")"
            cp += list(q.file_ids)
        conds.append(scope + ")")
        # Name qualifier appended to the stored reason (parsed by why()).
        # Tiered=0 (non-name/fuzzy queries) adds nothing, preserving the
        # old reason text exactly.
        name_q = ("COALESCE(reasons, '') || CASE WHEN COALESCE(nt.tiered,0)=1 THEN "
                  "CASE nt.tier WHEN 0 THEN ' | name:phrase' WHEN 1 THEN ' | name:ordered' "
                  "WHEN 2 THEN ' | name:position' WHEN 3 THEN ' | name:partial(' || nt.cov || '/' || nt.tot || ')' "
                  "ELSE ' | name:none' END ELSE '' END")
        tier_type = (f"CASE WHEN COALESCE(nt.tiered,0)=0 THEN {_TYPE_CASE} WHEN nt.tier=0 THEN 'exact_phrase' "
                     f"WHEN nt.tier=1 THEN 'name_ordered' WHEN nt.tier=2 THEN 'name_position' ELSE 'partial' END")
        join_nt = "LEFT JOIN temp.ntier nt ON nt.record_id = s.record_id"
        if unified_terms:
            total = int(n + len(q.filters))
            join_fh = "LEFT JOIN (SELECT record_id, COUNT(DISTINCT fidx) AS nf FROM temp.fhit GROUP BY record_id) fh ON fh.record_id = s.record_id"
            cov = "(nterms + COALESCE(nf,0))"
            mtype = (f"CASE WHEN {cov} < {total} THEN 'partial' ELSE {tier_type} END")
            reason = f"{name_q} || CASE WHEN {cov} = {total} THEN ' | field:full' ELSE ' | field:partial(' || {cov} || '/{total})' END"
            order = (f"(CASE WHEN {cov} = {total} THEN 0 ELSE 1 END), {cov} DESC, "
                     f"COALESCE(nt.is_fuzzy,0), COALESCE(nt.tier,0), worst, avgscore DESC, s.record_id")
            sql = (f"INSERT INTO search_hits(search_id, rank, record_id, match_type, score, reason) "
                   f"SELECT ?, ROW_NUMBER() OVER (ORDER BY {order}), s.record_id, {mtype}, avgscore, {reason} "
                   f"FROM ({src}) s {join_nt} {join_fh} WHERE " + " AND ".join(conds))
        else:
            order = "COALESCE(nt.is_fuzzy,0), COALESCE(nt.tier,0), worst, avgscore DESC, s.record_id"
            sql = (f"INSERT INTO search_hits(search_id, rank, record_id, match_type, score, reason) "
                   f"SELECT ?, ROW_NUMBER() OVER (ORDER BY {order}), s.record_id, {tier_type}, avgscore, {name_q} "
                   f"FROM ({src}) s {join_nt} WHERE " + " AND ".join(conds))
        # ترتيب المعاملات: sid ثم src ثم conds (totals above are inlined
        # ints, never user text, so no new placeholders are introduced).
        self.conn.execute(sql, [sid, *params, *cp])
        if q.limit:
            self.conn.execute("DELETE FROM search_hits WHERE search_id=? AND rank>?", (sid, q.limit))
