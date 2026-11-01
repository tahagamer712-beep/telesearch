"""AIProvider abstraction + OpenRouter implementation (stdlib urllib)."""
from __future__ import annotations

import abc
import json
import logging
import time
import urllib.error
import urllib.request

from ..config import Config

log = logging.getLogger("pdb.ai")


class AIUnavailable(Exception):
    """AI could not answer (offline, timeout, rate-limit, bad key...). Never contains secrets."""


class AIProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def enabled(self) -> bool: ...

    @abc.abstractmethod
    def complete(self, system: str, user: str) -> str: ...


class OpenRouterProvider(AIProvider):
    def __init__(self, config: Config) -> None:
        self.cfg = config

    @property
    def enabled(self) -> bool:
        return self.cfg.ai_enabled

    def complete(self, system: str, user: str) -> str:
        if not self.enabled:
            raise AIUnavailable("AI معطّل: OPENROUTER_API_KEY / OPENROUTER_MODEL غير مضبوطين")
        body = json.dumps({"model": self.cfg.openrouter_model, "temperature": 0, "max_tokens": 400,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(self.cfg.openrouter_base_url + "/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.cfg.openrouter_api_key}", "Content-Type": "application/json"})
        deadline = time.monotonic() + self.cfg.ai_timeout_sec
        delay = 1.0
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AIUnavailable("AI request timeout")
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    wait = min(delay, max(deadline - time.monotonic(), 0))
                    if attempt < 3 and wait > 0:
                        log.warning("AI http %s - backoff %.1fs", exc.code, wait)
                        time.sleep(wait)
                        delay *= 2
                        continue
                raise AIUnavailable(f"AI HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise AIUnavailable("AI غير متاح (لا يوجد اتصال او timeout)") from None
            except (KeyError, IndexError, ValueError):
                raise AIUnavailable("رد AI غير مفهوم") from None
        raise AIUnavailable("AI rate limit")
