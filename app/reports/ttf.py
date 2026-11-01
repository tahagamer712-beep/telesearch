"""Minimal TrueType reader: cmap (format 4/12), advance widths, metrics."""
from __future__ import annotations

import struct
from pathlib import Path

from ..errors import AppError


class TTFont:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:4] not in (b"\x00\x01\x00\x00", b"true"):
            raise AppError(f"الخط ليس TTF بسيط (ttc/otf غير مدعوم): {path}")
        n = struct.unpack(">H", self.data[4:6])[0]
        self.tables = {}
        for i in range(n):
            tag, _cs, off, ln = struct.unpack(">4sIII", self.data[12 + 16 * i:28 + 16 * i])
            self.tables[tag.decode("latin1")] = (off, ln)
        head = self.tables["head"][0]
        self.units = struct.unpack(">H", self.data[head + 18:head + 20])[0]
        self.bbox = struct.unpack(">4h", self.data[head + 36:head + 44])
        hhea = self.tables["hhea"][0]
        self.ascent, self.descent = struct.unpack(">2h", self.data[hhea + 4:hhea + 8])
        self.n_hmetrics = struct.unpack(">H", self.data[hhea + 34:hhea + 36])[0]
        self.num_glyphs = struct.unpack(">H", self.data[self.tables["maxp"][0] + 4:self.tables["maxp"][0] + 6])[0]
        self.cmap = self._read_cmap()
        self._hmtx = self.tables["hmtx"][0]

    def _read_cmap(self) -> dict[int, int]:
        off = self.tables["cmap"][0]
        n = struct.unpack(">H", self.data[off + 2:off + 4])[0]
        best = None
        for i in range(n):
            pid, eid, so = struct.unpack(">HHI", self.data[off + 4 + 8 * i:off + 12 + 8 * i])
            fmt = struct.unpack(">H", self.data[off + so:off + so + 2])[0]
            if (pid, eid, fmt) in ((3, 10, 12), (0, 4, 12), (0, 6, 12)):
                best = (off + so, fmt)
                break
            if (pid, eid, fmt) in ((3, 1, 4), (0, 3, 4)) and best is None:
                best = (off + so, fmt)
        if best is None:
            raise AppError("الخط لا يحتوي cmap مدعوم")
        base, fmt = best
        out: dict[int, int] = {}
        d = self.data
        if fmt == 12:
            ng = struct.unpack(">I", d[base + 12:base + 16])[0]
            for g in range(ng):
                s, e, gid = struct.unpack(">III", d[base + 16 + 12 * g:base + 28 + 12 * g])
                for cp in range(s, e + 1):
                    out[cp] = gid + cp - s
        else:
            segx2 = struct.unpack(">H", d[base + 6:base + 8])[0]
            seg = segx2 // 2
            ends = struct.unpack(f">{seg}H", d[base + 14:base + 14 + segx2])
            starts = struct.unpack(f">{seg}H", d[base + 16 + segx2:base + 16 + 2 * segx2])
            deltas = struct.unpack(f">{seg}h", d[base + 16 + 2 * segx2:base + 16 + 3 * segx2])
            ro_base = base + 16 + 3 * segx2
            ros = struct.unpack(f">{seg}H", d[ro_base:ro_base + segx2])
            for i in range(seg):
                for cp in range(starts[i], ends[i] + 1):
                    if cp == 0xFFFF:
                        continue
                    if ros[i] == 0:
                        gid = (cp + deltas[i]) & 0xFFFF
                    else:
                        p = ro_base + 2 * i + ros[i] + 2 * (cp - starts[i])
                        gid = struct.unpack(">H", d[p:p + 2])[0]
                        if gid:
                            gid = (gid + deltas[i]) & 0xFFFF
                    if gid:
                        out[cp] = gid
        return out

    def advance(self, gid: int) -> int:
        idx = min(gid, self.n_hmetrics - 1)
        return struct.unpack(">H", self.data[self._hmtx + 4 * idx:self._hmtx + 4 * idx + 2])[0]

    def gid(self, cp: int) -> int:
        return self.cmap.get(cp, 0)
