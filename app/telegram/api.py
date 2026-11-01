"""Minimal Telegram Bot API client on stdlib (no third-party dependency)."""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.description, self.code, self.retry_after = description, code, retry_after


class TelegramAPI:
    def __init__(self, token: str, base: str = "https://api.telegram.org", timeout: float = 40.0) -> None:
        self._token, self.base, self.timeout = token, base.rstrip("/"), timeout

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self._token}/{method}"

    def _clean(self, s: str) -> str:
        return s.replace(self._token, "***")

    # Pre-send network failures only: the request never reached Telegram, so a
    # retry cannot duplicate a message. Bare read timeouts are NOT retried for
    # sends (the server may already have processed them); getUpdates is always
    # safe to retry (read-only).
    _PRE_SEND_TOKENS = ("handshake", "ssl", "connection reset", "connection aborted",
                        "connection refused", "name or service not known", "temporary failure",
                        "network is unreachable", "eof occurred")

    def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        req = urllib.request.Request(self._url(method), data=json.dumps(params or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                try:
                    data = json.loads(exc.read().decode("utf-8", "replace"))
                except ValueError:
                    raise TelegramError(f"HTTP {exc.code}", exc.code) from None
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = str(getattr(exc, "reason", exc))
                retryable = method == "getUpdates" or any(t in reason.lower() for t in self._PRE_SEND_TOKENS)
                if retryable and attempt < 2:
                    time.sleep(2 if attempt == 0 else 5)
                    continue
                raise TelegramError("network: " + self._clean(reason)) from None
        if not data.get("ok"):
            raise TelegramError(self._clean(str(data.get("description", "error"))), data.get("error_code", 0),
                                (data.get("parameters") or {}).get("retry_after"))
        return data["result"]

    # ---- helpers
    def get_updates(self, offset: int, timeout: int):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}, timeout + 15)

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self.call("sendMessage", p)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self.call("editMessageText", p)

    def answer_callback(self, callback_id: str, text: str | None = None):
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id, **({"text": text} if text else {})})

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", {"file_id": file_id})

    def download(self, file_path: str, dest: Path, max_bytes: int) -> int:
        url = f"{self.base}/file/bot{self._token}/{urllib.parse.quote(file_path)}"
        total = 0
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r, open(dest, "wb") as out:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    total += len(b)
                    if total > max_bytes:
                        raise TelegramError("الملف اكبر من الحد المسموح")
                    out.write(b)
        except (urllib.error.URLError, OSError) as exc:
            dest.unlink(missing_ok=True)
            raise TelegramError("download failed: " + self._clean(str(exc))) from None
        return total

    def send_document(self, chat_id: int, path: Path, caption: str | None = None):
        """Streaming multipart upload (file is never fully loaded in RAM)."""
        path = Path(path)
        b = uuid.uuid4().hex
        fields = [("chat_id", str(chat_id))] + ([("caption", caption[:1000])] if caption else [])
        head = "".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields)
        head += f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        tail = f"\r\n--{b}--\r\n".encode()
        head_b = head.encode()
        size = path.stat().st_size
        u = urllib.parse.urlparse(self.base)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port, timeout=self.timeout * 3)
        try:
            conn.putrequest("POST", f"{u.path}/bot{self._token}/sendDocument")
            conn.putheader("Content-Type", f"multipart/form-data; boundary={b}")
            conn.putheader("Content-Length", str(len(head_b) + size + len(tail)))
            conn.endheaders()
            conn.send(head_b)
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    conn.send(chunk)
            conn.send(tail)
            data = json.loads(conn.getresponse().read().decode("utf-8", "replace"))
        except (OSError, ValueError) as exc:
            raise TelegramError("upload failed: " + self._clean(str(exc))) from None
        finally:
            conn.close()
        if not data.get("ok"):
            raise TelegramError(self._clean(str(data.get("description", "error"))), data.get("error_code", 0))
        return data["result"]
