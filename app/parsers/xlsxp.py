"""XLSX via zipfile + ElementTree.iterparse (streaming rows; shared strings held in memory)."""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Iterator

from ..errors import ParserError
from ..norm.dates import excel_serial_to_iso
from .base import Parser, Record
from .registry import register
from .tabular import sheet_header

_BUILTIN_DATE_FMTS = set(range(14, 23)) | {45, 46, 47}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _l(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _is_date_code(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code.lower())
    return bool(re.search(r"[dmyhs]", code))


@register
class XLSXParser(Parser):
    name = "xlsx"
    kinds = ("xlsx",)
    multi_dataset = True

    def _shared_strings(self, z: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in z.namelist():
            return []
        out: list[str] = []
        with z.open("xl/sharedStrings.xml") as f:
            for _, el in ET.iterparse(f, events=("end",)):
                if _l(el.tag) == "si":
                    txt = []
                    for ch in el:
                        t = _l(ch.tag)
                        if t == "t":
                            txt.append(ch.text or "")
                        elif t == "r":
                            txt.extend((x.text or "") for x in ch if _l(x.tag) == "t")
                    out.append("".join(txt))
                    el.clear()
        return out

    def _date_styles(self, z: zipfile.ZipFile) -> set[int]:
        if "xl/styles.xml" not in z.namelist():
            return set()
        custom: dict[int, str] = {}
        xfs: list[int] = []
        in_xfs = False
        with z.open("xl/styles.xml") as f:
            for ev, el in ET.iterparse(f, events=("start", "end")):
                t = _l(el.tag)
                if ev == "start" and t == "cellXfs":
                    in_xfs = True
                elif ev == "end":
                    if t == "numFmt":
                        custom[int(el.get("numFmtId"))] = el.get("formatCode", "")
                    elif t == "xf" and in_xfs:
                        xfs.append(int(el.get("numFmtId", "0")))
                    elif t == "cellXfs":
                        in_xfs = False
        return {i for i, fid in enumerate(xfs) if fid in _BUILTIN_DATE_FMTS or (fid in custom and _is_date_code(custom[fid]))}

    def _sheets(self, z: zipfile.ZipFile) -> list[tuple[str, str]]:
        wb = ET.parse(z.open("xl/workbook.xml")).getroot()
        rels = {r.get("Id"): r.get("Target") for r in ET.parse(z.open("xl/_rels/workbook.xml.rels")).getroot()}
        out = []
        for el in wb.iter():
            if _l(el.tag) == "sheet":
                target = rels.get(el.get(_REL), "")
                path = target.lstrip("/") if target.startswith("/") else "xl/" + target
                out.append((el.get("name", "sheet"), path))
        return out

    def records(self, skip: int = 0) -> Iterator[Record]:
        try:
            z = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise ParserError(f"XLSX تالف: {exc}") from exc
        n = 0
        with z:
            try:
                shared, date_styles, sheets = self._shared_strings(z), self._date_styles(z), self._sheets(z)
            except (KeyError, ET.ParseError) as exc:
                raise ParserError(f"بنية XLSX غير صالحة: {exc}") from exc
            for name, path in sheets:
                header: dict[int, str] | None = None
                sheet_data = None
                with z.open(path) as f:
                    for ev, el in ET.iterparse(f, events=("start", "end")):
                        tag = _l(el.tag)
                        if ev == "start":
                            if tag == "sheetData":
                                sheet_data = el
                            continue
                        if tag != "row":
                            continue
                        cells: dict[int, str | None] = {}
                        for c in el:
                            if _l(c.tag) != "c":
                                continue
                            idx = _col_index(c.get("r", "A1"))
                            t = c.get("t")
                            v = None
                            for ch in c:
                                ct = _l(ch.tag)
                                if ct == "v":
                                    v = ch.text
                                elif ct == "is":
                                    v = "".join((x.text or "") for x in ch.iter() if _l(x.tag) == "t")
                            if v is None:
                                continue
                            if t == "s":
                                v = shared[int(v)] if v.isdigit() and int(v) < len(shared) else None
                            elif t == "b":
                                v = "TRUE" if v == "1" else "FALSE"
                            elif t == "e":
                                v = None
                            elif t in (None, "n") and int(c.get("s", "0") or 0) in date_styles:
                                try:
                                    v = excel_serial_to_iso(float(v)) or v
                                except ValueError:
                                    pass
                            cells[idx] = v
                        rownum = el.get("r", "?")
                        if sheet_data is not None:
                            sheet_data.remove(el)
                        el.clear()
                        if not any(x not in (None, "") for x in cells.values()):
                            continue
                        if header is None:
                            header, is_hdr = sheet_header(cells)
                            if is_hdr:
                                continue
                            if len(self.warnings) < 20:
                                self.warnings.append(f"الورقة {name}: لم يُكتشف صف عناوين؛ استُخدمت col_N")
                        n += 1
                        if n <= skip:
                            continue
                        vals = {header.get(i, f"col_{i + 1}"): v for i, v in cells.items()}
                        yield Record(name, f"sheet={name};row={rownum}", vals)
