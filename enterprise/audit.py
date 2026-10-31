"""Tamper-evident audit log: JSON Lines, each entry carries the SHA-256 of the previous entry (hash chain).

It detects edits, deletions and re-ordering of earlier lines. It cannot stop someone with write access from deleting the
whole file or truncating the tail and re-chaining; keep a copy of the latest hash elsewhere for stronger assurance.
Never put secrets in ``details``: values are passed through the secret redactor and length-limited.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from security.secret_scanner import redact

GENESIS = "0" * 64
_LOCK = threading.Lock()


_SENSITIVE_KEY = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|credential|authorization|private", re.I)


def _clean(v: Any, depth: int = 0) -> Any:
    if isinstance(v, str):
        return redact(re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b", "[redacted]", v))[:500]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if depth > 3:
        return "..."
    if isinstance(v, dict):
        return {str(k)[:60]: ("[redacted]" if _SENSITIVE_KEY.search(str(k)) else _clean(x, depth + 1)) for k, x in list(v.items())[:40]}
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in list(v)[:40]]
    return redact(str(v))[:200]


def _digest(entry: Dict[str, Any]) -> str:
    body = {k: entry[k] for k in ("seq", "time", "actor", "action", "subject", "details", "prev")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last(self) -> Tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        try:
            e = json.loads(last)
            return int(e["seq"]), str(e["hash"])
        except (ValueError, KeyError, TypeError):
            raise ValueError("audit log tail is corrupt; run verify() and keep the damaged file as evidence")

    def append(self, action: str, actor: str = "system", subject: str = "", details: Dict[str, Any] | None = None) -> Dict[str, Any]:
        with _LOCK:
            seq, prev = self._last()
            e = {"seq": seq + 1, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": _clean(actor),
                 "action": _clean(action), "subject": _clean(subject), "details": _clean(details or {}), "prev": prev}
            e["hash"] = _digest(e)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
            return e

    def entries(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"seq": -1, "corrupt": True})
        return out

    def verify(self) -> Dict[str, Any]:
        prev, n = GENESIS, 0
        for i, e in enumerate(self.entries(), 1):
            try:
                ok = e["seq"] == i and e["prev"] == prev and e["hash"] == _digest(e)
            except (KeyError, TypeError):
                ok = False
            if not ok:
                return {"ok": False, "entries": n, "first_bad_line": i,
                        "message": f"chain broken at entry {i}: the log was edited, reordered or truncated before this point"}
            prev, n = e["hash"], n + 1
        return {"ok": True, "entries": n, "head": prev, "message": "chain intact"}
