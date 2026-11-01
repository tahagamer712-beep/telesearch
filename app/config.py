"""Configuration loader (.env + environment). Stdlib only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. Ignores blank lines and # comments."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path.name} سطر {lineno}: لازم يكون بصيغة KEY=VALUE")
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def _num(src: dict[str, str], key: str, default, cast, lo=None, hi=None):
    raw = src.get(key, "")
    if raw == "":
        return default
    try:
        val = cast(raw)
    except ValueError:
        raise ConfigError(f"{key} لازم يكون رقم صالح، القيمة الحالية: {raw!r}") from None
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        raise ConfigError(f"{key} خارج المدى المسموح ({lo}..{hi}): {val}")
    return val


def _bool(src: dict[str, str], key: str, default: bool) -> bool:
    raw = src.get(key, "").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} لازم true او false، القيمة الحالية: {raw!r}")


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            raise ConfigError(f"AUTHORIZED_USER_IDS فيه قيمة مو رقم: {part!r}") from None
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    base_dir: Path
    telegram_token: str = field(default="", repr=False)
    authorized_user_ids: frozenset[int] = frozenset()
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_model: str = ""
    ai_timeout_sec: float = 20.0
    max_workers: int = 1
    chunk_size: int = 1024 * 1024
    batch_size: int = 1000
    throttle_delay_sec: float = 0.0
    max_cpu_percent: int = 85
    max_ram_percent: int = 80
    min_free_storage_gb: float = 2.0
    max_storage_usage_gb: float = 0.0  # 0 = بدون حد
    result_page_size: int = 10
    telegram_message_limit: int = 4000  # قابل للتعديل؛ لازم يتحقق منه من توثيق Telegram
    report_retention_days: int = 30  # 0 = للأبد
    search_history_retention_days: int = 30
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5
    keep_awake: bool = False
    auto_index: bool = True
    auto_resume: bool = True
    search_timeout_sec: float = 30.0
    max_archive_files: int = 200000
    # Audit fix (Phase 6 8.5): the old default 2000 could never fire for
    # deflate bombs — deflate's ceiling for redundant data is ~1032:1, so a
    # 300 MB zeros bomb sailed through and was extracted in full. 500 sits
    # below the deflate ceiling while leaving ample room for legitimate
    # highly-compressible archives (real text rarely exceeds 100:1).
    max_extract_ratio: int = 500
    max_extract_gb: float = 0.0  # 0 = الحد هو المساحة الفارغة فقط
    max_archive_depth: int = 3
    intake_settle_sec: int = 30
    telegram_download_limit_mb: int = 20
    telegram_upload_limit_mb: int = 50
    json_max_object_mb: int = 64
    pdf_font_path: str = ""
    backup_passphrase: str = field(default="", repr=False)
    backup_allow_unencrypted: bool = False
    backup_keep: int = 7
    telegram_poll_timeout: int = 30
    keep_extracted: bool = False
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    telegram_api_base: str = "https://api.telegram.org"
    ai_cache_size: int = 128
    auto_backup_hours: int = 0
    bomb_min_mb: int = 256
    callback_ttl_min: int = 1440
    force_sub_cache_sec: int = 60

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openrouter_api_key and self.openrouter_model)

    @property
    def db_path(self) -> Path:
        return self.base_dir / "database" / "main.sqlite3"


def load_config(base_dir: Path | None = None, require_telegram: bool = True) -> Config:
    """Load config. Real environment variables override .env values."""
    base = Path(base_dir or Path(__file__).resolve().parent.parent).resolve()
    src = parse_env_file(base / ".env")
    src.update({k: v for k, v in os.environ.items() if k in src or k.isupper()})

    token = src.get("TELEGRAM_BOT_TOKEN", "").strip()
    if require_telegram and not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN مفقود. ضعه بملف .env")
    ids = _ids(src.get("AUTHORIZED_USER_IDS", ""))
    if require_telegram and not ids:
        raise ConfigError("AUTHORIZED_USER_IDS فارغ. ضع رقم حسابك بتيليكرام على الاقل")

    return Config(
        base_dir=base,
        telegram_token=token,
        authorized_user_ids=ids,
        openrouter_api_key=src.get("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=src.get("OPENROUTER_MODEL", "").strip(),
        ai_timeout_sec=_num(src, "AI_TIMEOUT_SEC", 20.0, float, 1, 300),
        max_workers=_num(src, "MAX_WORKERS", 1, int, 1, 8),
        chunk_size=_num(src, "CHUNK_SIZE", 1024 * 1024, int, 4096, 64 * 1024 * 1024),
        batch_size=_num(src, "BATCH_SIZE", 1000, int, 10, 100000),
        throttle_delay_sec=_num(src, "THROTTLE_DELAY_SEC", 0.0, float, 0, 10),
        max_cpu_percent=_num(src, "MAX_CPU_PERCENT", 85, int, 10, 100),
        max_ram_percent=_num(src, "MAX_RAM_PERCENT", 80, int, 10, 95),
        min_free_storage_gb=_num(src, "MIN_FREE_STORAGE_GB", 2.0, float, 0, None),
        max_storage_usage_gb=_num(src, "MAX_STORAGE_USAGE_GB", 0.0, float, 0, None),
        result_page_size=_num(src, "RESULT_PAGE_SIZE", 10, int, 1, 100),
        telegram_message_limit=_num(src, "TELEGRAM_MESSAGE_LIMIT", 4000, int, 500, 4096),
        report_retention_days=_num(src, "REPORT_RETENTION_DAYS", 30, int, 0, 3650),
        search_history_retention_days=_num(src, "SEARCH_HISTORY_RETENTION_DAYS", 30, int, 0, 3650),
        log_max_bytes=_num(src, "LOG_MAX_BYTES", 5 * 1024 * 1024, int, 10240, None),
        log_backup_count=_num(src, "LOG_BACKUP_COUNT", 5, int, 1, 100),
        keep_awake=_bool(src, "KEEP_AWAKE", False),
        auto_index=_bool(src, "AUTO_INDEX", True),
        auto_resume=_bool(src, "AUTO_RESUME", True),
        search_timeout_sec=_num(src, "SEARCH_TIMEOUT_SEC", 30.0, float, 1, 3600),
        max_archive_files=_num(src, "MAX_ARCHIVE_FILES", 200000, int, 1, 10_000_000),
        max_extract_ratio=_num(src, "MAX_EXTRACT_RATIO", 500, int, 1, 1_000_000),
        max_extract_gb=_num(src, "MAX_EXTRACT_GB", 0.0, float, 0, None),
        max_archive_depth=_num(src, "MAX_ARCHIVE_DEPTH", 3, int, 1, 10),
        intake_settle_sec=_num(src, "INTAKE_SETTLE_SEC", 30, int, 1, 3600),
        telegram_download_limit_mb=_num(src, "TELEGRAM_DOWNLOAD_LIMIT_MB", 20, int, 1, 2000),
        telegram_upload_limit_mb=_num(src, "TELEGRAM_UPLOAD_LIMIT_MB", 50, int, 1, 2000),
        json_max_object_mb=_num(src, "JSON_MAX_OBJECT_MB", 64, int, 1, 1024),
        pdf_font_path=src.get("PDF_FONT_PATH", "").strip(),
        backup_passphrase=src.get("BACKUP_PASSPHRASE", ""),
        backup_allow_unencrypted=_bool(src, "BACKUP_ALLOW_UNENCRYPTED", False),
        backup_keep=_num(src, "BACKUP_KEEP", 7, int, 1, 1000),
        telegram_poll_timeout=_num(src, "TELEGRAM_POLL_TIMEOUT", 30, int, 1, 60),
        keep_extracted=_bool(src, "KEEP_EXTRACTED", False),
        openrouter_base_url=src.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/"),
        telegram_api_base=src.get("TELEGRAM_API_BASE", "https://api.telegram.org").strip().rstrip("/"),
        ai_cache_size=_num(src, "AI_CACHE_SIZE", 128, int, 0, 10000),
        auto_backup_hours=_num(src, "AUTO_BACKUP_HOURS", 0, int, 0, 8760),
        bomb_min_mb=_num(src, "BOMB_MIN_MB", 256, int, 1, 100000),
        callback_ttl_min=_num(src, "CALLBACK_TTL_MIN", 1440, int, 1, 100000),
        force_sub_cache_sec=_num(src, "FORCE_SUB_CACHE_SEC", 60, int, 0, 3600),
    )
