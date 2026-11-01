"""PDF text via optional `pdftotext` (poppler), with an OCR fallback
(tesseract + pdftoppm) for scanned PDFs that yield no text layer.
OCR text is partial by nature: searchable lines, not a faithful table."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

from ..errors import ParserError, ParserUnavailableError
from .base import Parser, Record
from .registry import register


@register
class PDFParser(Parser):
    name = "pdf"
    kinds = ("pdf",)

    def records(self, skip: int = 0) -> Iterator[Record]:
        tool = shutil.which("pdftotext")
        if not tool:
            raise ParserUnavailableError("Text extraction unavailable: الاداة pdftotext غير مثبتة (pkg install poppler)")
        # stderr الى ملف مؤقت: PDF تالف يطبع آلاف التحذيرات وPIPE ممتلئ يجمّد pdftotext للابد
        err_f = tempfile.TemporaryFile()
        proc = subprocess.Popen([tool, "-layout", "-enc", "UTF-8", str(self.path), "-"], stdout=subprocess.PIPE, stderr=err_f, text=True, errors="replace")
        page, n, produced = 1, 0, 0
        finished = False
        try:
            for raw in proc.stdout:
                parts = raw.split("\f")
                for i, seg in enumerate(parts):
                    if i > 0:
                        page += 1
                    line = seg.strip()
                    if not line:
                        continue
                    n += 1
                    produced += 1
                    if n > skip:
                        yield Record("main", f"page={page};line={n}", {"page": str(page), "text": line})
            finished = True
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            if not finished and proc.poll() is None:
                proc.kill()
            proc.wait()
            err_f.seek(0)
            err = err_f.read().decode("utf-8", "replace")
            err_f.close()
        if proc.returncode != 0:
            raise ParserError(f"pdftotext فشل: {err.strip()[:200]}")
        if produced == 0:
            yield from self._ocr_records(skip)

    def _ocr_records(self, skip: int = 0) -> Iterator[Record]:
        """OCR fallback: render pages and read them with tesseract."""
        import logging
        import os
        import tempfile

        log = logging.getLogger("pdb.pdf")
        tess = shutil.which("tesseract")
        ppm = shutil.which("pdftoppm")
        if not tess or not ppm:
            raise ParserError("Text extraction unavailable: لا يوجد نص قابل للاستخراج (PDF ممسوح ضوئياً؟). OCR غير مفعّل.")
        try:
            langs = subprocess.run([tess, "--list-langs"], capture_output=True, text=True, timeout=30).stdout
        except Exception:
            langs = ""
        lang = "ara+eng" if "ara" in langs else "eng"
        tmp = tempfile.mkdtemp(prefix="ocr_")
        try:
            subprocess.run([ppm, "-png", "-r", "150", str(self.path), os.path.join(tmp, "pg")],
                           capture_output=True, timeout=600, check=False)
            pages = sorted(Path(tmp).glob("pg-*.png"))
            if len(pages) > 100:
                raise ParserError("تجاوز حد صفحات OCR (100 صفحة).")
            n = 0
            for i, png in enumerate(pages, 1):
                try:
                    out = subprocess.run([tess, str(png), "stdout", "-l", lang, "--psm", "6"],
                                         capture_output=True, text=True, errors="replace", timeout=180)
                except subprocess.TimeoutExpired:
                    log.warning("OCR timeout page %s", i)
                    continue
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    if n > skip:
                        from .ocr_table import parse_ocr_line
                        kind, vals = parse_ocr_line(line)
                        if kind == "header":
                            fields = {"page": str(i), "ملاحظة": line}
                        elif kind == "data" and vals:
                            fields = {"page": str(i), **vals}
                        else:
                            fields = {"page": str(i), "text": line}
                        yield Record("main", f"page={i};line={n}", fields)
            if n == 0:
                raise ParserError("Text extraction unavailable: لا يوجد نص قابل للاستخراج (PDF ممسوح ضوئياً؟). OCR لم يجد نصاً.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
