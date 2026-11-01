"""Turn search hits into displayable records (with provenance) and apply RBAC masking."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Iterator

from ..auth import User, mask_value
from .dsl import SearchQuery
from .engine import filter_candidates, term_norms


@dataclass
class ResultRecord:
    rank: int
    record_id: int
    match_type: str
    score: float | None
    reason: str | None
    file_id: int
    file_name: str
    sha256: str
    size_bytes: int
    parser: str | None
    detected_type: str | None
    parent_id: int | None
    indexed_at: str | None
    locator: str | None
    fields: list[tuple[str, str, str]] = field(default_factory=list)  # (name, role, original)
    matched: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return match_label(self.match_type, self.score)[0]

    @property
    def label_text(self) -> str:
        return match_label(self.match_type, self.score)[1]

    def why(self) -> str:
        """Human explanation built ONLY from stored evidence (no AI, no invention)."""
        kind = {"exact_field": "تطابق تام لقيمة الحقل", "exact_token": "الكلمة موجودة كما هي", "normalized_exact": "تطابق بعد التطبيع (تشكيل/همزات/أرقام)",
                "prefix": "القيمة تبدأ بنص البحث", "fulltext": "تطابق جزئي بالنص الكامل", "contains": "نص البحث موجود داخل القيمة",
                "fuzzy": "تشابه تقريبي (قد لا يكون نفس الكلمة)",
                "exact_phrase": "تطابق تام كعبارة (الاسم كاملاً بالترتيب)",
                "name_ordered": "تطابق الاسم بالترتيب",
                "name_position": "كلمات الاسم موجودة لكن بترتيب مختلف",
                "partial": "مطابقة جزئية"}[self.match_type]
        extra = ""
        if self.reason and "fuzzy:" in self.reason:
            part = self.reason.split("fuzzy:", 1)[1].split("|")[0].split(":")
            extra = f" — كلمة مشابهة: {part[0]} ({part[1]})"
        if self.reason:
            m = re.search(r"name:(phrase|ordered|position|partial\((\d+)/(\d+)\)|none)", self.reason)
            if m:
                if m.group(1) == "phrase":
                    extra += " — الاسم مطابق كعبارة كاملة بالترتيب"
                elif m.group(1) == "ordered":
                    extra += " — الاسم مطابق بالترتيب"
                elif m.group(1) == "position":
                    extra += " — كلمات الاسم موجودة لكن بترتيب مختلف عن البحث"
                elif m.group(1).startswith("partial"):
                    extra += f" — مطابقة جزئية بالاسم: {m.group(2)} من {m.group(3)} كلمات"
                else:
                    extra += " — كلمات الاسم غير موجودة بحقل الاسم (وجدت بحقول أخرى)"
            f = re.search(r"field:(full|partial\((\d+)/(\d+)\))", self.reason)
            if f:
                if f.group(1) == "full":
                    extra += " — يطابق كل الحقول المكتشفة"
                else:
                    extra += f" — يطابق {f.group(2)} من {f.group(3)} حقول مكتشفة"
        where = ("، الحقول: " + ", ".join(self.matched)) if self.matched else ""
        return kind + extra + where


def match_label(mtype: str, score: float | None) -> tuple[str, str]:
    if mtype == "exact_field":
        return "🟢", "Exact Match"
    if mtype == "exact_token":
        return "🟢", "Token Match"
    if mtype == "normalized_exact":
        return "🟢", "Normalized Match"
    if mtype == "exact_phrase":
        return "🟢", "Exact Phrase Match"
    if mtype == "name_ordered":
        return "🟢", "Name Match (In Order)"
    if mtype == "name_position":
        return "🔀", "Name Match (Wrong Order)"
    if mtype == "partial":
        return "🔶", "Partial Match"
    if mtype == "fuzzy":
        return ("🟡", "Fuzzy Match (Strong)") if (score or 0) >= 0.85 else ("🟠", "Fuzzy Match (Possible)")
    return "🔵", {"prefix": "Prefix Match", "fulltext": "Full Text Match", "contains": "Contains Match"}.get(mtype, "Match")


def _matched_fields(fields, query: SearchQuery | None, reason: str | None = None) -> list[str]:
    if query is None:
        return []
    norms = [n for t in query.terms for n in term_norms(t)]
    norms += re.findall(r"fuzzy:([^:|]+):", reason or "")   # الكلمة المشابهة التي اوجدها fuzzy فعلاً من قاعدة البيانات
    years = [f.value for f in query.filters if f.op == "year"]
    out = []
    for name, role, _orig, norm in fields:
        if norm and ((norms and any(n and n in norm for n in norms)) or (role == "date" and any(norm.startswith(y + "-") for y in years))):
            out.append(name)
    fnorms = [(f.role, c) for f in query.filters if f.op != "year" for c in filter_candidates(f)]
    for name, role, _o, norm in fields:
        if norm and any(r == role and n and n in norm for r, n in fnorms) and name not in out:
            out.append(name)
    return out


def iter_results(conn: sqlite3.Connection, search_id: int, offset: int = 0, limit: int | None = None, batch: int = 500,
                 query: SearchQuery | None = None) -> Iterator[ResultRecord]:
    """Stream results in rank order using keyset pagination (constant memory)."""
    if query is None:
        row = conn.execute("SELECT query_json FROM search_history WHERE id=?", (search_id,)).fetchone()
        query = SearchQuery.from_json(row["query_json"]) if row else None
    emitted, last_rank = 0, offset  # rank يبدأ من 1
    while limit is None or emitted < limit:
        n = batch if limit is None else min(batch, limit - emitted)
        hits = conn.execute("SELECT rank, record_id, match_type, score, reason FROM search_hits WHERE search_id=? AND rank>? ORDER BY rank LIMIT ?",
                            (search_id, last_rank, n)).fetchall()
        if not hits:
            return
        ids = [h["record_id"] for h in hits]
        q = ",".join("?" * len(ids))
        recs = {r["id"]: r for r in conn.execute(
            f"SELECT r.id, r.locator, f.id AS fid, f.original_name, f.sha256, f.size_bytes, f.parser, f.detected_type, f.parent_file_id, f.indexed_at "
            f"FROM records r JOIN files f ON f.id=r.file_id WHERE r.id IN ({q})", ids)}
        vals: dict[int, list] = {}
        for v in conn.execute(
                f"SELECT rv.record_id, fl.name, fl.detected_type AS role, rv.original_value, rv.normalized_value FROM record_values rv "
                f"JOIN fields fl ON fl.id=rv.field_id WHERE rv.record_id IN ({q}) ORDER BY rv.record_id, fl.position", ids):
            vals.setdefault(v["record_id"], []).append((v["name"], v["role"] or "text", v["original_value"], v["normalized_value"]))
        for h in hits:
            r = recs.get(h["record_id"])
            if r is None:
                continue
            fv = vals.get(h["record_id"], [])
            yield ResultRecord(h["rank"], h["record_id"], h["match_type"], h["score"], h["reason"], r["fid"], r["original_name"], r["sha256"],
                               r["size_bytes"], r["parser"], r["detected_type"], r["parent_file_id"], r["indexed_at"], r["locator"],
                               [(n, ro, o) for n, ro, o, _ in fv], _matched_fields(fv, query, h["reason"]))
            emitted += 1
        last_rank = hits[-1]["rank"]


def mask_for(rr: ResultRecord, user: User) -> ResultRecord:
    return rr  # لا masking: ADMIN/USER يرون القيم كاملة ضمن صلاحياتهم


def archive_parent_name(conn, parent_id: int | None) -> str | None:
    if not parent_id:
        return None
    r = conn.execute("SELECT original_name FROM files WHERE id=?", (parent_id,)).fetchone()
    return r["original_name"] if r else None


@dataclass
class UnifiedResult:
    rank: int
    entity_id: int | None
    public_id: str | None
    match_type: str
    score: float | None
    hit_count: int
    view: dict | None          # unified view (None for records outside ER)
    record: "ResultRecord | None"  # representative raw record (always available)
    evidence: list = field(default_factory=list)  # ما الذي ادى للمطابقة: القيمة الاصلية + المصدر + الصف + هل هي variant

    @property
    def label(self) -> str:
        return match_label(self.match_type, self.score)[0]

    @property
    def label_text(self) -> str:
        return match_label(self.match_type, self.score)[1]


def iter_groups(conn: sqlite3.Connection, search_id: int, offset: int = 0, limit: int | None = None, query: SearchQuery | None = None) -> Iterator[UnifiedResult]:
    """Entity-level results. Falls back to raw records for rows that are not part of ER."""
    from ..er import entities
    if query is None:
        row = conn.execute("SELECT query_json FROM search_history WHERE id=?", (search_id,)).fetchone()
        query = SearchQuery.from_json(row["query_json"]) if row else None
    rows = conn.execute("SELECT * FROM search_groups WHERE search_id=? AND rank>? ORDER BY rank" + (" LIMIT ?" if limit else ""), (search_id, offset) + ((limit,) if limit else ())).fetchall()
    for g in rows:
        rec = None
        hit = conn.execute("SELECT rank FROM search_hits WHERE search_id=? AND record_id=?", (search_id, g["record_id"])).fetchone()
        for rr in iter_results(conn, search_id, offset=(hit["rank"] - 1) if hit else 0, limit=1, query=query):
            rec = rr
        view = entities.entity_view(conn, g["entity_id"]) if g["entity_id"] else None
        ev = match_evidence(conn, search_id, g["entity_id"], query, view) if g["entity_id"] else _record_evidence(rec)
        yield UnifiedResult(g["rank"], g["entity_id"], view["public_id"] if view else None, g["match_type"], g["score"], g["hit_count"], view, rec, ev)


def _record_evidence(rr: "ResultRecord | None") -> list[dict]:
    if rr is None:
        return []
    vals = {n: o for n, _r, o in rr.fields}
    return [{"field": c, "value": vals.get(c), "file": rr.file_name, "archive": None, "locator": rr.locator, "match_type": rr.match_type, "variant": False, "why": rr.why()} for c in rr.matched[:3]]


def match_evidence(conn: sqlite3.Connection, search_id: int, entity_id: int, query: SearchQuery | None, view: dict | None, limit: int = 4) -> list[dict]:
    """Which stored value(s) caused the match, where they came from (file/row) and whether the value is a
    VARIANT (a known value other than the entity's preferred one). Built only from imported data."""
    hits = conn.execute(
        "SELECT h.rank, h.match_type FROM search_hits h JOIN entity_records en ON en.record_id=h.record_id AND en.active=1 "
        "WHERE h.search_id=? AND en.entity_id=? ORDER BY h.rank LIMIT ?", (search_id, entity_id, limit)).fetchall()
    # (record_id, column) -> index of the known-value group inside its field (0 = preferred)
    group_of: dict[tuple[int, str], int] = {}
    if view:
        for sf, f in view["fields"].items():
            for gi, k in enumerate(f["known"]):
                for s in k["sources"]:
                    group_of[(s["record_id"], s["column"])] = gi
    out = []
    for h in hits:
        for rr in iter_results(conn, search_id, offset=h["rank"] - 1, limit=1, query=query):
            vals = {n: o for n, _r, o in rr.fields}
            parent = archive_parent_name(conn, rr.parent_id)
            for col in rr.matched[:2]:
                out.append({"field": col, "value": vals.get(col), "file": rr.file_name, "archive": parent, "locator": rr.locator, "record_id": rr.record_id,
                            "match_type": rr.match_type, "variant": group_of.get((rr.record_id, col), 0) > 0 or (rr.match_type == "fuzzy"), "why": rr.why()})
    return out
