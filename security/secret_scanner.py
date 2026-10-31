"""Secret redaction. Used for every snippet that reaches a report or log."""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_URL_CRED = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)")
_KV = re.compile(
    r"(?i)\b([A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|client[_\-]?secret))"
    r"(\s*[=:]\s*)([\"']?)([^\"'\s;&,]+)"
)
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]{8,}=*")


def redact(text: str) -> str:
    """Return ``text`` with passwords, tokens and key material masked."""
    text = _URL_CRED.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", text)
    text = _AWS.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    return text
