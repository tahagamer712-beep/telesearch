"""Legacy .xls (BIFF8, Excel 97-2003) parser in pure stdlib. Streams the Workbook stream.
Not supported (reported clearly): BIFF5/95, password-protected workbooks."""
from __future__ import annotations

import struct
from typing import Iterator

from ..errors import ParserError
from ..norm.dates import excel_serial_to_iso
from .base import Parser, Record
from .cfb import CFB
from .registry import register
from .tabular import sheet_header

_DATE_FMTS = set(range(14, 23)) | {45, 46, 47}


def _is_date_fmt(code: str) -> bool:
    import re
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code.lower())
    return bool(re.search(r"[dmyhs]", code))


def _rk(v: int) -> float:
    if v & 2:
        n = v >> 2
        if n & 0x20000000:
            n -= 0x40000000
        val = float(n)
    else:
        val = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return val / 100 if v & 1 else val


def _num(x: float) -> str:
    # أرقام صحيحة طويلة (سيريالات/كروت 16+ رقم) كانت تطلع "1234567890123456.0" او 1.2e+16
    if x != x or x in (float("inf"), float("-inf")):
        return repr(x)
    return str(int(x)) if x == int(x) and abs(x) < 1e18 else repr(x)


class _Seg:
    """Reads an SST split across CONTINUE records."""
    def __init__(self, chunks: list[bytes]) -> None:
        self.c, self.i, self.p = chunks, 0, 0

    def _adv(self) -> None:
        while self.i < len(self.c) and self.p >= len(self.c[self.i]):
            self.i, self.p = self.i + 1, 0

    def eof(self) -> bool:
        self._adv()
        return self.i >= len(self.c)

    def take(self, n: int) -> bytes:
        out = b""
        while n > 0:
            self._adv()
            if self.i >= len(self.c):
                raise ParserError("SST مقطوع")
            piece = self.c[self.i][self.p:self.p + n]
            self.p += len(piece)
            n -= len(piece)
            out += piece
        return out

    def string(self) -> str:
        nchars = struct.unpack("<H", self.take(2))[0]
        flags = self.take(1)[0]
        wide, ext, rich = flags & 1, flags & 4, flags & 8
        runs = struct.unpack("<H", self.take(2))[0] if rich else 0
        extlen = struct.unpack("<I", self.take(4))[0] if ext else 0
        out: list[str] = []
        left = nchars
        while left > 0:
            self._adv()
            if self.i >= len(self.c):
                raise ParserError("SST مقطوع داخل نص")
            avail = (len(self.c[self.i]) - self.p) // (2 if wide else 1)
            n = min(left, avail)
            if n:
                raw = self.c[self.i][self.p:self.p + n * (2 if wide else 1)]
                out.append(raw.decode("utf-16-le" if wide else "latin-1"))
                self.p += len(raw)
                left -= n
            if left > 0:  # استمرار في record CONTINUE: اول بايت = flags جديدة
                self.i, self.p = self.i + 1, 0
                if self.i >= len(self.c):
                    raise ParserError("SST مقطوع")
                wide = self.c[self.i][0] & 1
                self.p = 1
        self.take(runs * 4)
        self.take(extlen)
        return "".join(out)


@register
class XLSParser(Parser):
    name = "xls"
    kinds = ("xls",)
    multi_dataset = True

    def _records(self, stream) -> Iterator[tuple[int, int, bytes]]:
        off = 0
        while True:
            h = stream.read(4)
            if len(h) < 4:
                return
            rid, ln = struct.unpack("<HH", h)
            body = stream.read(ln)
            if len(body) < ln:
                raise ParserError("XLS مقطوع (ملف ناقص/تالف)")
            yield off, rid, body
            off += 4 + ln

    def records(self, skip: int = 0) -> Iterator[Record]:
        try:
            cfb = CFB(self.path)
        except (ParserError, OSError, struct.error) as exc:
            raise ParserError(f"XLS تالف: {exc}") from exc
        try:
            entry = cfb.find("Workbook", "Book")
            if entry is None:
                raise ParserError("لا يوجد Workbook داخل الملف (BIFF5 او ليس Excel)")
            stream = cfb.open_stream(entry)
            yield from self._parse(stream, skip)
        except (struct.error, IndexError) as exc:
            raise ParserError(f"بنية XLS غير صالحة: {exc}") from exc
        finally:
            cfb.close()

    def _parse(self, stream, skip: int) -> Iterator[Record]:
        sst: list[str] = []
        sheets: dict[int, str] = {}
        formats: dict[int, str] = {}
        xf_fmt: list[int] = []
        date1904 = False
        pending: list[tuple[int, int, bytes]] = []
        it = iter(self._records(stream))
        first = True
        n = 0
        cur_sheet: str | None = None
        rows: dict[int, dict[int, str | None]] = {}
        header: dict[int, str] | None = None
        last_formula: tuple[int, int] | None = None
        buf_rec = None

        def next_rec():
            nonlocal buf_rec
            if buf_rec is not None:
                r, buf_rec = buf_rec, None
                return r
            return next(it, None)

        def date_xf(xf: int) -> bool:
            if xf >= len(xf_fmt):
                return False
            fid = xf_fmt[xf]
            return fid in _DATE_FMTS or (fid in formats and _is_date_fmt(formats[fid]))

        def flush_row(rownum: int):
            nonlocal header, n
            cells = rows.pop(rownum, {})
            if not any(v not in (None, "") for v in cells.values()):
                return None
            if header is None:
                header, is_hdr = sheet_header(cells)
                if is_hdr:
                    return None
                if len(self.warnings) < 20:
                    self.warnings.append(f"الورقة {cur_sheet}: لم يُكتشف صف عناوين؛ استُخدمت col_N")
            n += 1
            if n <= skip:
                return None
            return Record(cur_sheet or "sheet", f"sheet={cur_sheet};row={rownum + 1}", {header.get(c, f"col_{c + 1}"): v for c, v in cells.items()})

        while True:
            r = next_rec()
            if r is None:
                break
            off, rid, body = r
            if first:
                if rid != 0x809:
                    raise ParserError("XLS: البداية ليست BOF")
                ver = struct.unpack("<H", body[:2])[0]
                if ver != 0x600:
                    raise ParserError(f"XLS بصيغة BIFF قديمة (0x{ver:04X}) غير مدعومة؛ احفظه كـxlsx")
                first = False
            if rid == 0x2F:
                raise ParserError("XLS محمي بكلمة مرور (FILEPASS) غير مدعوم")
            if rid == 0x22:
                date1904 = bool(struct.unpack("<H", body[:2])[0])
            elif rid == 0x85:
                pos = struct.unpack("<I", body[:4])[0]
                cch, fl = body[6], body[7]
                nm = body[8:8 + cch * (2 if fl & 1 else 1)].decode("utf-16-le" if fl & 1 else "latin-1")
                if body[5] == 0:
                    sheets[pos] = nm
            elif rid == 0xFC:
                chunks = [body[8:]]
                while True:
                    nx = next_rec()
                    if nx is None:
                        break
                    if nx[1] == 0x3C:
                        chunks.append(nx[2])
                    else:
                        buf_rec = nx
                        break
                seg = _Seg(chunks)
                total = struct.unpack("<I", body[4:8])[0]
                for _ in range(total):
                    if seg.eof():
                        break
                    sst.append(seg.string())
            elif rid == 0x41E:
                fid, ln = struct.unpack("<HH", body[:2] + body[2:4])
                flags = body[4]
                formats[fid] = body[5:5 + ln * (2 if flags & 1 else 1)].decode("utf-16-le" if flags & 1 else "latin-1", "replace")
            elif rid == 0xE0:
                xf_fmt.append(struct.unpack("<H", body[2:4])[0])
            elif rid == 0x809 and struct.unpack("<H", body[2:4])[0] == 0x10:
                cur_sheet = sheets.get(off)
                rows.clear()
                header = None
            elif rid == 0x0A and cur_sheet is not None:  # EOF of worksheet
                for rn in sorted(rows):
                    rec = flush_row(rn)
                    if rec:
                        yield rec
                cur_sheet = None
            elif cur_sheet is not None:
                cells = self._cell(rid, body, sst, date_xf, date1904)
                if rid == 0x207 and last_formula:  # STRING record = نتيجة formula
                    rr, cc = last_formula
                    ln = struct.unpack("<H", body[:2])[0]
                    wide = body[2] & 1
                    rows.setdefault(rr, {})[cc] = body[3:3 + ln * (2 if wide else 1)].decode("utf-16-le" if wide else "latin-1", "replace")
                    last_formula = None
                for (rr, cc, val) in cells:
                    if rows and rr not in rows and len(rows) > 0:
                        for old in [k for k in rows if k < rr]:
                            rec = flush_row(old)
                            if rec:
                                yield rec
                    rows.setdefault(rr, {})[cc] = val
                    if rid == 0x06 and val is None:
                        last_formula = (rr, cc)

    def _cell(self, rid, body, sst, date_xf, d1904):
        def fmt_num(x: float, xf: int):
            if date_xf(xf):
                return excel_serial_to_iso(x + (1462 if d1904 else 0)) or _num(x)
            return _num(x)
        if rid == 0xFD:
            r, c, xf, idx = struct.unpack("<HHHI", body[:10])
            return [(r, c, sst[idx] if idx < len(sst) else None)]
        if rid == 0x203:
            r, c, xf = struct.unpack("<HHH", body[:6])
            return [(r, c, fmt_num(struct.unpack("<d", body[6:14])[0], xf))]
        if rid == 0x27E:
            r, c, xf, rk = struct.unpack("<HHHI", body[:10])
            return [(r, c, fmt_num(_rk(rk), xf))]
        if rid == 0xBD:
            r, c0 = struct.unpack("<HH", body[:4])
            out = []
            for i in range((len(body) - 6) // 6):
                xf, rk = struct.unpack("<HI", body[4 + 6 * i:10 + 6 * i])
                out.append((r, c0 + i, fmt_num(_rk(rk), xf)))
            return out
        if rid == 0x204:
            r, c, xf, ln = struct.unpack("<HHHH", body[:8])
            wide = body[8] & 1 if len(body) > 8 + ln and len(body) >= 9 + ln * 2 else 0
            return [(r, c, body[9:9 + ln * (2 if wide else 1)].decode("utf-16-le" if wide else "latin-1", "replace"))]
        if rid == 0x205:
            r, c, xf, v, is_err = struct.unpack("<HHHBB", body[:8])
            return [(r, c, None if is_err else ("TRUE" if v else "FALSE"))]
        if rid == 0x06:
            r, c, xf = struct.unpack("<HHH", body[:6])
            res = body[6:14]
            if res[6:8] == b"\xff\xff":
                t = res[0]
                if t == 1:
                    return [(r, c, "TRUE" if res[2] else "FALSE")]
                return [(r, c, None)]  # string (يأتي في STRING) او error/empty
            return [(r, c, fmt_num(struct.unpack("<d", res)[0], xf))]
        return []
