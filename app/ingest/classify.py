"""File intelligence: magic bytes + content sniffing (extension is only a hint)."""
from __future__ import annotations

import codecs
import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

ARCHIVE_KINDS = {"zip", "7z", "rar", "gzip", "bzip2", "xz", "tar"}
DATA_KINDS = {"csv", "json", "jsonl", "sql", "sqlite", "xlsx", "xls", "txt", "pdf", "mdb", "accdb", "doc"}

MIME = {
    "zip": "application/zip", "7z": "application/x-7z-compressed", "rar": "application/vnd.rar",
    "gzip": "application/gzip", "bzip2": "application/x-bzip2", "xz": "application/x-xz",
    "tar": "application/x-tar", "sqlite": "application/vnd.sqlite3", "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xls": "application/vnd.ms-excel", "ole": "application/x-ole-storage",
    "csv": "text/csv", "json": "application/json", "jsonl": "application/x-ndjson",
    "sql": "application/sql", "txt": "text/plain", "unknown": "application/octet-stream",
    "mdb": "application/vnd.ms-access", "accdb": "application/vnd.ms-access", "doc": "application/msword",
}


@dataclass
class FileInfo:
    kind: str = "unknown"
    mime: str = "application/octet-stream"
    encoding: str | None = None
    encoding_confidence: str = "n/a"  # high | low | n/a
    delimiter: str | None = None
    ext_mismatch: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_archive(self) -> bool:
        return self.kind in ARCHIVE_KINDS

    @property
    def is_data(self) -> bool:
        return self.kind in DATA_KINDS


_JSON_START = re.compile(r'^(?:\{\s*(?:"|\})|\[\s*(?:\{|\[|"|\]|-?\d[\d.eE+\-]*\s*(?:,|\](?:\s*$|\s*[,\]\}]))|true\b|false\b|null\b))')


def _magic(head: bytes) -> str | None:
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")): return "zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"): return "7z"
    if head.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")): return "rar"
    if head.startswith(b"\x1f\x8b"): return "gzip"
    if head.startswith(b"BZh") and len(head) > 3 and head[3:4] in b"123456789": return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"): return "xz"
    if head.startswith(b"SQLite format 3\x00"): return "sqlite"
    if head.startswith(b"%PDF-"): return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"): return "ole"
    if head.startswith(b"\x00\x01\x00\x00Standard Jet DB\x00"): return "mdb"
    if head.startswith(b"\x00\x01\x00\x00Standard ACE DB\x00"): return "accdb"
    if len(head) >= 262 and head[257:262] == b"ustar": return "tar"
    return None


def detect_encoding(sample: bytes, truncated: bool) -> tuple[str | None, str]:
    """Return (encoding, confidence). Never silently 'fixes' data: a non-UTF-8
    result is reported as low confidence so callers can warn."""
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "high"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", "high"
    if b"\x00" in sample:
        return None, "n/a"
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        dec.decode(sample, final=not truncated)
        return "utf-8", "high"
    except UnicodeDecodeError:
        pass
    try:
        sample.decode("cp1256")
        return "cp1256", "low"
    except UnicodeDecodeError:
        return None, "n/a"


def _zip_kind(fp: BinaryIO) -> str:
    try:
        with zipfile.ZipFile(fp) as z:
            names = set(z.namelist())
        if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
            return "xlsx"
    except (zipfile.BadZipFile, OSError):
        pass
    return "zip"


def _sniff_text(sample: bytes, enc: str, truncated: bool) -> tuple[str, str | None]:
    text = sample.decode(enc, errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if truncated and lines:
        lines = lines[:-1] or lines
    stripped = text.lstrip("\ufeff \t\r\n")
    if re.search(r"(?im)^\s*(create\s+table|insert\s+into)\b", text):
        return "sql", None
    if len(lines) >= 2 and all(ln.lstrip().startswith("{") for ln in lines[:20]):
        objs = 0
        for ln in lines[:20]:
            try:
                if isinstance(json.loads(ln), dict):
                    objs += 1
            except ValueError:
                pass
        if objs >= max(2, int(0.8 * len(lines[:20]))):
            return "jsonl", None
    if stripped.startswith("{\\rtf"):
        return "txt", None  # RTF ليس JSON
    if stripped and stripped[0] in "[{" and _JSON_START.match(stripped):
        return "json", None
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:50]), delimiters=",;\t|")
        rows = list(csv.reader(io.StringIO("\n".join(lines[:50])), dialect))
        widths = {len(r) for r in rows}
        if len(rows) >= 2 and len(widths) <= 2 and max(widths) >= 2:
            return "csv", dialect.delimiter
    except csv.Error:
        pass
    # صفوف غير متساوية الطول (بيانات مخربطة): نختار الفاصل الذي يعطي >=2 عمود لاغلب الاسطر
    sample = lines[:50]
    for d in (",", "\t", ";", "|"):
        counts = [len(r) for r in csv.reader(io.StringIO("\n".join(sample)), delimiter=d)]
        if len(counts) >= 2 and sum(1 for c in counts if c >= 2) >= 0.8 * len(counts):
            return "csv", d
    return "txt", None


def classify(path: Path | None = None, fp: BinaryIO | None = None, name: str = "", sample_size: int = 65536) -> FileInfo:
    """Classify a file (or an already-open seekable stream)."""
    close = False
    if fp is None:
        fp = open(path, "rb")
        close = True
    try:
        fp.seek(0)
        head = fp.read(sample_size)
        truncated = len(head) == sample_size
        info = FileInfo()
        kind = _magic(head)
        if kind is None and len(head) >= 262 and head[257:262] == b"ustar":
            kind = "tar"
        if kind == "ole":
            from ..parsers.cfb import has_workbook, has_word_document
            if path and has_workbook(path):
                kind = "xls"
            elif path and has_word_document(path):
                kind = "doc"
            else:
                kind = "unknown"
                info.notes.append("ملف OLE2 غير مدعوم (ليس Excel ولا Word)")
        if kind == "zip":
            fp.seek(0)
            kind = _zip_kind(fp)
        if kind is None:
            enc, conf = detect_encoding(head, truncated)
            if enc is None:
                kind = "unknown"
                info.notes.append("محتوى ثنائي غير معروف")
            else:
                info.encoding, info.encoding_confidence = enc, conf
                py_enc = "utf-8" if enc == "utf-8-sig" else enc
                kind, info.delimiter = _sniff_text(head.lstrip(codecs.BOM_UTF8) if enc == "utf-8-sig" else head, py_enc, truncated)
                if conf == "low":
                    info.notes.append(f"الترميز غير مؤكد ({enc}) - راجع النتائج")
        ext = Path(name or (path.name if path else "")).suffix.lower().lstrip(".")
        # Audit fix (Phase 5 A13): a header-only .csv/.tsv has a single line,
        # so the text sniffer falls through to 'txt' and the TXT parser turns
        # the header itself into 1 bogus record. A one-line delimited file is
        # a header with zero data rows — the CSV parser yields exactly that.
        if kind == "txt" and ext in ("csv", "tsv") and not truncated:
            _one = [ln for ln in head.decode(enc if enc not in (None, "utf-8-sig") else "utf-8", errors="replace").splitlines() if ln.strip()]
            if len(_one) == 1:
                kind = "csv"
                info.delimiter = "," if "," in _one[0] else ("\t" if "\t" in _one[0] else (";" if ";" in _one[0] else ","))
        # Audit fix (Phase 5 A16): ASCII garbage named .xlsx/.xls used to be
        # sniffed as 'txt' and indexed as data. A spreadsheet extension with
        # unstructured-text content is a corrupt/misnamed file, not data.
        if kind == "txt" and ext in ("xlsx", "xls"):
            kind = "unknown"
            info.notes.append(f"الملف بامتداد .{ext} لكن محتواه نص غير منظم وليس ملف Excel — مرفوض")
        info.kind, info.mime = kind, MIME.get(kind, MIME["unknown"])
        expected = {"csv": {"csv", "tsv"}, "json": {"json"}, "jsonl": {"jsonl", "ndjson", "json"}, "sql": {"sql"},
                    "sqlite": {"sqlite", "sqlite3", "db"}, "xlsx": {"xlsx"}, "xls": {"xls"}, "pdf": {"pdf"}, "zip": {"zip"},
                    "7z": {"7z"}, "rar": {"rar"}, "gzip": {"gz", "tgz"}, "bzip2": {"bz2", "tbz2"}, "xz": {"xz", "txz"},
                    "tar": {"tar"}, "mdb": {"mdb"}, "accdb": {"accdb"}, "doc": {"doc"}}.get(kind)
        if expected and ext and ext not in expected and not ext.isdigit():
            info.ext_mismatch = True
            info.notes.append(f"الامتداد .{ext} لا يطابق المحتوى الفعلي ({kind})")
        return info
    finally:
        if close:
            fp.close()


_PART_RE = re.compile(r"^(?P<base>.+)\.(?P<n>\d{3})$")


def find_multipart_group(path: Path) -> list[Path] | None:
    """If `path` looks like NAME.001, return the contiguous list of sibling parts
    (001..N) or None. Completeness of the LAST part is only provable later, when
    the archive stream decodes successfully."""
    m = _PART_RE.match(path.name)
    if not m or m["n"] != "001":
        return None
    base = m["base"]
    parts = [path]
    n = 2
    while True:
        nxt = path.with_name(f"{base}.{n:03d}")
        if not nxt.is_file():
            break
        parts.append(nxt)
        n += 1
    return parts


def find_gap(path: Path) -> list[int]:
    """Numbers of missing parts between 001 and the highest existing part."""
    m = _PART_RE.match(path.name)
    if not m:
        return []
    base = m["base"]
    existing = sorted(int(p.name.rsplit(".", 1)[1]) for p in path.parent.glob(glob_escape(base) + ".[0-9][0-9][0-9]"))
    if not existing:
        return []
    return [i for i in range(1, existing[-1] + 1) if i not in existing]


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", s)
