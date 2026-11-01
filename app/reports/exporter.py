"""Report generation (HTML / TXT / PDF) streamed from search_hits. Never truncates:
the report contains ALL hits of the search.

Two styles (additive, 2026-10-27):
  A «احترافي» (default): header frame/band, meta, one card per result, sources, footer.
  B «جدولي»: one big table (one row per result, one column per field).
Levels (results/evidence/metadata) keep their semantics in both styles.
Style B buffers result rows (widths must be known before the header is written);
style A streams exactly like the original exporter.
"""
from __future__ import annotations

import html
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .. import audit
from ..config import Config
from ..auth import User
from ..errors import PermissionDenied
from ..jobs import JobContext, JobQueue, ProgressETA
from ..search.dsl import SearchQuery
from ..search.present import ResultRecord, iter_results
from ..storage import StorageManager

log = logging.getLogger("pdb.reports")
LEVELS = ("results", "evidence", "metadata")
FORMATS = ("html", "txt", "pdf")
STYLES = ("A", "B")
STYLE_NAMES = {"A": "احترافي", "B": "جدولي"}
ROOT = Path(__file__).resolve().parents[2]


def resolve_font(cfg: Config) -> Path | None:
    for cand in (cfg.pdf_font_path, str(ROOT / "assets" / "fonts" / "DejaVuSans.ttf")):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def norm_style(style) -> str:
    """Unknown or missing style always falls back to A (never crashes)."""
    return style if style in STYLES else "A"


def _split_levels(rr: ResultRecord, level: str, parent_name: str | None):
    """(field_rows, evidence_rows, metadata_rows) for one record."""
    fields = [(n, o) for n, _r, o in rr.fields]
    ev = []
    meta = []
    if level in ("evidence", "metadata"):
        ev = [("— المصدر —", ""), ("Source file", rr.file_name), ("Location", rr.locator or "Not available"),
              ("Matched fields", ", ".join(rr.matched) or "Not available"), ("Match", f"{rr.label_text} ({rr.match_type})"), ("Why", rr.why())]
    if level == "metadata":
        meta = [("File ID", str(rr.file_id)), ("SHA-256", rr.sha256), ("Size (bytes)", str(rr.size_bytes)), ("Format", rr.detected_type or "Not available"),
                ("Parser", rr.parser or "Not available"), ("Indexed at", rr.indexed_at or "Not available"), ("Archive parent", parent_name or "None"),
                ("Record ID", str(rr.record_id))]
    return fields, ev, meta


def _record_lines(rr: ResultRecord, level: str, parent_name: str | None) -> list[tuple[str, str]]:
    """(label, value) pairs for a record at the requested level (unchanged semantics)."""
    fields, ev, meta = _split_levels(rr, level, parent_name)
    return fields + ev + meta


def _ev_text(ev_rows) -> str:
    """Evidence rows (minus the separator) joined for one-line display in style B."""
    return " | ".join(f"{k}: {v}" for k, v in ev_rows if v or not k.startswith("—"))


def _wrap_cell(text: str, width: int) -> list[str]:
    """Wrap one TXT-B cell to `width` chars. Never truncates: only word separators
    at break points are dropped; every other character is preserved."""
    if width < 1:
        width = 1
    if not text:
        return [""]
    if len(text) <= width:
        return [text]
    lines = []
    rest = text
    while len(rest) > width:
        cut = rest.rfind(" ", 0, width + 1)
        if cut <= 0:
            cut = width
        lines.append(rest[:cut])
        rest = rest[cut:].lstrip(" ")
    lines.append(rest)
    return lines


class ReportService:
    def __init__(self, config: Config, storage: StorageManager, queue: JobQueue) -> None:
        self.cfg, self.storage, self.queue = config, storage, queue
        queue.register("export", self.handle_export)

    def request(self, user: User, search_id: int, formats: list[str], level: str, style: str = "A") -> int:
        user.require("export")
        if self.queue.conn().execute("SELECT 1 FROM search_history WHERE id=? AND user_id=?", (int(search_id), user.telegram_id)).fetchone() is None:
            raise PermissionDenied("search ownership")
        if level not in LEVELS or not formats or not set(formats) <= set(FORMATS):
            raise ValueError("صيغة/مستوى تقرير غير صالح")
        style = norm_style(style)
        return self.queue.submit("export", None, {"search_id": search_id, "formats": formats, "level": level, "style": style}, user.telegram_id)

    def reports_for_job(self, conn, job_id: int):
        return conn.execute("SELECT * FROM reports WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    # ------------------------------------------------------------------ job
    def handle_export(self, ctx: JobContext) -> None:
        conn, p = ctx.conn, ctx.payload
        sid, level = int(p["search_id"]), p["level"]
        style = norm_style(p.get("style", "A"))
        srow = conn.execute("SELECT * FROM search_history WHERE id=?", (sid,)).fetchone()
        if srow is not None and ctx.created_by is not None and srow["user_id"] != ctx.created_by:
            raise PermissionDenied("search ownership")
        if srow is None:
            raise RuntimeError("البحث غير موجود او تم تنظيفه (retention)")
        query = SearchQuery.from_json(srow["query_json"])
        total = srow["result_count"] or 0
        files = conn.execute("SELECT COUNT(DISTINCT r.file_id) FROM search_hits h JOIN records r ON r.id=h.record_id WHERE h.search_id=?", (sid,)).fetchone()[0]
        src_rows = conn.execute("SELECT f.original_name, COUNT(*) FROM search_hits h JOIN records r ON r.id=h.record_id JOIN files f ON f.id=r.file_id WHERE h.search_id=? GROUP BY f.id ORDER BY MIN(h.rank)", (sid,)).fetchall()
        sources = [(r[0], r[1]) for r in src_rows]
        now = datetime.now()
        id_rows = conn.execute("SELECT record_id FROM search_hits WHERE search_id=? ORDER BY rank", (sid,)).fetchall()
        record_ids = [r[0] for r in id_rows]
        parents: dict[int, str | None] = {}
        # Progress + ETA across ALL formats (rows are re-iterated per
        # format, so total work = result_count * n_formats). ETA comes
        # from the measured rate only; before that it stays None.
        n_formats = len(p["formats"])
        eta = ProgressETA(total * n_formats)
        done_rows = [0]
        fmt_done = [0]

        def tick(n: int) -> None:
            done_rows[0] += int(n)
            pct, eta_dt = eta.update(done_rows[0])
            ctx.progress(pct)
            if eta_dt is not None and eta.should_log(pct):
                ctx.log(f"تقدم {pct:.0f}% — الانتهاء المتوقع {eta_dt:%H:%M}")
        ctx.tick = tick
        for fmt in p["formats"]:
            ctx.check()
            rid = conn.execute("INSERT INTO reports(user_id, search_id, format, level, result_count, path, job_id, status) VALUES (?,?,?,?,?,?,?,'building')",
                               (ctx.created_by or 0, sid, fmt, level, total, "", ctx.id)).lastrowid
            name = f"search_{now:%Y-%m-%d_%H%M%S}_EXPORT-{rid:06d}.{fmt}"
            path = self.storage.dir("reports") / name
            header = {"id": f"EXPORT-{rid:06d}", "query": query.explain(), "time": now.isoformat(timespec="seconds"), "total": total, "files": files, "level": level,
                      "search_id": sid, "record_ids": record_ids, "style": style, "sources": sources}
            try:
                self._write(fmt, path, header, conn, sid, query, level, parents, ctx)
            except BaseException:
                conn.execute("UPDATE reports SET status='failed' WHERE id=?", (rid,))
                path.unlink(missing_ok=True)
                raise
            os.chmod(path, 0o600)
            conn.execute("UPDATE reports SET path=?, size_bytes=?, status='ready' WHERE id=?", (str(path), path.stat().st_size, rid))
            audit.log(conn, ctx.created_by, audit.EXPORT, "report", rid, {"format": fmt, "level": level, "results": total, "search_id": sid, "style": style})
            ctx.log(f"تقرير {fmt}: {name}")
            # Snap to exact per-format counts (the last partial <200-row
            # chunk of a format never ticked above).
            fmt_done[0] += 1
            if total:
                done_rows[0] = fmt_done[0] * total
                pct, _ = eta.update(done_rows[0])
                ctx.progress(pct)

    def _iter(self, conn, sid, query, parents, ctx):
        for i, rr in enumerate(iter_results(conn, sid, query=query)):
            if i % 200 == 0:
                ctx.check()
                if getattr(ctx, "tick", None):
                    ctx.tick(200)
            if rr.parent_id and rr.parent_id not in parents:
                r = conn.execute("SELECT original_name FROM files WHERE id=?", (rr.parent_id,)).fetchone()
                parents[rr.parent_id] = r["original_name"] if r else None
            yield rr, parents.get(rr.parent_id)

    def _collect_rows(self, conn, sid, query, level, parents, ctx):
        """Style B input: one iteration collecting every result row (field values,
        matched names, evidence/metadata lines). Widths must be known before the
        table header is written, so B cannot stream like A."""
        rows = []
        for rr, parent in self._iter(conn, sid, query, parents, ctx):
            fields, ev, meta = _split_levels(rr, level, parent)
            rows.append({"rank": rr.rank, "label": rr.label, "label_text": rr.label_text,
                         "file_name": rr.file_name, "record_id": rr.record_id,
                         "fields": fields, "matched": list(rr.matched),
                         "ev_text": _ev_text(ev), "meta": meta})
        return rows

    def _field_names(self, rows):
        names, seen = [], set()
        for row in rows:
            for n, _v in row["fields"]:
                if n not in seen:
                    seen.add(n)
                    names.append(n)
        return names

    def _write(self, fmt, path, header, conn, sid, query, level, parents, ctx) -> None:
        style = header["style"]
        if fmt == "txt":
            with open(path, "w", encoding="utf-8") as f:
                if style == "B":
                    self._txt_b(f, header, conn, sid, query, level, parents, ctx)
                else:
                    self._txt_a(f, header, conn, sid, query, level, parents, ctx)
        elif fmt == "html":
            if style == "B":
                self._html_b(path, header, conn, sid, query, level, parents, ctx)
            else:
                self._html_a(path, header, conn, sid, query, level, parents, ctx)
        else:
            if style == "B":
                self._pdf_b(path, header, conn, sid, query, level, parents, ctx)
            else:
                self._pdf_a(path, header, conn, sid, query, level, parents, ctx)

    # ------------------------------------------------------------ TXT style A
    def _txt_a(self, f, header, conn, sid, query, level, parents, ctx) -> None:
        total = header["total"]
        f.write("=" * 60 + "\nتقرير نتائج البحث\n" + "=" * 60 + "\n")
        f.write("الاستعلام:\n")
        f.writelines(f"  - {l}\n" for l in header["query"])
        f.write(f"عدد النتائج    : {total}\nعدد المصادر    : {header['files']}\nالمستوى        : {level}\nالشكل          : {STYLE_NAMES[header['style']]}\nرقم البحث      : {header['search_id']}\nتاريخ التقرير  : {header['time']}\n")
        # Machine-readable meta line (kept verbatim from the pre-redesign
        # layout: existing tests assert these exact strings).
        f.write(f"Search ID: {header['search_id']} | Results: {total} | Sources: {header['files']} | Level: {level}\n")
        f.write("=" * 60 + "\n")
        for rr, parent in self._iter(conn, sid, query, parents, ctx):
            rows = _record_lines(rr, level, parent)
            w = max((len(k) for k, v in rows if v or not k.startswith("—")), default=0)
            f.write(f"\n{'-' * 60}\nالنتيجة #{rr.rank} من {total} | المصدر: {rr.file_name} | السجل: {rr.record_id}\n{'-' * 60}\n")
            for k, v in rows:
                f.write(f"{k}\n" if not v and k.startswith("—") else f"{k.ljust(w)} : {v}\n")
        f.write("\n" + "=" * 60 + "\nالمصادر\n" + "=" * 60 + "\n")
        for i, (name, cnt) in enumerate(header["sources"], 1):
            dots = "." * max(3, 30 - len(name))
            f.write(f"{i}. {name} {dots} {cnt} سجلات\n")
        ids_txt = "، ".join(str(i) for i in header["record_ids"]) if header["record_ids"] else "none"
        f.write("=" * 60 + "\n")
        f.write(f"نهاية التقرير | رقم البحث: {header['search_id']} | أرقام السجلات: {ids_txt}\n")
        f.write(f"Provenance — Search ID: {header['search_id']} | Record IDs: {ids_txt}\n")
        f.write("تم إنشاء هذا التقرير من نتائج البحث فقط — لا يعد وثيقة رسمية.\n")
        f.write("=" * 60 + "\n")

    # ------------------------------------------------------------ TXT style B
    def _txt_b(self, f, header, conn, sid, query, level, parents, ctx) -> None:
        total = header["total"]
        rows = self._collect_rows(conn, sid, query, level, parents, ctx)
        names = self._field_names(rows)
        f.write("تقرير نتائج البحث (جدولي)\n" + "=" * 60 + "\n")
        f.write(f"رقم البحث: {header['search_id']} | عدد النتائج: {total} | عدد المصادر: {header['files']} | المستوى: {level} | الشكل: {STYLE_NAMES[header['style']]}\n")
        f.write(f"Search ID: {header['search_id']} | Results: {total} | Sources: {header['files']} | Level: {level}\n")
        f.write(f"تاريخ التقرير: {header['time']}\nالاستعلام:\n")
        f.writelines(f"  - {l}\n" for l in header["query"])
        f.write("=" * 60 + "\n")
        headers = ["#", "المصدر"] + names
        widths = [max(len("#"), len(str(total)))]
        widths.append(max(len("المصدر"), min(max((len(r["file_name"]) for r in rows), default=0), 24)))
        for n in names:
            vmax = max((len(dict(r["fields"]).get(n, "")) for r in rows), default=0)
            widths.append(max(len(n), min(vmax, 28)))
        f.write(" | ".join(c.ljust(w) for c, w in zip(headers, widths)) + "\n")
        f.write("-" * (sum(widths) + 3 * (len(widths) - 1)) + "\n")
        for r in rows:
            vals = dict(r["fields"])
            cells = [str(r["rank"]), r["file_name"]] + [vals.get(n, "") for n in names]
            wrapped = [_wrap_cell(c, w) for c, w in zip(cells, widths)]
            h = max(len(x) for x in wrapped)
            for li in range(h):
                parts = [(wrapped[i][li] if li < len(wrapped[i]) else "").ljust(widths[i]) for i in range(len(widths))]
                f.write(" | ".join(parts) + "\n")
            if level in ("evidence", "metadata") and r["ev_text"]:
                f.write(f"  أدلة: {r['ev_text']}\n")
        if level == "metadata":
            f.write("-" * 60 + "\nبيانات وصفية\n" + "-" * 60 + "\n")
            for r in rows:
                meta_txt = " | ".join(f"{k}: {v}" for k, v in r["meta"])
                f.write(f"السجل {r['record_id']} — {meta_txt}\n")
        f.write("=" * 60 + "\nالمصادر\n" + "=" * 60 + "\n")
        for i, (name, cnt) in enumerate(header["sources"], 1):
            dots = "." * max(3, 30 - len(name))
            f.write(f"{i}. {name} {dots} {cnt} سجلات\n")
        ids_txt = "، ".join(str(i) for i in header["record_ids"]) if header["record_ids"] else "none"
        f.write("=" * 60 + "\n")
        f.write(f"نهاية التقرير | رقم البحث: {header['search_id']} | أرقام السجلات: {ids_txt}\n")
        f.write(f"Provenance — Search ID: {header['search_id']} | Record IDs: {ids_txt}\n")
        f.write("تم إنشاء هذا التقرير من نتائج البحث فقط — لا يعد وثيقة رسمية.\n")
        f.write("=" * 60 + "\n")

    # ----------------------------------------------------------- HTML styles
    _CSS_BASE = ("body{font-family:'Segoe UI',Tahoma,'Noto Sans Arabic',sans-serif;margin:0;background:#F3F4F6;color:#111827}"
                 "header{background:#1F2937;color:#fff;padding:28px 32px}header h1{margin:0 0 6px;font-size:24px}header p{margin:0;color:#D1D5DB;font-size:14px}"
                 ".page{max-width:820px;margin:24px auto;background:#fff;border:1px solid #E5E7EB}"
                 ".meta{display:flex;flex-wrap:wrap;gap:8px;padding:16px 32px;border-bottom:1px solid #E5E7EB;background:#F9FAFB}"
                 ".chip{background:#fff;border:1px solid #E5E7EB;border-radius:999px;padding:5px 12px;font-size:13px}.chip b{color:#1F2937}"
                 ".query{padding:12px 32px;font-size:14px}.query ul{margin:6px 0}"
                 ".results{padding:8px 32px 24px}"
                 "footer{border-top:1px solid #E5E7EB;padding:16px 32px;font-size:12.5px;color:#6B7280;background:#F9FAFB}"
                 "h2{font-size:17px;margin:28px 0 10px}"
                 "table.srcs{width:100%;border-collapse:collapse;font-size:14px}table.srcs th{text-align:right;background:#1F2937;color:#fff;padding:9px 12px}"
                 "table.srcs td{padding:9px 12px;border-bottom:1px solid #E5E7EB}"
                 "mark{background:#FEF3C7;padding:0 3px;border-radius:3px}"
                 "@media print{body{background:#fff}.page{border:none;margin:0;max-width:none}}")
    _CSS_A = (".card{border:1px solid #E5E7EB;border-radius:10px;margin-top:18px;overflow:hidden;break-inside:avoid}"
              ".card-head{display:flex;align-items:center;gap:12px;background:#F9FAFB;padding:12px 16px;border-bottom:1px solid #E5E7EB}"
              ".card-head h3{margin:0;font-size:16px}"
              ".rank{width:30px;height:30px;min-width:30px;border-radius:50%;background:#1F2937;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:14px}"
              ".card-head .src{font-size:13px;color:#6B7280}"
              "table.fields{width:100%;border-collapse:collapse}table.fields td{padding:9px 14px;border-bottom:1px solid #F3F4F6;font-size:15px;vertical-align:top}"
              "table.fields tr:last-child td{border-bottom:none}table.fields td.lbl{width:150px;background:#F9FAFB;color:#374151;font-weight:600}"
              "tr.sec td{font-weight:bold;background:#F0F0F0}")
    _CSS_B = ("table.big{width:100%;border-collapse:collapse;font-size:14px}table.big th{background:#1F2937;color:#fff;padding:9px 10px;text-align:right}"
              "table.big td{padding:8px 10px;border-bottom:1px solid #E5E7EB;vertical-align:top;word-break:break-word}"
              "table.big tbody tr:nth-child(even){background:#F9FAFB}tr.ev td{background:#FFFBEB;font-size:13px;color:#374151}")

    def _html_foot(self, header):
        e = html.escape
        ids_txt = "، ".join(str(i) for i in header["record_ids"]) if header["record_ids"] else "none"
        src_rows = "".join(f"<tr><td>{e(n)}</td><td>{c}</td></tr>" for n, c in header["sources"])
        return (f'<h2>المصادر</h2><table class="srcs"><tr><th>المصدر</th><th>السجلات الظاهرة</th></tr>{src_rows}</table>'
                f'</div><footer>رقم البحث: {header["search_id"]} · أرقام السجلات: {e(ids_txt)} · '
                f'تم إنشاء هذا التقرير من نتائج البحث فقط — لا يعد وثيقة رسمية.'
                f'<br>Search ID: <b>{header["search_id"]}</b> · Record IDs: {e(ids_txt)}</footer></div></body></html>')

    def _html_a(self, path, header, conn, sid, query, level, parents, ctx) -> None:
        e = html.escape
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>{e(header["id"])}</title><style>{self._CSS_BASE}{self._CSS_A}</style></head><body>')
            f.write(self._html_head_body(header))
            for rr, parent in self._iter(conn, sid, query, parents, ctx):
                fields, ev, meta = _split_levels(rr, level, parent)
                # Card title = the record's name value when present, else its
                # first field value, else the match-type text (never the emoji
                # label: the proposal sample has no emoji, and a non-BMP char
                # would force UCS4 decoding of the whole file on read).
                title = dict(fields).get("الاسم الكامل") or (fields[0][1] if fields else rr.label_text)
                f.write(f'<div class="card"><div class="card-head"><div class="rank">{rr.rank}</div>'
                        f'<div><h3>#{rr.rank} {e(title)}</h3><div class="src">{e(rr.file_name)} · السجل {rr.record_id}</div></div></div><table class="fields">')
                for n, o in fields:
                    val = f"<mark>{e(o)}</mark>" if n in rr.matched else e(o)
                    f.write(f'<tr><td class="lbl" dir="auto">{e(n)}</td><td dir="auto">{val}</td></tr>')
                for k, v in ev:
                    if not v and k.startswith("—"):
                        f.write(f'<tr class="sec"><td colspan="2">{e(k)}</td></tr>')
                    else:
                        f.write(f'<tr><td class="lbl" dir="auto">{e(k)}</td><td dir="auto">{e(v)}</td></tr>')
                for k, v in meta:
                    f.write(f'<tr><td class="lbl" dir="auto">{e(k)}</td><td dir="auto">{e(v)}</td></tr>')
                f.write("</table></div>")
            f.write(self._html_foot(header))

    def _html_head_body(self, header):
        """Header band + meta chips + query list, without <style> (style is in <head>)."""
        e = html.escape
        chips = (f'<span class="chip">رقم البحث: <b>{header["search_id"]}</b></span>'
                 f'<span class="chip">التاريخ: <b>{e(header["time"])}</b></span>'
                 f'<span class="chip">المستوى: <b>{e(header["level"])}</b></span>'
                 f'<span class="chip">الشكل: <b>{STYLE_NAMES[header["style"]]}</b></span>')
        query = "".join(f"<li dir=auto>{e(l)}</li>" for l in header["query"])
        return (f'<div class="page"><header><h1>تقرير نتائج البحث</h1>'
                f'<p>{e(header["id"])} &nbsp;·&nbsp; <b>{header["total"]}</b> نتائج من {header["files"]} مصادر</p></header>'
                f'<div class="meta">{chips}</div><div class="query">الاستعلام:<ul>{query}</ul></div><div class="results">')

    def _html_b(self, path, header, conn, sid, query, level, parents, ctx) -> None:
        e = html.escape
        rows = self._collect_rows(conn, sid, query, level, parents, ctx)
        names = self._field_names(rows)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>{e(header["id"])}</title><style>{self._CSS_BASE}{self._CSS_B}</style></head><body>')
            f.write(self._html_head_body(header))
            f.write('<table class="big"><thead><tr><th>#</th><th>المصدر</th>')
            f.write("".join(f"<th dir=auto>{e(n)}</th>" for n in names))
            f.write("</tr></thead><tbody>")
            ncol = 2 + len(names)
            for r in rows:
                vals = dict(r["fields"])
                f.write(f"<tr><td>{r['rank']}</td><td>{e(r['file_name'])}</td>")
                for n in names:
                    o = vals.get(n, "")
                    val = f"<mark>{e(o)}</mark>" if n in r["matched"] else e(o)
                    f.write(f"<td dir=auto>{val}</td>")
                f.write("</tr>")
                if level in ("evidence", "metadata") and r["ev_text"]:
                    f.write(f'<tr class="ev"><td colspan="{ncol}">أدلة: {e(r["ev_text"])}</td></tr>')
            f.write("</tbody></table>")
            if level == "metadata":
                f.write("<h2>بيانات وصفية</h2>")
                for r in rows:
                    meta_txt = " | ".join(f"{k}: {v}" for k, v in r["meta"])
                    f.write(f"<p>السجل {r['record_id']} — {e(meta_txt)}</p>")
            f.write(self._html_foot(header))

    # ------------------------------------------------------------ PDF styles
    def _pdf_a(self, path, header, conn, sid, query, level, parents, ctx) -> None:
        font = resolve_font(self.cfg)
        pdf = PdfWriterCtx(path, font, header)
        try:
            pdf.w.add_text("تقرير نتائج البحث")
            pdf.w.add_text("=" * 70, bold_gap=True)
            for l in header["query"]:
                pdf.w.add_text("- " + l, indent=12)
            pdf.w.add_text(f"عدد النتائج: {header['total']}   عدد المصادر: {header['files']}   المستوى: {level}   الشكل: {STYLE_NAMES[header['style']]}")
            pdf.w.add_text(f"رقم البحث: {header['search_id']}   تاريخ التقرير: {header['time']}")
            pdf.w.add_text("=" * 70, bold_gap=True)
            if font is None:
                pdf.w.add_text("[NOTE] No Unicode font found: Arabic text cannot be rendered in this PDF. Set PDF_FONT_PATH. HTML/TXT reports are complete.")
            for rr, parent in self._iter(conn, sid, query, parents, ctx):
                pdf.w.add_text(f"النتيجة {rr.rank} من {header['total']} | المصدر: {rr.file_name} | السجل: {rr.record_id}")
                fields, ev, meta = _split_levels(rr, level, parent)
                if rr.matched and level == "results":
                    pdf.w.add_text("الحقول المطابقة: " + ", ".join(rr.matched), indent=14)
                pdf.w.add_table(None, [[k, v] for k, v in fields])
                if ev:
                    pdf.w.add_text("— المصدر —")
                    pdf.w.add_table(None, [[k, v] for k, v in ev if v or not k.startswith("—")])
                if meta:
                    pdf.w.add_table(None, [[k, v] for k, v in meta])
                pdf.w.add_text("-" * 70, bold_gap=True)
            pdf.w.add_text("المصادر")
            for i, (name, cnt) in enumerate(header["sources"], 1):
                pdf.w.add_text(f"{i}. {name} — {cnt} سجلات", indent=14)
            pdf.w.add_text(f"Provenance — Search ID: {header['search_id']} | Generated: {header['time']} | Results: {header['total']}")
            pdf.w.add_text("Record IDs: " + (", ".join(str(i) for i in header["record_ids"]) if header["record_ids"] else "none"))
        finally:
            pdf.close()

    def _pdf_b(self, path, header, conn, sid, query, level, parents, ctx) -> None:
        font = resolve_font(self.cfg)
        pdf = PdfWriterCtx(path, font, header)
        try:
            pdf.w.add_text("تقرير نتائج البحث (جدولي)")
            pdf.w.add_text("=" * 70, bold_gap=True)
            for l in header["query"]:
                pdf.w.add_text("- " + l, indent=12)
            pdf.w.add_text(f"عدد النتائج: {header['total']}   عدد المصادر: {header['files']}   المستوى: {level}   الشكل: {STYLE_NAMES[header['style']]}")
            pdf.w.add_text(f"رقم البحث: {header['search_id']}   تاريخ التقرير: {header['time']}")
            pdf.w.add_text("=" * 70, bold_gap=True)
            if font is None:
                pdf.w.add_text("[NOTE] No Unicode font found: Arabic text cannot be rendered in this PDF. Set PDF_FONT_PATH. HTML/TXT reports are complete.")
            rows = self._collect_rows(conn, sid, query, level, parents, ctx)
            names = self._field_names(rows)
            table_rows = []
            for r in rows:
                vals = dict(r["fields"])
                table_rows.append([str(r["rank"]), r["file_name"]] + [vals.get(n, "") for n in names])
                if rr_matched := r["matched"]:
                    if level == "results":
                        table_rows.append(("__SPAN__", "الحقول المطابقة: " + ", ".join(rr_matched)))
                if level in ("evidence", "metadata") and r["ev_text"]:
                    table_rows.append(("__SPAN__", "أدلة: " + r["ev_text"]))
            pdf.w.add_table(["#", "المصدر"] + names, table_rows, repeat_header=True)
            if level == "metadata":
                pdf.w.add_text("بيانات وصفية")
                for r in rows:
                    meta_txt = " | ".join(f"{k}: {v}" for k, v in r["meta"])
                    pdf.w.add_text(f"السجل {r['record_id']} — {meta_txt}", indent=14)
            pdf.w.add_text("المصادر")
            for i, (name, cnt) in enumerate(header["sources"], 1):
                pdf.w.add_text(f"{i}. {name} — {cnt} سجلات", indent=14)
            pdf.w.add_text(f"Provenance — Search ID: {header['search_id']} | Generated: {header['time']} | Results: {header['total']}")
            pdf.w.add_text("Record IDs: " + (", ".join(str(i) for i in header["record_ids"]) if header["record_ids"] else "none"))
        finally:
            pdf.close()


class PdfWriterCtx:
    def __init__(self, path, font, header) -> None:
        from .pdf import PdfWriter
        style_name = STYLE_NAMES.get(header.get("style", "A"), "احترافي")
        self.w = PdfWriter(path, font, title=header["id"], header=f"{header['id']} | {style_name} | {header['time']}")

    def close(self) -> None:
        self.w.close()
