"""File intake: hash -> classify -> move into originals/ -> register in DB."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..errors import IngestError
from ..storage import StorageManager, sanitize_filename
from .chain import ChainReader
from .classify import FileInfo, classify, find_multipart_group
from .hashing import sha256_file, sha256_stream

log = logging.getLogger("pdb.intake")


@dataclass
class IntakeResult:
    file_id: int | None
    status: str  # registered | duplicate
    sha256: str
    size: int
    name: str
    info: FileInfo | None = None
    duplicate_of: int | None = None


def find_duplicate(conn: sqlite3.Connection, sha256: str, size: int, scope: str = "main") -> int | None:
    row = conn.execute(
        "SELECT id FROM files WHERE sha256=? AND size_bytes=? AND parent_file_id IS NULL "
        "AND status NOT IN ('failed') AND scope=? ORDER BY id LIMIT 1", (sha256, size, scope)).fetchone()
    return row["id"] if row else None


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)  # نفس الملف-سيستم = فوري حتى لملف 4GB
    except OSError:
        shutil.move(str(src), str(dst))


def _insert_file(conn, *, name, stored, sha, size, info: FileInfo, parent_id=None, source_path=None, scope: str = "main") -> int:
    warnings = json.dumps(info.notes, ensure_ascii=False) if info.notes else None
    status = "unsupported" if info.kind == "unknown" else "received"
    cur = conn.execute(
        "INSERT INTO files(parent_file_id, original_name, stored_path, sha256, size_bytes, detected_type, mime, status, warnings, source_path, scope) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (parent_id, name, str(stored), sha, size, info.kind, info.mime, status, warnings, source_path, scope))
    return cur.lastrowid


def register_file(conn, storage: StorageManager, src: Path, original_name: str | None = None,
                  chunk: int = 1 << 20, allow_duplicate: bool = False, progress=None, scope: str = "main") -> IntakeResult:
    """Register one file. Original is MOVED (not modified) into originals/<sha[:2]>/."""
    src = Path(src)
    if not src.is_file():
        raise IngestError(f"الملف غير موجود: {src}")
    # Audit fix (Phase 5 A33): the stored name is '{sha}_' (65 bytes) + name,
    # so the name budget is 255 - 65 = 190 bytes (sanitize counts bytes now).
    name = sanitize_filename(original_name or src.name, max_len=190)
    sha, size = sha256_file(src, chunk, progress)
    dup = find_duplicate(conn, sha, size, scope)
    if dup and not allow_duplicate:
        return IntakeResult(None, "duplicate", sha, size, name, duplicate_of=dup)
    info = classify(src, name=name)
    dst = storage.dir("originals") / sha[:2] / f"{sha}_{name}"
    if dst.exists():
        _extra = f"{sha}_{os.urandom(3).hex()}_"
        _nb = name.encode("utf-8")[:255 - len(_extra)].decode("utf-8", errors="ignore")
        dst = dst.with_name(_extra + _nb)
    try:
        _move(src, dst)
    except OSError as exc:
        # A33 defense: a filesystem failure while storing must surface as a
        # clean intake error, never an uncaught crash out of ingest_path.
        raise IngestError(f"تعذر حفظ الملف الأصلي: {exc}") from exc
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    fid = _insert_file(conn, name=name, stored=dst, sha=sha, size=size, info=info, source_path=str(src), scope=scope)
    log.info("registered file id=%s kind=%s size=%s", fid, info.kind, size)
    return IntakeResult(fid, "registered", sha, size, name, info, dup)


def register_multipart(conn, storage: StorageManager, parts: list[Path], allow_duplicate: bool = False, scope: str = "main") -> IntakeResult:
    """Register NAME.001..NNN as one logical archive. Hash is over the joined stream."""
    parts = [Path(p) for p in parts]
    base = parts[0].name.rsplit(".", 1)[0]
    name = sanitize_filename(base)
    with ChainReader(parts) as chain:
        sha, size = sha256_stream(chain)
        chain.seek(0)
        info = classify(fp=chain, name=name)
    dup = find_duplicate(conn, sha, size, scope)
    if dup and not allow_duplicate:
        return IntakeResult(None, "duplicate", sha, size, name, duplicate_of=dup)
    if not info.is_archive:
        # Audit fix (Phase 5 A27b): the old note implied the parts would work
        # as one file; they cannot be indexed (pipeline rejects non-archive
        # part lists). Say so plainly instead of promising.
        info.notes.append("الأجزاء لا تشكل أرشيفاً معروفاً؛ لن تُفهرس كملف واحد — ادمج الأجزاء في ملف واحد وأعد إرساله (التجزئة .001 مدعومة للأرشيفات فقط)")
    dest_dir = storage.dir("originals") / sha[:2] / f"{sha}_parts"
    stored_parts = []
    for p in parts:
        dst = dest_dir / sanitize_filename(p.name)
        _move(p, dst)
        stored_parts.append(dst)
    fid = _insert_file(conn, name=name, stored=stored_parts[0], sha=sha, size=size, info=info, source_path=str(parts[0]), scope=scope)
    for i, sp in enumerate(stored_parts, 1):
        conn.execute("INSERT INTO file_parts(file_id, part_index, path, size_bytes) VALUES (?,?,?,?)",
                     (fid, i, str(sp), sp.stat().st_size))
    log.info("registered multipart file id=%s parts=%s size=%s", fid, len(parts), size)
    return IntakeResult(fid, "registered", sha, size, name, info, dup)


def register_extracted(conn, path: Path, member_name: str, parent_id: int, chunk: int = 1 << 20, scope: str | None = None) -> int:
    """Register a file extracted from an archive (stays where extraction put it).
    Scope is inherited from the parent archive when not given: an archive's
    members always live in the parent's scope, never leak across scopes."""
    sha, size = sha256_file(path, chunk)
    info = classify(path, name=member_name)
    if scope is None:
        prow = conn.execute("SELECT scope FROM files WHERE id=?", (parent_id,)).fetchone()
        scope = prow["scope"] if prow and prow["scope"] else "main"
    return _insert_file(conn, name=member_name, stored=path, sha=sha, size=size, info=info, parent_id=parent_id, scope=scope)


def stored_source(conn, file_id: int):
    """Return (path_or_parts, row). parts list is returned when file has file_parts."""
    row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise IngestError(f"file {file_id} غير موجود")
    parts = [Path(r["path"]) for r in conn.execute("SELECT path FROM file_parts WHERE file_id=? ORDER BY part_index", (file_id,))]
    return (parts if parts else Path(row["stored_path"])), row


def check_source_changed(conn, file_id: int) -> tuple[bool, str, str]:
    """Re-hash the stored original; returns (changed, original_sha, current_sha)."""
    src, row = stored_source(conn, file_id)
    if isinstance(src, list):
        with ChainReader(src) as ch:
            cur, _ = sha256_stream(ch)
    else:
        cur, _ = sha256_file(src)
    return cur != row["sha256"], row["sha256"], cur
