"""Streaming PDF writer (pages are flushed to disk as they fill; RAM stays flat).
Unicode/Arabic via embedded TTF (Identity-H) + own shaping/bidi. Falls back to
Helvetica (Latin only) when no font is available."""
from __future__ import annotations

import zlib
from pathlib import Path

from .arabic_shaping import visual
from .ttf import TTFont

PAGE_W, PAGE_H, MARGIN = 595.0, 842.0, 42.0


class PdfWriter:
    def __init__(self, path: Path, font_path: Path | None, title: str = "", header: str = "", font_size: float = 10.0) -> None:
        self.f = open(path, "wb")
        self.font = TTFont(font_path) if font_path and Path(font_path).is_file() else None
        self.size, self.lead = font_size, font_size * 1.55
        self.header, self.title = header, title
        self.offsets: dict[int, int] = {}
        self.next_id = 5  # 1 catalog, 2 pages, 3 font, 4 reserved
        self.page_ids: list[int] = []
        self.lines: list[tuple[float, str]] = []  # (x, hexstring)
        self.y = PAGE_H - MARGIN - 28
        self.used: dict[int, str] = {}
        self.warned_latin = False
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self._page_no = 0
        self._widths: dict[int, float] = {}

    # ---- low level
    def _obj(self, oid: int, body: bytes) -> None:
        self.offsets[oid] = self.f.tell()
        self.f.write(b"%d 0 obj\n" % oid + body + b"\nendobj\n")

    def _stream(self, oid: int, dictextra: bytes, data: bytes, compress: bool = True) -> None:
        if compress:
            data = zlib.compress(data, 6)
            dictextra += b" /Filter /FlateDecode"
        self._obj(oid, b"<< " + dictextra + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    # ---- text metrics
    def _glyphs(self, vis: str) -> list[int]:
        return [self.font.gid(ord(c)) or self.font.gid(ord("?")) for c in vis]

    def _width(self, vis: str) -> float:
        if not self.font:
            return len(vis) * self.size * 0.5
        return sum(self.font.advance(g) for g in self._glyphs(vis)) * self.size / self.font.units

    def _wrap(self, text: str, maxw: float) -> list[str]:
        words, lines, cur = text.split(" "), [], ""
        for w in words:
            trial = (cur + " " + w) if cur else w
            if self._width(visual(trial)[0]) <= maxw:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while self._width(visual(w)[0]) > maxw and len(w) > 1:  # كلمة طويلة: قطع بالحروف
                k = len(w)
                while k > 1 and self._width(visual(w[:k])[0]) > maxw:
                    k -= 1
                lines.append(w[:k])
                w = w[k:]
            cur = w
        lines.append(cur)
        return lines

    # ---- public
    def add_text(self, text: str, indent: float = 0.0, bold_gap: bool = False) -> None:
        maxw = PAGE_W - 2 * MARGIN - indent
        for raw in text.split("\n"):
            for line in self._wrap(raw, maxw):
                if self.y < MARGIN + 20:
                    self._flush_page()
                vis, rtl = visual(line)
                w = self._width(vis)
                x = (PAGE_W - MARGIN - w) if rtl else MARGIN + indent
                self._emit(x, vis)
                self.y -= self.lead
        if bold_gap:
            self.y -= self.lead * 0.5

    def _emit(self, x: float, vis: str) -> None:
        if self.font:
            gl = self._glyphs(vis)
            for c, g in zip(vis, gl):
                self.used.setdefault(g, c)
            self.lines.append((x, self.y, "<" + "".join(f"{g:04X}" for g in gl) + ">"))
        else:
            b = vis.encode("cp1252", "replace")
            esc = b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
            self.lines.append((x, self.y, "(" + esc.decode("latin1") + ")"))

    def _flush_page(self) -> None:
        self._page_no += 1
        ops = ["BT /F1 %.1f Tf" % self.size]
        last_x = last_y = 0.0
        for x, y, s in self.lines:
            ops.append("1 0 0 1 %.2f %.2f Tm %s Tj" % (x, y, s))
        ops.append("ET")
        # header + footer
        hdr = self.header
        foot = f"Page {self._page_no}"
        for txt, y in ((hdr, PAGE_H - MARGIN), (foot, MARGIN - 10)):
            vis, rtl = visual(txt)
            w = self._width(vis)
            x = PAGE_W - MARGIN - w if rtl and y > 100 else (MARGIN if y > 100 else (PAGE_W - w) / 2)
            if self.font:
                gl = self._glyphs(vis)
                for c, g in zip(vis, gl):
                    self.used.setdefault(g, c)
                s = "<" + "".join(f"{g:04X}" for g in gl) + ">"
            else:
                s = "(" + vis.encode("cp1252", "replace").decode("latin1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
            ops.append("BT /F1 8 Tf 1 0 0 1 %.2f %.2f Tm %s Tj ET" % (x, y, s))
        ops.append("0.6 g 0.5 w %.1f %.1f m %.1f %.1f l S" % (MARGIN, PAGE_H - MARGIN - 6, PAGE_W - MARGIN, PAGE_H - MARGIN - 6))
        cid, pid = self._new_id(), self._new_id()
        self._stream(cid, b"", "\n".join(ops).encode("latin1"))
        self._obj(pid, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.0f %.0f] /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (PAGE_W, PAGE_H, cid))
        self.page_ids.append(pid)
        self.lines, self.y = [], PAGE_H - MARGIN - 28

    def add_table(self, headers, rows, repeat_header=True):
        """Render a table using the existing writer only (no new deps, no fills).

        headers: list[str] | None (None = no header row, e.g. per-result 2-col tables).
        rows: lists of cell strings, or ("__SPAN__", text) full-width lines.
        Columns flow right-to-left (first column rightmost, correct for RTL);
        each cell is right-aligned within its column. The header row is re-emitted
        after every page break when repeat_header and headers are given, followed
        by a rule line. The page footer stays "Page N" (the streaming writer bakes
        footers at flush time, so a total page count would need a second pass).
        """
        ncols = len(headers) if headers else max((len(r) for r in rows if isinstance(r, list)), default=0)
        if ncols == 0:
            return
        # Widths are measured on visual order (same total as logical); wrapping
        # itself always runs on LOGICAL text (like add_text) and visual() is
        # applied only at emit time — never wrap already-visual text.
        gap = 8.0
        avail = PAGE_W - 2 * MARGIN
        widths = [0.0] * ncols
        if headers:
            for i, h in enumerate(headers):
                widths[i] = max(widths[i], self._width(visual(h)[0]) + 4)
        for r in rows:
            if isinstance(r, tuple):
                continue
            for i, c in enumerate(r):
                widths[i] = max(widths[i], self._width(visual(c)[0]) + 4)
        # Floor narrow columns, then scale down proportionally if over budget.
        widths = [max(w, 14.0) for w in widths]
        total = sum(widths) + gap * (ncols - 1)
        if total > avail:
            scale = avail / total
            widths = [w * scale for w in widths]
        # Right edges: column 0 is rightmost.
        edges = []
        x = PAGE_W - MARGIN
        for w in widths:
            edges.append(x)
            x -= w + gap

        def header_row():
            if not headers:
                return
            self._in_header = True
            try:
                wrapped = [self._wrap(h, widths[i]) for i, h in enumerate(headers)]
                h = max(len(w) for w in wrapped)
                for li in range(h):
                    need_space()
                    y0 = self.y
                    for i, cell in enumerate(wrapped):
                        if li < len(cell):
                            vis = visual(cell[li])[0]
                            self._emit(edges[i] - self._width(vis), vis)
                    self.y = y0 - self.lead
                self._emit(MARGIN, "_" * 70)
                self.y -= self.lead
            finally:
                self._in_header = False

        def need_space():
            if self.y < MARGIN + 20:
                self._flush_page()
                if repeat_header and not getattr(self, "_in_header", False):
                    header_row()

        need_space()
        header_row()
        for r in rows:
            if isinstance(r, tuple):
                need_space()
                self.add_text(r[1])
                continue
            wrapped = [self._wrap(c, widths[i]) for i, c in enumerate(r)]
            h = max(len(w) for w in wrapped)
            for li in range(h):
                need_space()
                y0 = self.y
                for i, cell in enumerate(wrapped):
                    if li < len(cell):
                        vis = visual(cell[li])[0]
                        self._emit(edges[i] - self._width(vis), vis)
                self.y = y0 - self.lead
            # Row separator (thin rule, same visual language as add_text rules).
            need_space()
            self._emit(MARGIN, "-" * 70)
            self.y -= self.lead

    def close(self) -> None:
        if self.lines or not self.page_ids:
            self._flush_page()
        self._write_font()
        kids = " ".join(f"{p} 0 R" for p in self.page_ids)
        self._obj(2, ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(self.page_ids))).encode())
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        info = self._new_id()
        self._obj(info, b"<< /Title <" + ("\ufeff" + self.title).encode("utf-16-be").hex().upper().encode() + b"> /Producer (PrivateDatabase) >>")
        xref = self.f.tell()
        size = self.next_id
        self.f.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
        for i in range(1, size):
            self.f.write(b"%010d 00000 n \n" % self.offsets.get(i, 0) if i in self.offsets else b"0000000000 65535 f \n")
        self.f.write(b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, info, xref))
        self.f.close()

    def _write_font(self) -> None:
        if not self.font:
            self._obj(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
            return
        ft = self.font
        desc, cidf, fontfile, tou = self._new_id(), self._new_id(), self._new_id(), self._new_id()
        ws = " ".join(f"{g} [{round(ft.advance(g) * 1000 / ft.units)}]" for g in sorted(self.used))
        self._obj(3, b"<< /Type /Font /Subtype /Type0 /BaseFont /EmbeddedFont /Encoding /Identity-H /DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (cidf, tou))
        self._obj(cidf, (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /EmbeddedFont /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                         b"/FontDescriptor %d 0 R /DW 600 /W [%s] /CIDToGIDMap /Identity >>" % (desc, ws.encode())))
        s = 1000.0 / ft.units
        bb = " ".join(str(round(v * s)) for v in ft.bbox)
        self._obj(desc, (b"<< /Type /FontDescriptor /FontName /EmbeddedFont /Flags 4 /FontBBox [%s] /ItalicAngle 0 /Ascent %d /Descent %d /CapHeight %d /StemV 80 /FontFile2 %d 0 R >>"
                         % (bb.encode(), round(ft.ascent * s), round(ft.descent * s), round(ft.ascent * s), fontfile)))
        self._stream(fontfile, b"/Length1 %d" % len(ft.data), ft.data)
        import unicodedata
        entries = []
        for g, ch in sorted(self.used.items()):
            base = unicodedata.normalize("NFKC", ch)
            entries.append("<%04X> <%s>" % (g, "".join("%04X" % ord(x) for x in base)))
        chunks = ["\n".join(entries[i:i + 100]) for i in range(0, len(entries), 100)]
        cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
                "1 begincodespacerange <0000> <FFFF> endcodespacerange\n" +
                "".join(f"{len(c.splitlines())} beginbfchar\n{c}\nendbfchar\n" for c in chunks) + "endcmap CMapName currentdict /CMap defineresource pop end end")
        self._stream(tou, b"", cmap.encode())
