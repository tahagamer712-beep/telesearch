"""Rotating logs with secret redaction."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config


class SecretRedactor(logging.Filter):
    """Replaces known secret strings in log messages with ***."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


def setup_logging(config: Config, level: int = logging.INFO) -> logging.Logger:
    log_dir = Path(config.base_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("pdb")
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = SecretRedactor([config.telegram_token, config.openrouter_api_key, config.backup_passphrase])
    for handler in (
        RotatingFileHandler(log_dir / "app.log", maxBytes=config.log_max_bytes,
                            backupCount=config.log_backup_count, encoding="utf-8"),
        logging.StreamHandler(),
    ):
        handler.setFormatter(fmt)
        handler.addFilter(redactor)
        root.addHandler(handler)
    return root
