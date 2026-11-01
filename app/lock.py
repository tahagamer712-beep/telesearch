"""Single-instance lock (prevents two bots polling the same token -> Telegram Conflict)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # غير متوفر خارج Unix
    fcntl = None


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path, self.fd = Path(path), None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if fcntl is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise AlreadyRunning("Bot already running") from None
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def is_locked(path: Path) -> bool:
        if fcntl is None or not Path(path).exists():
            return False
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
