"""Streaming .xlsx writer (stdlib only). Rows are written to temp files, never held in RAM;
a sheet that reaches Excel's row limit continues in a numbered sheet. Cells are inline strings/numbers
(never formulas), so spreadsheet formula-injection is not possible."""
from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

MAX_ROWS = 1_048_000
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class Sheet:
    def __init__(self, book: "XlsxBook", title: str, header: list[str], widths: list[int] | None) -> None:
        self.book, self.base, self.header, self.widths = book, title, header, widths
        self.part = 1
        self._open()

    def _open(self) -> None:
        self.rows = 0
        self.tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=self.book.tmpdir, suffix=".xml")
        name = self.base if self.part == 1 else f"{self.base[:27]} ({self.part})"
        self.book.sheets.append((name, self.tmp.name, self))
        self._row(self.header, style=1)

    def _row(self, values, style: int = 0) -> None:
        self.rows += 1
        cells = []
        for i, v in enumerate(values):
            ref = f"{_col(i)}{self.rows}"
            if v is None or v == "":
                continue
            if isinstance(v, bool):
                v = "TRUE" if v else "FALSE"
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{ref}" s="{style}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(_ILLEGAL.sub("", str(v))[:32000])}</t></is></c>')
        self.tmp.write(f'<row r="{self.rows}">{"".join(cells)}</row>')

    def write(self, values) -> None:
        if self.rows >= MAX_ROWS:
            self._close_part()
            self.part += 1
            self._open()
        self._row(values)

    def _close_part(self) -> None:
        self.tmp.close()

    def finish(self) -> None:
        self._close_part()


class XlsxBook:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.tmpdir = tempfile.mkdtemp(prefix="xlsx_", dir=self.path.parent)
        self.sheets: list[tuple[str, str, object]] = []
        self._sheet_objs: list[Sheet] = []

    def sheet(self, title: str, header: list[str], widths: list[int] | None = None) -> Sheet:
        s = Sheet(self, re.sub(r"[\[\]:*?/\\]", "_", title)[:31], header, widths)
        self._sheet_objs.append(s)
        return s

    def close(self) -> None:
        for s in self._sheet_objs:
            s.finish()
        names = []
        used = set()
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                       + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(self.sheets) + 1)) + "</Types>")
            z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            sheets_xml, rels = [], []
            for i, (name, tmp, _so) in enumerate(self.sheets, 1):
                nm = name
                k = 2
                while nm.lower() in used:
                    nm = f"{name[:28]}_{k}"; k += 1
                used.add(nm.lower())
                sheets_xml.append(f'<sheet name="{escape(nm)}" sheetId="{i}" r:id="rId{i}"/>')
                rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            n = len(self.sheets)
            z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + "".join(sheets_xml) + "</sheets></workbook>")
            z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            z.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/></cellXfs></styleSheet>')
            for i, (name, tmp, so) in enumerate(self.sheets, 1):
                widths = so.widths or [18] * len(so.header)
                cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{w}" customWidth="1"/>' for j, w in enumerate(widths))
                head = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0" rightToLeft="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                        f'<cols>{cols}</cols><sheetData>')
                tail = f'</sheetData><autoFilter ref="A1:{_col(len(so.header) - 1)}1"/></worksheet>'
                with z.open(f"xl/worksheets/sheet{i}.xml", "w", force_zip64=True) as out:
                    out.write(head.encode())
                    with open(tmp, "rb") as src:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                    out.write(tail.encode())
        for _, tmp, _so in self.sheets:
            Path(tmp).unlink(missing_ok=True)
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass
