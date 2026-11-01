"""Entry point for the MTProto bridge service (runs on the Nokia).

Reads everything sensitive from the environment at runtime; nothing
secret is stored in code or printed to logs:

  MTPROTO_SESSION_PATH (required) — the EXISTING Telethon session file
  TELEGRAM_API_ID / TELEGRAM_API_HASH (required)
  MTPROTO_BOT_PEER (default @iraqdatabassbot) — the bot dialog to read
  PDB_BASE_DIR (default ~/private_database)
  MTPROTO_PREMIUM ("1" -> 4GB account cap, default 2GB)
  MTPROTO_POLL_SEC (default 30)

The bridge NEVER sends a message and NEVER opens any dialog except
the bot's. If the session is not authorized it refuses to start
rather than prompting for a login code. Run via start-bridge.sh (see
DEPLOYMENT.md); a second instance is refused by run/bridge.lock.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..db.connection import connect as db_connect
from ..lock import AlreadyRunning, InstanceLock
from ..storage import StorageManager
from .bridge import DialogFile, serve
from .pending import ensure_schema

log = logging.getLogger("pdb.mtproto")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


class TelethonBridgeClient:
    """Sync Telethon adapter for the bridge client protocol.

    Telethon is imported lazily here so unit tests never need it.
    """

    def __init__(self, session_path: str, api_id: str, api_hash: str,
                 bot_peer: str) -> None:
        from telethon.sync import TelegramClient
        self._client = TelegramClient(session_path, int(api_id), api_hash)
        self.bot_peer = bot_peer
        self._peer = None

    def start(self) -> "TelethonBridgeClient":
        self._client.connect()
        if not self._client.is_user_authorized():
            raise SystemExit(
                "mtproto session is not authorized; refusing interactive login")
        return self

    def _resolve_peer(self):
        if self._peer is None:
            self._peer = self._client.get_entity(self.bot_peer)
        return self._peer

    def list_recent(self, bot_peer: str, limit: int = 200) -> list[DialogFile]:
        peer = self._resolve_peer()
        out: list[DialogFile] = []
        for msg in self._client.get_messages(peer, limit=limit):
            f = getattr(msg, "file", None)
            if f is None:
                continue
            out.append(DialogFile(
                id=getattr(msg, "id", None),
                out=bool(getattr(msg, "out", False)),
                file_name=getattr(f, "name", "") or "",
                file_size=int(getattr(f, "size", 0) or 0),
                raw=msg))
        return out

    def download_media(self, raw, dest_path: Path) -> int:
        result = self._client.download_media(raw, file=str(dest_path))
        return Path(result).stat().st_size if result else 0

    def stop(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    session_path = _env("MTPROTO_SESSION_PATH", required=True)
    api_id = _env("TELEGRAM_API_ID", required=True)
    api_hash = _env("TELEGRAM_API_HASH", required=True)
    bot_peer = _env("MTPROTO_BOT_PEER", "@iraqdatabassbot")
    base_dir = Path(_env(
        "PDB_BASE_DIR", str(Path.home() / "private_database"))).expanduser()
    premium = _env("MTPROTO_PREMIUM", "0") == "1"
    poll_sec = float(_env("MTPROTO_POLL_SEC", "30"))
    db_path = base_dir / "database" / "main.sqlite3"
    if not db_path.exists():
        raise SystemExit(
            f"database not found at {db_path}; refusing to create one")
    lock = InstanceLock(base_dir / "run" / "bridge.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        print("bridge already running; refusing a second instance")
        return 0
    incoming = StorageManager(base_dir).dir("incoming")
    conn = db_connect(db_path)
    ensure_schema(conn)
    client = TelethonBridgeClient(
        session_path, api_id, api_hash, bot_peer).start()
    log.info("mtproto bridge started (bot peer %s)", bot_peer)
    try:
        serve(conn, client, incoming, bot_peer, premium=premium,
              poll_sec=poll_sec)
    finally:
        client.stop()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
