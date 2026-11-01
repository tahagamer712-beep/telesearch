"""Minimal OLE2 / Compound File Binary reader (enough to stream the 'Workbook' of .xls)."""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Iterator

from ..errors import ParserError

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END = 0xFFFFFFFE


class CFB:
    def __init__(self, path: Path) -> None:
        self.f = open(path, "rb")
        try:
            h = self.f.read(512)
            if h[:8] != MAGIC:
                raise ParserError("ليس ملف OLE2")
            self.ssz = 1 << struct.unpack("<H", h[30:32])[0]
            self.msz = 1 << struct.unpack("<H", h[32:34])[0]
            n_fat, self.dir_start, _, self.cutoff, self.mfat_start, n_mfat, difat_start, n_difat = struct.unpack("<IIIIIIII", h[44:76])
            difat = list(struct.unpack("<109I", h[76:512]))
            sid, seen = difat_start, 0
            while sid < 0xFFFFFFFA and seen < n_difat + 1:
                sec = self._sector(sid)
                vals = struct.unpack(f"<{self.ssz // 4}I", sec)
                difat.extend(vals[:-1])
                sid, seen = vals[-1], seen + 1
            self.fat: list[int] = []
            for s in difat[:n_fat]:
                if s < 0xFFFFFFFA:
                    self.fat.extend(struct.unpack(f"<{self.ssz // 4}I", self._sector(s)))
            self.entries = self._read_dir()
            self._mini = None

        except BaseException:
            # __init__ failed after opening (bad magic, truncated header,
            # corrupt chain): the handle would otherwise leak until GC.
            self.f.close()
            raise

    def _sector(self, sid: int) -> bytes:
        self.f.seek((sid + 1) * self.ssz)
        b = self.f.read(self.ssz)
        if len(b) < self.ssz:
            b = b.ljust(self.ssz, b"\0")
        return b

    def _chain(self, start: int) -> Iterator[int]:
        sid, n = start, 0
        while sid < 0xFFFFFFFA:
            yield sid
            sid = self.fat[sid] if sid < len(self.fat) else _END
            n += 1
            if n > len(self.fat) + 1:
                raise ParserError("سلسلة sectors دائرية (ملف تالف)")

    def _read_dir(self) -> list[dict]:
        data = b"".join(self._sector(s) for s in self._chain(self.dir_start))
        out = []
        for i in range(0, len(data) - 127, 128):
            e = data[i:i + 128]
            nl = struct.unpack("<H", e[64:66])[0]
            typ = e[66]
            if typ == 0 or nl < 2:
                continue
            start = struct.unpack("<I", e[116:120])[0]
            size = struct.unpack("<Q", e[120:128])[0] & 0xFFFFFFFF
            out.append({"name": e[:nl - 2].decode("utf-16-le", "replace"), "type": typ, "start": start, "size": size})
        return out

    def find(self, *names: str) -> dict | None:
        for e in self.entries:
            if e["type"] == 2 and e["name"] in names:
                return e
        return None

    def open_stream(self, entry: dict):
        """Return a file-like object for a stream (streamed from disk for big streams)."""
        if entry["size"] >= self.cutoff:
            return _SectorStream(self, entry)
        root = next(e for e in self.entries if e["type"] == 5)
        ministream = b"".join(self._sector(s) for s in self._chain(root["start"]))
        mfat = []
        for s in self._chain(self.mfat_start):
            mfat.extend(struct.unpack(f"<{self.ssz // 4}I", self._sector(s)))
        buf = bytearray()
        sid, n = entry["start"], 0
        while sid < 0xFFFFFFFA and n < len(mfat) + 1:
            buf += ministream[sid * self.msz:(sid + 1) * self.msz]
            sid = mfat[sid] if sid < len(mfat) else _END
            n += 1
        return io.BytesIO(bytes(buf[:entry["size"]]))

    def close(self) -> None:
        self.f.close()


class _SectorStream(io.RawIOBase):
    def __init__(self, cfb: CFB, entry: dict) -> None:
        self.cfb, self.size = cfb, entry["size"]
        self.chain = cfb._chain(entry["start"])
        self.buf = b""
        self.pos = 0

    def readable(self) -> bool: return True

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        while len(self.buf) < n:
            try:
                self.buf += self.cfb._sector(next(self.chain))
            except StopIteration:
                break
        out, self.buf = self.buf[:n], self.buf[n:]
        self.pos += len(out)
        return out


def has_workbook(path: Path) -> bool:
    try:
        c = CFB(path)
        ok = c.find("Workbook", "Book") is not None
        c.close()
        return ok
    except Exception:
        return False

def has_word_document(path: Path) -> bool:
    try:
        c = CFB(path)
        ok = c.find("WordDocument") is not None
        c.close()
        return ok
    except Exception:
        return False
