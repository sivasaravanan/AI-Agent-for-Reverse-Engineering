"""Secret redaction + secret scanning (standalone copy; no dependency on other phases)."""
from __future__ import annotations

import re
from typing import List, Tuple

MASK = "****"
_TEMPLATED = re.compile(r"^\s*(?:|<[^>]*>|\$\{[^}]*\}|\$[A-Za-z_]\w*|%\(?\w*\)?[sdr]|\{\{.*\}\}|\*{3,}|"
                        r"(?:process\.env|os\.environ|os\.getenv|getenv|env|ENV)\b.*)\s*$", re.I)
_URL_CRED = re.compile(r"(?P<s>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<u>[^:/?#@\s'\"]*):(?P<p>[^@/?#\s'\"]*)@")
_KEYS = (r"(?:password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|"
         r"shared[_-]?access[_-]?key|client[_-]?secret|private[_-]?key|authorization)")
_KV = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<k>(?:[A-Za-z0-9]+[_.\-])*" + _KEYS + r")"
                 r"(?P<sep>[\"']?\s*(?:=>|[=:])\s*)(?:(?P<q>[\"'])(?P<qv>[^\"']*)(?P=q)|(?P<v>\$\{[^}]*\}|[^;&\s\"',}]+))")
# token shapes that are secrets wherever they appear
_TOKENS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("Anthropic/OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
]


def _templated(v: str) -> bool:
    return bool(_TEMPLATED.match(v or ""))


def _sub(m: "re.Match[str]") -> str:
    val = m.group("qv") if m.group("q") else m.group("v")
    if _templated(val or ""):
        return m.group(0)
    q = m.group("q") or ""
    return f'{m.group("k")}{m.group("sep")}{q}{MASK}{q}'


def redact_text(text: str) -> str:
    if not text:
        return text
    text = _URL_CRED.sub(lambda m: f'{m.group("s")}{m.group("u")}:{MASK}@', text)
    text = _KV.sub(_sub, text)
    for _name, rx in _TOKENS:
        text = rx.sub(MASK, text)
    return text


_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".vb", ".go", ".php", ".rb", ".kt", ".sh", ".ps1", ".bat", ".cmd"}
_CONFIG_EXT = {".env", ".ini", ".cfg", ".conf", ".properties", ".yml", ".yaml", ".toml", ".config", ".xml", ".txt", ".json", ""}
_KEYWORDS = re.compile(r"(?i)(true|false|none|null|nil|required|optional|string|str|password|passwd|pwd|secret|token|changeme|your[_-]?\w*|x{3,}|\*+|example|dummy|test|sample)")
_CODEISH = re.compile(r"[\[\](){}<>]|\.\w+\(|^\w+\.\w+$|\s")


def find_secrets(text: str, filename: str = "") -> List[Tuple[str, int]]:
    """Return [(kind, line_number)] - never the secret value itself.
    Source-code files only count QUOTED literals (variable references such as password=p["password"] are code,
    not secrets); config-like files also count bare values. Token-shaped secrets count everywhere."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename.rsplit("/", 1)[-1] else ""
    base = filename.rsplit("/", 1)[-1].lower()
    code_file = ext in _CODE_EXT
    config_like = (ext in _CONFIG_EXT or base.startswith(".env")) and not code_file
    hits: List[Tuple[str, int]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > 5000:
            continue
        for name, rx in _TOKENS:
            if rx.search(line):
                hits.append((name, n))
        for m in _URL_CRED.finditer(line):
            if not _templated(m.group("p")) and not _KEYWORDS.fullmatch(m.group("p")):
                hits.append(("credentials in URL", n))
        for m in _KV.finditer(line):
            quoted = bool(m.group("q"))
            val = m.group("qv") if quoted else m.group("v")
            if not val or _templated(val) or len(val) < 4 or _KEYWORDS.fullmatch(val):
                continue
            if quoted:
                if code_file and _CODEISH.search(val):
                    continue          # string with spaces/brackets in code is text/format, not a credential
            elif not config_like or _CODEISH.search(val):
                continue
            hits.append(("hard-coded secret value", n))
    return hits
