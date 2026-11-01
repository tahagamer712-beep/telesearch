"""Access (.mdb / .accdb) parser via mdbtools (mdb-tables + mdb-export).

Streams one Record per table row; each table is a dataset (multi_dataset),
so indexed field names become "<table>.<column>". Reads only through the
mdbtools CLI (no JET/ACE code of our own); a missing tool degrades to
"unsupported" via ParserUnavailableError, a corrupt/unreadable database to
a clear ParserError (never a silent half-index).
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from typing import Iterator

from ..errors import ParserError, ParserUnavailableError
from .base import Parser, Record
from .registry import register
from .tabular import _make_header

_DATE_ARGS: list[str] | None = None


def _date_args() -> list[str]:
    """تواريخ ISO من mdb-export (الافتراضي MM/DD/YY غير قابل للبحث). نفحص دعم الخيارات مرة وحدة."""
    global _DATE_ARGS
    if _DATE_ARGS is None:
        args: list[str] = []
        try:
            p = subprocess.run(["mdb-export", "--help"], capture_output=True, text=True, errors="replace", timeout=15)
            h = (p.stdout or "") + (p.stderr or "")
            if "-T" in h and "-D" in h:
                args = ["-D", "%Y-%m-%d", "-T", "%Y-%m-%d %H:%M:%S"]
            elif "-D" in h:
                args = ["-D", "%Y-%m-%d %H:%M:%S"]
        except Exception:
            args = []
        _DATE_ARGS = args
    return _DATE_ARGS


@register
class AccessParser(Parser):
    name = "access"
    kinds = ("accdb", "mdb")
    multi_dataset = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._procs: list[subprocess.Popen] = []

    def _tables(self) -> list[str]:
        if shutil.which("mdb-tables") is None:
            raise ParserUnavailableError("mdbtools غير مثبت (mdb-tables غير موجود)")
        try:
            p = subprocess.run(
                ["mdb-tables", "-1", str(self.path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParserError("mdb-tables انتهت مهلته (ملف كبير او تالف؟)") from exc
        if p.returncode != 0:
            raise ParserError(f"mdb-tables فشل: {p.stderr.strip()[:200]}")
        # -1 = one table per line; keep names verbatim (spaces inside names
        # are legal, e.g. "Order Details"), drop only blank lines.
        return [ln for ln in p.stdout.splitlines() if ln.strip()]

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=15)
        except Exception:
            pass

    def records(self, skip: int = 0) -> Iterator[Record]:
        if shutil.which("mdb-export") is None:
            raise ParserUnavailableError("mdbtools غير مثبت (mdb-export غير موجود)")
        tables = self._tables()
        if not tables:
            self.warnings.append("لم تُعثر على جداول بالملف")
            return
        env = dict(os.environ)
        env.setdefault("MDB_JET3_CHARSET", "cp1256")  # ملفات Access 97 العربية
        n = 0  # global row counter across tables (skip is global, order is mdb-tables order)
        for table in tables:
            # stderr الى ملف مؤقت وليس PIPE: تحذيرات mdbtools الكثيرة (>64KB) كانت تجمّد العملية للابد
            err_f = tempfile.TemporaryFile()
            try:
                proc = subprocess.Popen(
                    ["mdb-export", *_date_args(), str(self.path), table],
                    stdout=subprocess.PIPE, stderr=err_f,
                    text=True, encoding="utf-8", errors="replace", env=env,
                )
            except OSError as exc:
                err_f.close()
                raise ParserError(f"تعذر تشغيل mdb-export: {exc}") from exc
            self._procs.append(proc)
            finished = False
            try:
                assert proc.stdout is not None
                reader = csv.reader(proc.stdout)
                try:
                    header = next(reader)
                except StopIteration:
                    header = []
                header = _make_header(header)
                for ri, row in enumerate(reader, 1):
                    if n < skip:
                        n += 1
                        continue
                    values: dict[str, str | None] = {}
                    for i, col in enumerate(header):
                        v = row[i] if i < len(row) else None
                        values[col] = v if (v is not None and v != "") else None
                    yield Record(dataset=table, locator=f"{table}:{ri}", values=values)
                    n += 1
                finished = True
            finally:
                if not finished:
                    # stop/cancel/خطأ: اقتل العملية فوراً (قبل: ينتظر 300ث ثم يرمي خطأ مهلة كاذب)
                    self._kill(proc)
                    err_f.close()
            try:
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                err_f.close()
                raise ParserError(f"mdb-export انتهت مهلته للجدول {table}") from None
            if proc.returncode != 0:
                err_f.seek(0)
                err = err_f.read().decode("utf-8", "replace")
                err_f.close()
                raise ParserError(f"mdb-export فشل للجدول {table}: {err.strip()[-200:]}")
            err_f.close()

    def close(self) -> None:
        for proc in self._procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        self._procs.clear()
