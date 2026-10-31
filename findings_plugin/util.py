"""Small shared helpers: language detection and secret redaction."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

LANG_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".cs": "csharp", ".vb": "vbnet", ".java": "java", ".kt": "kotlin",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css",
    ".php": "php", ".go": "go", ".json": "json", ".xml": "xml",
    ".yml": "yaml", ".yaml": "yaml", ".ini": "ini", ".cfg": "ini", ".toml": "toml",
}


def detect_language(path: str | None) -> str | None:
    """Language from file extension only (evidence-based, no guessing)."""
    if not path:
        return None
    ext = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    return LANG_BY_EXT.get(ext)


_REDACTIONS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|client[_-]?secret)\b"
                r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s;,&]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
]


def redact(text: str | None) -> str:
    """Mask passwords, tokens, API keys and connection-string secrets."""
    if not text:
        return text or ""
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text
