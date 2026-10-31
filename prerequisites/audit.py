"""Append-only audit trail (JSON lines) of every download and install action - institutions usually require one.
Secrets (credentials inside URLs/proxies, tokens) are masked before anything is written."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.runtime_paths import data_root

_LOCK = threading.Lock()
_CRED_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_TOKEN = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+")


def mask(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    return _TOKEN.sub(r"\1\2****", _CRED_URL.sub(r"\1\2:****@", text))


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return mask(value)[:4000]
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value][:200]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in list(value.items())[:100]}
    return value if isinstance(value, (int, float, bool)) or value is None else mask(str(value))


def audit_path(root: Optional[Path] = None) -> Path:
    return (root or data_root()) / "logs" / "prerequisites_audit.jsonl"


class AuditLog:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else audit_path()

    def log(self, event: str, **fields: Any) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **{k: _clean(v) for k, v in fields.items()}}
        try:
            with _LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass   # an unwritable log must never block the user's work

    def read(self, limit: int = 200) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out
