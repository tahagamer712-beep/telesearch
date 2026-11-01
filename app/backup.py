"""Backup / restore: DB (online, consistent) + metadata + redacted config, optionally AES-256 encrypted
via the `openssl` CLI (stdlib has no cipher). Original raw files are NOT included (they are already on disk)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .config import Config
from .db.connection import connect
from .errors import BackupError

_ALLOWED = {"database.sqlite3", "metadata.json", "config.json", "manifest.json"}
MANIFEST_VERSION = 1


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _redacted_config(cfg: Config) -> dict:
    d = {k: (str(v) if isinstance(v, (Path, frozenset)) else v) for k, v in cfg.__dict__.items()}
    for k in ("telegram_token", "openrouter_api_key", "backup_passphrase"):
        d[k] = "***" if d.get(k) else ""
    d["authorized_user_ids"] = sorted(cfg.authorized_user_ids)
    return d


def _openssl(cfg: Config, decrypt: bool, src: Path, dst: Path, passphrase: str) -> None:
    exe = shutil.which("openssl")
    if not exe:
        raise BackupError("openssl غير مثبت (pkg install openssl-tool). لا يمكن التشفير/فك التشفير.")
    cmd = [exe, "enc", "-d" if decrypt else "-e", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-in", str(src), "-out", str(dst), "-pass", "env:PDB_BACKUP_PASS"]
    r = subprocess.run(cmd, env={**os.environ, "PDB_BACKUP_PASS": passphrase}, capture_output=True, text=True)
    if r.returncode != 0:
        dst.unlink(missing_ok=True)
        raise BackupError("فشل التشفير/فك التشفير (كلمة المرور خاطئة او الملف تالف)")


def create_backup(cfg: Config, conn: sqlite3.Connection) -> Path:
    if not cfg.backup_passphrase and not cfg.backup_allow_unencrypted:
        raise BackupError("BACKUP_PASSPHRASE غير مضبوط. النسخ الاحتياطية تحتوي بيانات حساسة؛ اضبط كلمة مرور او فعّل BACKUP_ALLOW_UNENCRYPTED=true بوعي.")
    bdir = cfg.base_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    with tempfile.TemporaryDirectory(dir=cfg.base_dir / "data" / "temporary") as tmp:
        tmp = Path(tmp)
        dbcopy = tmp / "database.sqlite3"
        dst = sqlite3.connect(dbcopy)
        try:
            conn.backup(dst, pages=2000, sleep=0.005)
            if dst.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise BackupError("النسخة الاحتياطية فشلت في فحص السلامة")
        finally:
            # Audit fix (Phase 9/16): the old code closed only on the happy
            # path — a failed quick_check leaked the connection.
            dst.close()
        meta = [dict(r) for r in conn.execute("SELECT id, parent_file_id, original_name, sha256, size_bytes, detected_type, status, record_count, received_at, indexed_at FROM files")]
        (tmp / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False))
        users = [dict(r) for r in conn.execute("SELECT telegram_id, role, enabled FROM users")]
        (tmp / "config.json").write_text(json.dumps({"config": _redacted_config(cfg), "users": users}, ensure_ascii=False, default=str))
        from .db.migrator import MIGRATIONS_DIR, current_version
        manifest = {"manifest_version": MANIFEST_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "schema_version": current_version(conn),
                    "known_migrations": len(list(MIGRATIONS_DIR.glob("*.sql"))), "sha256": {n: _sha(tmp / n) for n in ("database.sqlite3", "metadata.json", "config.json")}}
        (tmp / "manifest.json").write_text(json.dumps(manifest))
        tar_path = tmp / "bundle.tar.gz"
        with tarfile.open(tar_path, "w:gz", compresslevel=1) as t:
            for n in sorted(_ALLOWED):
                t.add(tmp / n, arcname=n)
        out = bdir / (f"backup_{ts}.tar.gz.enc" if cfg.backup_passphrase else f"backup_{ts}.tar.gz")
        if cfg.backup_passphrase:
            _openssl(cfg, False, tar_path, out, cfg.backup_passphrase)
        else:
            shutil.move(str(tar_path), out)
    os.chmod(out, 0o600)
    h = hashlib.sha256()
    with open(out, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    Path(str(out) + ".sha256").write_text(h.hexdigest())
    for old in sorted(bdir.glob("backup_*"))[: -2 * cfg.backup_keep]:  # كل نسخة لها ملف .sha256
        old.unlink(missing_ok=True)
    return out


def restore_backup(cfg: Config, backup: Path, passphrase: str = "") -> Path:
    """Restore database from a backup. Current DB is preserved as pre_restore_*.sqlite3."""
    backup = Path(backup)
    pid = cfg.base_dir / "run" / "app.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 0)
            raise BackupError("النظام يعمل حالياً. شغّل ./stop.sh قبل الاستعادة.")
        except (ProcessLookupError, ValueError):
            pass
        except PermissionError:
            raise BackupError("النظام يعمل حالياً.")
    sidecar = Path(str(backup) + ".sha256")
    if sidecar.exists():
        h = hashlib.sha256()
        with open(backup, "rb") as f:
            while b := f.read(1 << 20):
                h.update(b)
        if h.hexdigest() != sidecar.read_text().strip():
            raise BackupError("ملف النسخة الاحتياطية تالف (SHA-256 لا يطابق)")
    with tempfile.TemporaryDirectory(dir=cfg.base_dir / "data" / "temporary") as tmp:
        tmp = Path(tmp)
        tarp = backup
        if backup.suffix == ".enc":
            tarp = tmp / "bundle.tar.gz"
            _openssl(cfg, True, backup, tarp, passphrase or cfg.backup_passphrase)
        with tarfile.open(tarp, "r:gz") as t:
            for m in t:
                if m.name not in _ALLOWED or not m.isfile():
                    raise BackupError(f"عنصر غير متوقع داخل النسخة: {m.name}")
                t.extract(m, tmp, filter="data") if hasattr(tarfile, "data_filter") else t.extract(m, tmp)
        newdb = tmp / "database.sqlite3"
        try:
            man = json.loads((tmp / "manifest.json").read_text())
        except (OSError, ValueError):
            raise BackupError("manifest.json مفقود/تالف: رُفضت الاستعادة")
        from .db.migrator import MIGRATIONS_DIR
        if man.get("manifest_version") != MANIFEST_VERSION or man.get("schema_version", 0) > len(list(MIGRATIONS_DIR.glob("*.sql"))):
            raise BackupError("النسخة من اصدار اجدد/غير متوافق: رُفضت الاستعادة")
        for n, h in man.get("sha256", {}).items():
            if not (tmp / n).exists() or _sha(tmp / n) != h:
                raise BackupError(f"SHA-256 غير مطابق للعنصر {n}: النسخة تالفة، رُفضت الاستعادة")
        c = sqlite3.connect(newdb)
        ok = c.execute("PRAGMA quick_check(1)").fetchone()[0]
        c.close()
        if ok != "ok":
            raise BackupError("قاعدة البيانات داخل النسخة تالفة")
        cur = cfg.db_path
        if cur.exists():
            # Audit fix (Phase 5 E04b / Phase 6 11.8): the DB runs in WAL
            # mode, so copying only main.sqlite3 dropped committed
            # transactions still living in -wal — and the -wal was then
            # deleted. The "previous copy preserved" guarantee was broken for
            # recent writes. Checkpoint first; refuse instead of destroying
            # data if the checkpoint cannot complete (app still running?).
            _chk = sqlite3.connect(cur)
            try:
                _busy, _log, _ckpt = _chk.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                _chk.close()
            if _busy or _ckpt < _log:
                raise BackupError("تعذر تفريغ WAL قبل الاستعادة (قاعدة مشغولة؟) — أوقف التطبيق أولاً")
            shutil.copy2(cur, cur.with_name(f"pre_restore_{time.strftime('%Y%m%d_%H%M%S')}.sqlite3"))
        for suf in ("", "-wal", "-shm"):
            Path(str(cur) + suf).unlink(missing_ok=True)
        shutil.move(str(newdb), cur)
    return cur
