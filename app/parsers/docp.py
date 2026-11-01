"""Legacy Word (.doc, OLE2 Word 97-2003) parser via antiword.

antiword extracts plain text (paragraphs + tables as pipe rows). Each
blank-line-separated block becomes one Record ("main" dataset, "para N"
locator, internal newlines joined so wrapped paragraphs stay searchable
as one unit). A missing tool degrades to "unsupported" via
ParserUnavailableError; an unreadable/encrypted/corrupt document to a
clear ParserError (never a silent half-index).

Limitations (by tool, not by us): embedded images/objects are ignored
(text only); password-protected .doc files fail with a clear error;
.docx (OOXML zip) is NOT this parser -- it classifies as a zip archive.
Note: antiword is not streaming -- it holds the whole document text in
memory. Fine for typical .doc sizes (KBs to a few MB); a very large .doc
would spike RAM the same way any whole-document tool does.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Iterator

from ..errors import ParserError, ParserUnavailableError
from .base import Parser, Record
from .registry import register


@register
class DocParser(Parser):
    name = "doc"
    kinds = ("doc",)
    multi_dataset = False

    def records(self, skip: int = 0) -> Iterator[Record]:
        if shutil.which("antiword") is None:
            raise ParserUnavailableError("antiword غير مثبت")
        try:
            p = subprocess.run(
                ["antiword", str(self.path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParserError("antiword انتهت مهلته (ملف كبير او تالف؟)") from exc
        if p.returncode != 0:
            raise ParserError(f"antiword فشل: {p.stderr.strip()[:200]}")
        text = p.stdout or ""
        if not text.strip():
            raise ParserError("antiword لم يستخرج اي نص (ملف فارغ او مشفر او تالف)")
        blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
        n = 0
        for i, block in enumerate(blocks, 1):
            joined = " ".join(ln.strip() for ln in block.splitlines() if ln.strip())
            if not joined:
                continue
            if n < skip:
                n += 1
                continue
            yield Record(dataset="main", locator=f"para {i}", values={"text": joined})
            n += 1
