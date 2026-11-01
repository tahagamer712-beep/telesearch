"""MTProto bridge: download oversized owner files into data/incoming.

Runs as the OWNER's Telegram account (Telethon, see service.py). It
ONLY opens the dialog with the bot, ONLY reads messages in that
dialog, and NEVER sends any message. It downloads the single matched
file to a hidden .part file inside data/incoming and renames it when
complete, so the existing watcher only ever sees complete files.

This module is stdlib-only and takes its Telegram client as an
injected object (see the BridgeClient protocol below), so unit tests
run with a fake client and never touch a real session. Telethon is
imported lazily in service.py only.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .pending import (STATUS_DONE, STATUS_FAILED, claim_pending,
                      complete_pending, fail_pending, release_pending)

log = logging.getLogger("pdb.mtproto")

MAX_ACCOUNT_BYTES = 2 * 1024 ** 3
MAX_PREMIUM_BYTES = 4 * 1024 ** 3


class BridgeError(Exception):
    """Permanent bridge failure: mark the pending row failed."""


class TransientError(Exception):
    """Transient failure (network, partial download): release for retry."""


@dataclass
class DialogFile:
    """One file-bearing message in the owner's dialog with the bot."""
    id: int | None
    out: bool  # True only for messages sent BY the owner (never others')
    file_name: str
    file_size: int
    raw: Any  # opaque handle passed back to client.download_media


class BridgeClient:
    """Protocol for the Telegram client used by the bridge.

    A real implementation (service.TelethonBridgeClient) wraps Telethon;
    tests use a fake. The bridge never calls any send method — none
    exists on this protocol by design.
    """

    def list_recent(self, bot_peer: str, limit: int = 200) -> list[DialogFile]:
        """Recent file-bearing messages in the bot dialog, newest first."""
        raise NotImplementedError

    def download_media(self, raw: Any, dest_path: Path) -> int:
        """Download raw's media to dest_path. Returns bytes written."""
        raise NotImplementedError


def account_limit(premium: bool) -> int:
    """Max bytes the owner's account may download (no Premium assumed)."""
    return MAX_PREMIUM_BYTES if premium else MAX_ACCOUNT_BYTES


def match_file(messages: list[DialogFile], pending: dict) -> DialogFile | None:
    """Find the pending file's message, or None (never guess).

    Bot API message ids and MTProto message ids differ for the same
    message, so the pending row's message id is NEVER used for
    matching (it is kept only for the failure log below). Exactly
    one outgoing message with matching name and size qualifies;
    zero or several matches return None.
    """
    name = pending.get("file_name")
    size = pending.get("file_size")
    cands = [m for m in messages
             if m.out and m.file_name == name and m.file_size == size]
    if len(cands) == 1:
        return cands[0]
    pid = pending.get("message_id")
    if pid is None:
        id_note = "no row message id"
    elif pid in [m.id for m in messages]:
        hit = next(m for m in messages if m.id == pid)
        diffs = []
        if not hit.out:
            diffs.append("out=False")
        if hit.file_name != name:
            diffs.append("name mismatch")
        if hit.file_size != size:
            diffs.append("size mismatch")
        if diffs:
            id_note = "row id present but differs: " + ", ".join(diffs)
        else:
            id_note = "row id present and matches, but not unique"
    else:
        id_note = "row id absent from listed ids"
    log.warning(
        "bridge: match failed for %s (%s bytes): %s; "
        "out+name+size candidates=%d, listed messages=%d",
        name, size, id_note, len(cands), len(messages))
    return None


def unique_incoming_path(incoming_dir: Path, file_name: str,
                         pending_id: int) -> Path:
    """Final incoming path, never overwriting an existing file.

    file_name was already sanitized by the bot at intake; separators
    are still stripped defensively here.
    """
    safe = (Path(file_name).name or "file").replace("/", "_").replace("\\", "_")
    candidate = incoming_dir / safe
    if not candidate.exists():
        return candidate
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, ""
    else:
        ext = "." + ext
    candidate = incoming_dir / f"{stem}_{pending_id}{ext}"
    n = 2
    while candidate.exists():
        candidate = incoming_dir / f"{stem}_{pending_id}_{n}{ext}"
        n += 1
    return candidate


def download_to_incoming(client: BridgeClient, target: DialogFile,
                         incoming_dir: Path, pending_id: int,
                         expected_size: int) -> Path:
    """Download target into incoming via a hidden .part file, then rename.

    The watcher skips dotfiles and .part files, so it can never pick
    up a partial download. A size mismatch unlinks the temp file and
    raises TransientError (a truncated download may succeed on retry;
    a wrong file will exhaust attempts and fail).
    """
    incoming_dir = Path(incoming_dir)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    final = unique_incoming_path(incoming_dir, target.file_name, pending_id)
    tmp = incoming_dir / f".{final.name}.part"
    try:
        client.download_media(target.raw, tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    try:
        got = tmp.stat().st_size
    except OSError:
        tmp.unlink(missing_ok=True)
        raise TransientError("download produced no file")
    if got != expected_size:
        tmp.unlink(missing_ok=True)
        raise TransientError(
            f"size mismatch: got {got}, expected {expected_size}")
    os.replace(tmp, final)
    return final


def process_one(conn, client: BridgeClient, incoming_dir: Path,
                bot_peer: str, premium: bool = False) -> tuple[str, int | None]:
    """Process one pending row. Returns (outcome, pending_id).

    Outcomes: 'none' (nothing pending), 'done', 'failed' (permanent),
    'retry' (transient; the claim is released for a later poll).
    """
    row = claim_pending(conn)
    if row is None:
        return "none", None
    pid = row["id"]
    log.info("bridge: processing pending %s (%s, %s bytes)",
             pid, row["file_name"], row["file_size"])
    try:
        limit = account_limit(premium)
        if row["file_size"] > limit:
            raise BridgeError(
                f"file size {row['file_size']} exceeds account download "
                f"limit {limit}")
        messages = client.list_recent(bot_peer, limit=200)
        target = match_file(messages, row)
        if target is None:
            raise BridgeError(
                "target message not found or ambiguous in bot dialog")
        final = download_to_incoming(
            client, target, Path(incoming_dir), pid, row["file_size"])
        complete_pending(conn, pid, str(final))
        log.info("bridge: pending %s done -> %s", pid, final.name)
        return "done", pid
    except BridgeError as exc:
        fail_pending(conn, pid, str(exc))
        log.warning("bridge: pending %s failed: %s", pid, exc)
        return "failed", pid
    except Exception as exc:
        release_pending(conn, pid)
        log.warning("bridge: pending %s transient error, will retry: %s",
                    pid, exc)
        return "retry", pid


def serve(conn, client: BridgeClient, incoming_dir: Path, bot_peer: str,
          premium: bool = False, poll_sec: float = 30,
          sleep_fn: Callable[[float], None] = time.sleep,
          max_iterations: int | None = None) -> int:
    """Poll-process loop. Returns iterations run (max_iterations caps it
    for tests; None runs forever). Sleeps only when idle or retrying,
    so a backlog drains without delay."""
    n = 0
    while max_iterations is None or n < max_iterations:
        n += 1
        try:
            outcome, _pid = process_one(
                conn, client, incoming_dir, bot_peer, premium)
        except Exception as exc:
            log.exception("bridge: iteration failed: %s", exc)
            outcome = "error"
        if outcome in ("none", "retry", "error"):
            sleep_fn(poll_sec)
    return n
