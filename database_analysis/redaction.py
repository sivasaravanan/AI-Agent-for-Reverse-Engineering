"""Secret redaction. Every string that reaches a report, log or UI goes through here."""
from __future__ import annotations

import re

MASK = "****"

_TEMPLATED_RE = re.compile(
    r"^\s*(?:|<[^>]*>|\$\{[^}]*\}|\$[A-Za-z_]\w*|%\(?\w*\)?[sdr]|\{\{.*\}\}|\{[^{}]*\}|\*{3,}|\?|#\{.*\}"
    r"|(?:process\.env|os\.environ|os\.getenv|getenv|env|ENV|System\.getenv|"
    r"Environment\.GetEnvironmentVariable|config|settings|Configuration)\b.*)\s*$",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"(<[^>]+>|\$\{[^}]*\}|\{\{.*?\}\}|\{[A-Za-z_]\w*\}|%\(\w+\)s|%s|\byour[_-]?\w*|\bexample\b|"
    r"\bxxx+\b|changeme|\bplaceholder\b|\bhostname\b|\bdbname\b|\bdatabase_?name\b|\bmyhost\b|\bmydb\b)",
    re.I,
)

_URL_CRED = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/?#@\s'\"]*):(?P<pw>[^@/?#\s'\"]*)@"
)

_SECRET_KEYS = (
    r"(?:password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|"
    r"shared[_-]?access[_-]?key|sas[_-]?token|client[_-]?secret|private[_-]?key)"
)
_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<key>(?:[A-Za-z0-9]+[_.\-])*" + _SECRET_KEYS + r")"
    r"(?P<sep>[\"']?\s*(?:=>|[=:])\s*)"
    r"(?:(?P<q>[\"'])(?P<qv>[^\"']*)(?P=q)|(?P<v>\$\{[^}]*\}|[^;&\s\"',}]+))"
)


def looks_templated(value: str) -> bool:
    """True for empty values and env/template references (not a hard-coded secret)."""
    return bool(_TEMPLATED_RE.match(value or ""))


def is_placeholder_value(value: str) -> bool:
    """True when a host/db/user value is clearly a placeholder (e.g. <host>, ${DB_HOST})."""
    return bool(_PLACEHOLDER_RE.search(value or ""))


def _kv_sub(m: "re.Match[str]") -> str:
    val = m.group("qv") if m.group("q") else m.group("v")
    if looks_templated(val or ""):
        return m.group(0)
    if m.group("q"):
        return f'{m.group("key")}{m.group("sep")}{m.group("q")}{MASK}{m.group("q")}'
    return f'{m.group("key")}{m.group("sep")}{MASK}'


def redact_text(text: str) -> str:
    """Mask URL credentials and key/value secrets (password=..., "token": "...")."""
    if not text:
        return text
    text = _URL_CRED.sub(lambda m: f'{m.group("scheme")}{m.group("user")}:{MASK}@', text)
    return _KV.sub(_kv_sub, text)


def snippet(line: str, limit: int = 200) -> str:
    s = redact_text(line.strip())
    return s if len(s) <= limit else s[: limit - 1] + "…"
