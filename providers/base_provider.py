"""AI provider interface (Phase 7). AI is OPTIONAL: nothing in the app requires a provider or an API key.

Secrets: API keys are never stored in config or code. They come from an environment variable named by
``api_key_env`` or from a session-only value held in memory (``set_session_key``). Keys, prompts and source text are
never logged; error text is passed through ``safe_error`` which masks key-looking strings.
"""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from security.secret_scanner import redact

log = logging.getLogger("ase.providers")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# transport(method, url, headers, body_bytes_or_None, timeout) -> (status, body_bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, bytes]]


class ProviderError(Exception):
    """Base class; messages are already secret-safe."""
    retryable = False


class ProviderUnavailable(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    retryable = True


class ProviderServerError(ProviderError):
    retryable = True


class ProviderTimeout(ProviderError):
    retryable = True


class ProviderResponseError(ProviderError):
    pass


def safe_error(text: Any, secrets: Tuple[str, ...] = ()) -> str:
    s = redact(str(text))
    for sec in secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "***REDACTED***")
    return s[:400]


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host == "host.docker.internal"


@dataclass
class ProviderConfig:
    name: str = ""
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = 60
    max_retries: int = 2
    kind: str = ""                      # ollama | openai_compatible | openrouter
    fallback_models: List[str] = field(default_factory=list)
    max_tokens: int = 1024              # per-response cap sent to the API where supported
    api_key_env: str = ""               # NAME of an environment variable - never the key itself
    enabled: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)   # non-secret headers only (e.g. X-Title)

    @property
    def is_local(self) -> bool:
        return is_local_url(self.base_url)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class GenerationResult:
    text: str
    provider: str
    model: str
    attempts: int = 1
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: str = ""
    truncated: bool = False
    fallback_used: bool = False
    errors: List[str] = field(default_factory=list)


def default_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                      timeout: float) -> Tuple[int, bytes]:
    if urlparse(url).scheme not in ("http", "https"):
        raise ProviderUnavailable("Only http(s) endpoints are allowed.")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (scheme checked above)
            return resp.status, resp.read(8_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000)
    except (socket.timeout, TimeoutError) as exc:
        raise ProviderTimeout("Request timed out.") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ProviderTimeout("Request timed out.") from exc
        raise ProviderUnavailable(f"Cannot reach {urlparse(url).hostname}: {safe_error(exc.reason)}") from exc


class BaseProvider(ABC):
    kind = ""

    def __init__(self, config: Optional[ProviderConfig] = None, transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config or ProviderConfig()
        self._transport = transport or default_transport
        self._sleep = sleep
        self._session_key: str = ""
        self.request_count = 0

    # -- keys (never persisted)
    def set_session_key(self, key: str) -> None:
        self._session_key = (key or "").strip()

    def api_key(self) -> str:
        if self._session_key:
            return self._session_key
        env = self.config.api_key_env
        return os.environ.get(env, "").strip() if env else ""

    @property
    def name(self) -> str:
        return self.config.name or self.kind

    @property
    def is_local(self) -> bool:
        return self.config.is_local

    # -- HTTP with timeouts, bounded retries and secret-safe errors
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "ASE-Agent/0.4"}
        h.update(self.config.extra_headers)
        return h

    def _request(self, method: str, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None,
                 retries: Optional[int] = None) -> Tuple[dict, int]:
        url = self.config.base_url.rstrip("/") + path
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        tmo = float(timeout or self.config.timeout_seconds)
        tries = (self.config.max_retries if retries is None else retries) + 1
        secrets = (self.api_key(),)
        last: Optional[ProviderError] = None
        for attempt in range(1, tries + 1):
            self.request_count += 1
            try:
                status, data = self._transport(method, url, self._headers(), body, tmo)
            except ProviderError as exc:
                last = exc
            else:
                if status in (401, 403):
                    raise ProviderAuthError(f"{self.name}: the server rejected the credentials (HTTP {status}).")
                if status == 404:
                    raise ProviderResponseError(f"{self.name}: endpoint or model not found (HTTP 404). "
                                                f"{safe_error(data.decode('utf-8', 'replace'), secrets)[:160]}")
                if status in RETRY_STATUS:
                    last = (ProviderRateLimited if status == 429 else ProviderServerError)(
                        f"{self.name}: HTTP {status}")
                elif status >= 400:
                    raise ProviderResponseError(f"{self.name}: HTTP {status}: "
                                                f"{safe_error(data.decode('utf-8', 'replace'), secrets)[:200]}")
                else:
                    try:
                        return json.loads(data.decode("utf-8", "replace") or "{}"), attempt
                    except ValueError as exc:
                        raise ProviderResponseError(f"{self.name}: the server did not return JSON.") from exc
            if attempt < tries and last is not None and last.retryable:
                self._sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) + random.random() * 0.1)
                continue
            break
        assert last is not None
        raise last

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def list_models(self) -> List[str]: ...

    @abstractmethod
    def complete(self, prompt: str, system: str = "", model: str = "", max_tokens: Optional[int] = None) -> GenerationResult: ...

    def generate(self, prompt: str, system: str = "", model: str = "", max_tokens: Optional[int] = None) -> str:
        return self.complete(prompt, system, model, max_tokens).text

    def check(self) -> Tuple[bool, str]:
        """(available, human-readable reason). Never raises."""
        try:
            models = self.list_models()
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{self.name}: unexpected error {type(exc).__name__}"
        if self.config.model and models and self.config.model not in models:
            return True, f"Reachable, but model '{self.config.model}' is not in the server's model list."
        return True, f"Reachable ({len(models)} model(s) listed)." if models else "Reachable."
