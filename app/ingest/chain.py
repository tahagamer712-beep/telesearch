"""ChainReader: present several split parts (file.001, file.002 ...) as ONE
seekable read-only file, without copying/merging on disk."""
from __future__ import annotations

import io
import os
from pathlib import Path


class ChainReader(io.RawIOBase):
    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self._paths = [Path(p) for p in paths]
        self._sizes = [os.path.getsize(p) for p in self._paths]
        self._starts = []
        acc = 0
        for s in self._sizes:
            self._starts.append(acc)
            acc += s
        self._total = acc
        self._pos = 0
        self._idx = -1
        self._fp = None

    # -- io api
    def readable(self) -> bool: return True
    def seekable(self) -> bool: return True
    def tell(self) -> int: return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._pos + offset
        else:
            new = self._total + offset
        self._pos = max(0, new)
        return self._pos

    def _open_for(self, pos: int) -> None:
        lo, hi = 0, len(self._starts) - 1
        idx = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._starts[mid] <= pos:
                idx, lo = mid, mid + 1
            else:
                hi = mid - 1
        if idx != self._idx:
            if self._fp:
                self._fp.close()
            self._fp = open(self._paths[idx], "rb")
            self._idx = idx
        self._fp.seek(pos - self._starts[idx])

    def readinto(self, b) -> int:
        if self._pos >= self._total:
            return 0
        self._open_for(self._pos)
        room = self._starts[self._idx] + self._sizes[self._idx] - self._pos
        n = self._fp.readinto(memoryview(b)[: min(len(b), room)])
        self._pos += n
        return n

    def close(self) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None
        super().close()
