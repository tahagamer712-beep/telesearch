"""Safe archive extraction. stdlib for zip/tar/gz/bz2/xz; external tools (optional)
for 7z/rar. Every write stays inside `dest`; symlinks/devices are never created."""
from __future__ import annotations

import bz2
import errno
import gzip
import io
import logging
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import time
import zlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Config
from ..errors import ArchiveError, InsufficientSpaceError, JobInterrupted, ToolUnavailableError
from ..ingest.chain import ChainReader
from ..storage import sanitize_filename

log = logging.getLogger("pdb.archive")
_EXEC_EXT = {".exe", ".dll", ".bat", ".cmd", ".sh", ".apk", ".com", ".scr", ".msi", ".jar", ".ps1", ".vbs", ".so"}
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")
GB = 1024 ** 3


@dataclass
class ExtractedFile:
    rel: str  # الاسم الاصلي داخل الارشيف (provenance)
    path: Path
    size: int


@dataclass
class ExtractResult:
    files: list[ExtractedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suspicious: bool = False


def split_member_path(name: str) -> list[str] | None:
    """Return safe path components, or None if the name is unsafe."""
    if "\x00" in name:
        return None
    n = name.replace("\\", "/")
    if n.startswith("/") or _WIN_DRIVE.match(n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return parts


class _Budget:
    def __init__(self, config: Config, compressed: int, dest: Path, stop: Callable | None) -> None:
        self.cfg, self.compressed, self.dest, self.stop = config, max(compressed, 1), dest, stop
        self.files = 0
        self.bytes = 0
        self._next_check = 0

    def add_file(self) -> None:
        self.files += 1
        if self.files > self.cfg.max_archive_files:
            raise ArchiveError(f"عدد الملفات داخل الارشيف تجاوز الحد ({self.cfg.max_archive_files})")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.stop:
            self.stop()
        cap = self.cfg.max_extract_gb * GB
        if cap and self.bytes > cap:
            raise ArchiveError(f"حجم المستخرج تجاوز MAX_EXTRACT_GB={self.cfg.max_extract_gb}")
        if self.bytes > self.cfg.bomb_min_mb * 1024 * 1024 and self.bytes / self.compressed > self.cfg.max_extract_ratio:
            raise ArchiveError("نسبة الضغط غير طبيعية (archive bomb محتمل) - تم الايقاف")
        if self.bytes >= self._next_check:
            self._next_check = self.bytes + 64 * 1024 * 1024
            free = shutil.disk_usage(self.dest).free
            if free < self.cfg.min_free_storage_gb * GB:
                raise InsufficientSpaceError(self.bytes, free)


class ArchiveManager:
    def __init__(self, config: Config) -> None:
        self.cfg = config

    # ---------- tools
    @staticmethod
    def tools() -> dict[str, str | None]:
        w = shutil.which
        return {"7z": w("7z") or w("7za") or w("7zr"), "unrar": w("unrar") or w("unrar-free"), "pdftotext": w("pdftotext"), "openssl": w("openssl")}

    def supported_kinds(self) -> dict[str, bool]:
        t = self.tools()
        return {"zip": True, "tar": True, "gzip": True, "bzip2": True, "xz": True,
                "7z": bool(t["7z"]), "rar": bool(t["7z"] or t["unrar"])}

    # ---------- space
    def require_space(self, required: int, where: Path) -> None:
        free = shutil.disk_usage(where).free
        avail = free - int(self.cfg.min_free_storage_gb * GB)
        if required > avail:
            raise InsufficientSpaceError(required, max(avail, 0))

    # ---------- public API
    def extract(self, source: Path | list[Path], kind: str, dest: Path, stop: Callable | None = None) -> ExtractResult:
        dest.mkdir(parents=True, exist_ok=True)
        parts = source if isinstance(source, list) else None
        first = parts[0] if parts else Path(source)
        compressed = sum(p.stat().st_size for p in parts) if parts else first.stat().st_size
        budget = _Budget(self.cfg, compressed, dest, stop)
        res = ExtractResult()
        if kind in ("7z", "rar"):
            self._extract_external(kind, first, dest, res, budget, compressed)
            return res
        fp = ChainReader(parts) if parts else open(first, "rb")
        try:
            if kind == "zip":
                self._extract_zip(fp, dest, res, budget)
            elif kind == "tar":
                self._extract_tar(fp, "r|", dest, res, budget)
            elif kind in ("gzip", "bzip2", "xz"):
                self._extract_compressed(kind, fp, first.name if not parts else first.name.rsplit(".", 1)[0], dest, res, budget)
            else:
                raise ArchiveError(f"صيغة ارشيف غير مدعومة: {kind}")
        finally:
            fp.close()
        return res

    # ---------- helpers
    def _target(self, dest: Path, parts: list[str]) -> Path:
        clean = [sanitize_filename(p) for p in parts]
        target = dest.joinpath(*clean)
        base = target
        i = 1
        while target.exists():
            target = base.with_name(f"{base.stem}__{i}{base.suffix}")
            i += 1
        resolved = target.resolve()
        if not resolved.is_relative_to(dest.resolve()):
            raise ArchiveError("مسار خارج مجلد الاستخراج")
        return target

    def _write(self, src, target: Path, rel: str, res: ExtractResult, budget: _Budget) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        budget.add_file()
        size = 0
        try:
            with open(target, "wb") as out:
                while True:
                    block = src.read(self.cfg.chunk_size)
                    if not block:
                        break
                    out.write(block)
                    size += len(block)
                    budget.add_bytes(len(block))
            os.chmod(target, 0o600)
        except (zipfile.BadZipFile, EOFError, OSError, lzma.LZMAError, tarfile.TarError, zlib.error) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise InsufficientSpaceError(budget.bytes, 0) from exc
            target.unlink(missing_ok=True)
            res.skipped.append((rel, f"تالف/غير مكتمل: {exc}"))
            return
        if Path(rel).suffix.lower() in _EXEC_EXT:
            res.warnings.append(f"ملف تنفيذي داخل الارشيف (لم يُشغَّل): {rel}")
        res.files.append(ExtractedFile(rel, target, size))

    # ---------- zip
    def _extract_zip(self, fp, dest: Path, res: ExtractResult, budget: _Budget) -> None:
        try:
            z = zipfile.ZipFile(fp)
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"ملف zip تالف او غير مكتمل: {exc}") from exc
        with z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos if not i.is_dir())
            self.require_space(total, dest)
            if len([i for i in infos if not i.is_dir()]) > self.cfg.max_archive_files:
                raise ArchiveError("عدد الملفات داخل الارشيف تجاوز الحد")
            for info in infos:
                if info.is_dir():
                    continue
                parts = split_member_path(info.filename)
                if parts is None:
                    res.skipped.append((info.filename, "مسار غير آمن (path traversal)"))
                    res.suspicious = True
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    res.skipped.append((info.filename, "symlink مرفوض"))
                    res.suspicious = True
                    continue
                if info.flag_bits & 0x1:
                    res.skipped.append((info.filename, "مشفّر بكلمة مرور"))
                    continue
                try:
                    with z.open(info) as member:
                        self._write(member, self._target(dest, parts), info.filename, res, budget)
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
                    res.skipped.append((info.filename, f"تعذر القراءة: {exc}"))

    # ---------- tar
    def _extract_tar(self, fp, mode: str, dest: Path, res: ExtractResult, budget: _Budget) -> None:
        try:
            tf = tarfile.open(fileobj=fp, mode=mode)
        except tarfile.TarError as exc:
            raise ArchiveError(f"ملف tar تالف: {exc}") from exc
        with tf:
            try:
                for m in tf:
                    if m.isdir():
                        continue
                    if not m.isfile():
                        res.skipped.append((m.name, "نوع غير مسموح (link/device)"))
                        res.suspicious = True
                        continue
                    parts = split_member_path(m.name)
                    if parts is None:
                        res.skipped.append((m.name, "مسار غير آمن (path traversal)"))
                        res.suspicious = True
                        continue
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    self._write(src, self._target(dest, parts), m.name, res, budget)
            except (tarfile.TarError, EOFError, lzma.LZMAError, OSError) as exc:
                if isinstance(exc, InsufficientSpaceError):
                    raise
                raise ArchiveError(f"الارشيف تالف او مقطوع (ناقص؟): {exc}") from exc

    # ---------- gz / bz2 / xz
    def _extract_compressed(self, kind: str, fp, name: str, dest: Path, res: ExtractResult, budget: _Budget) -> None:
        dec = {"gzip": lambda f: gzip.GzipFile(fileobj=f), "bzip2": lambda f: bz2.BZ2File(f), "xz": lambda f: lzma.LZMAFile(f)}[kind](fp)
        try:
            head = dec.peek(512)[:512]
        except (OSError, EOFError, lzma.LZMAError) as exc:
            raise ArchiveError(f"ملف مضغوط تالف: {exc}") from exc
        if len(head) >= 262 and head[257:262] == b"ustar":
            self._extract_tar(dec, "r|", dest, res, budget)
            return
        out_name = re.sub(r"\.(gz|bz2|xz)$", "", name, flags=re.I) or "decompressed"
        try:
            self._write(dec, self._target(dest, [out_name]), out_name, res, budget)
        except (OSError, EOFError, lzma.LZMAError) as exc:
            raise ArchiveError(f"ملف مضغوط تالف او مقطوع: {exc}") from exc
        if res.skipped and not res.files:
            raise ArchiveError("فشل فك الضغط: " + res.skipped[0][1])

    # ---------- 7z / rar via external tool
    def _extract_external(self, kind: str, src: Path, dest: Path, res: ExtractResult, budget: _Budget, compressed: int) -> None:
        t = self.tools()
        seven = t["7z"]
        if kind == "7z" and not seven:
            raise ToolUnavailableError("صيغة 7z تحتاج الاداة 7z (pkg install p7zip). الملف محفوظ كما هو.")
        if kind == "rar" and not (seven or t["unrar"]):
            raise ToolUnavailableError("صيغة rar تحتاج 7z او unrar (pkg install p7zip unrar). الملف محفوظ كما هو.")
        total = None
        if seven:
            proc = subprocess.run([seven, "l", "-slt", "-ba", "-y", str(src)], capture_output=True, text=True, errors="replace", timeout=600, stdin=subprocess.DEVNULL)
            if proc.returncode == 0:
                total = self._check_listing(proc.stdout, res)
            elif kind == "7z":
                raise ArchiveError("تعذر قراءة الارشيف (تالف او محمي بكلمة مرور او ناقص الاجزاء)")
        if total is not None:
            self.require_space(total, dest)
        # 7z extracts rar too: prefer it whenever present. The old condition fell
        # through to unrar (None when uninstalled) for any rar whose listing failed,
        # raising TypeError instead of a clean ArchiveError.
        if seven:
            cmd = [seven, "x", "-y", "-bd", f"-o{dest}", str(src)]
        elif t["unrar"]:
            cmd = [t["unrar"], "x", "-y", "-idq", str(src), str(dest) + os.sep]
        else:
            raise ToolUnavailableError("صيغة rar تحتاج 7z او unrar (pkg install p7zip unrar). الملف محفوظ كما هو.")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace")
        try:
            while True:
                try:
                    out, _ = proc.communicate(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    budget.bytes = self._dir_size(dest)
                    budget.add_bytes(0)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode not in (0, 1):  # 1 = warnings في 7z
            raise ArchiveError("فشل الاستخراج: " + (out or "")[-300:].strip())
        self._collect(dest, res, budget)

    def _check_listing(self, text: str, res: ExtractResult) -> int:
        total = 0
        for block in text.split("\n\n"):
            fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
            if "Path" not in fields:
                continue
            if fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
                continue
            if split_member_path(fields["Path"]) is None:
                raise ArchiveError(f"ارشيف مشبوه: مسار غير آمن ({fields['Path'][:80]})")
            try:
                total += int(fields.get("Size", "0") or 0)
            except ValueError:
                pass
        return total

    @staticmethod
    def _dir_size(d: Path) -> int:
        n = 0
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    n += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return n

    def _collect(self, dest: Path, res: ExtractResult, budget: _Budget) -> None:
        droot = dest.resolve()
        for root, dirs, files in os.walk(dest, followlinks=False):
            for name in list(dirs) + files:
                p = Path(root) / name
                if p.is_symlink():
                    p.unlink()
                    res.skipped.append((str(p.relative_to(dest)), "symlink حُذف"))
                    res.suspicious = True
            for name in files:
                p = Path(root) / name
                if not p.exists():
                    continue
                if not p.is_file():
                    # fifo / device / socket planted by external extraction:
                    # never hand a non-regular file to the ingest chain.
                    p.unlink()
                    res.suspicious = True
                    continue
                if not p.resolve().is_relative_to(droot):
                    p.unlink()
                    res.suspicious = True
                    continue
                budget.add_file()
                os.chmod(p, 0o600)
                rel = str(p.relative_to(dest))
                if p.suffix.lower() in _EXEC_EXT:
                    res.warnings.append(f"ملف تنفيذي داخل الارشيف (لم يُشغَّل): {rel}")
                res.files.append(ExtractedFile(rel, p, p.stat().st_size))
