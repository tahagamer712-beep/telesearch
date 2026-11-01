"""Streaming SHA-256."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

DEFAULT_CHUNK = 1024 * 1024


def sha256_stream(fp: BinaryIO, chunk: int = DEFAULT_CHUNK, progress=None) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    while True:
        block = fp.read(chunk)
        if not block:
            break
        h.update(block)
        total += len(block)
        if progress:
            progress(total)
    return h.hexdigest(), total


def sha256_file(path: Path, chunk: int = DEFAULT_CHUNK, progress=None) -> tuple[str, int]:
    with open(path, "rb") as fp:
        return sha256_stream(fp, chunk, progress)
