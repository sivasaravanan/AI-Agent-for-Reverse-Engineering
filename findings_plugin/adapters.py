"""Convert the main app's existing findings (dicts, objects, exceptions, strings) to Finding."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import Category, Confidence, Finding, Kind, Severity
from .util import detect_language

_SEV_ALIASES = {
    "error": Severity.HIGH, "major": Severity.HIGH, "high": Severity.HIGH, "fatal": Severity.CRITICAL,
    "blocker": Severity.CRITICAL, "critical": Severity.CRITICAL, "warning": Severity.MEDIUM,
    "warn": Severity.MEDIUM, "medium": Severity.MEDIUM, "moderate": Severity.MEDIUM, "minor": Severity.LOW,
    "low": Severity.LOW, "note": Severity.LOW, "hint": Severity.LOW, "info": Severity.INFO,
    "informational": Severity.INFO,
}
_KEYS = {
    "severity": ("severity", "level", "priority"),
    "title": ("title", "message", "msg", "summary", "description", "text"),
    "file_path": ("file_path", "file", "path", "filename"),
    "line": ("line", "lineno", "line_number", "row"),
    "column": ("column", "col", "col_offset", "colno"),
    "evidence": ("evidence", "snippet", "code", "line_text"),
    "explanation": ("explanation", "details", "detail"),
    "remediation": ("remediation", "fix", "suggestion", "recommendation"),
    "category": ("category", "type"),
    "source": ("source", "analyzer", "tool"),
    "confidence": ("confidence",),
    "rule_id": ("rule_id", "rule", "code", "check"),
    "language": ("language", "lang"),
}


def parse_severity(value: Any) -> tuple:
    """Return (Severity, assumed) where assumed=True if the input gave no usable severity."""
    if value is None or str(value).strip() == "":
        return Severity.MEDIUM, True
    key = str(value).strip().lower()
    if key in _SEV_ALIASES:
        return _SEV_ALIASES[key], False
    try:
        return Severity.parse(key), False
    except ValueError:
        return Severity.MEDIUM, True


def _pick(d: dict, name: str):
    for k in _KEYS[name]:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _pos_int(v) -> Optional[int]:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 1 else None


def coerce_finding(obj: Any, *, default_source: str = "legacy") -> Finding:
    """Best-effort conversion. Never invents locations; unknown severity is flagged as assumed."""
    if isinstance(obj, Finding):
        return obj
    if isinstance(obj, SyntaxError):
        return from_syntax_error(obj, source=default_source)
    if isinstance(obj, str):
        d: dict = {"title": obj}
    elif isinstance(obj, dict):
        d = obj
    elif hasattr(obj, "__dict__"):
        d = vars(obj)
    else:
        raise TypeError(f"cannot convert {type(obj).__name__} to Finding")
    title = _pick(d, "title")
    if title is None:
        raise ValueError("finding has no message/title")
    sev, assumed = parse_severity(_pick(d, "severity"))
    source = str(_pick(d, "source") or default_source)
    cat_raw = _pick(d, "category")
    category = str(cat_raw).lower() if cat_raw else Category.GENERAL
    kind_raw = d.get("kind")
    try:
        kind = Kind(str(kind_raw).lower()) if kind_raw else None
    except ValueError:
        kind = None
    if kind is None:
        kind = Kind.PARSER_ERROR if "syntax" in (source + category).lower() else Kind.HEURISTIC
    file_path = _pick(d, "file_path")
    file_path = str(file_path) if file_path else None
    line = _pos_int(_pick(d, "line"))
    col = _pos_int(_pick(d, "column")) if line else None
    f = Finding(title=str(title), severity=sev, category=category, kind=kind, file_path=file_path,
                line=line, column=col, evidence=str(_pick(d, "evidence") or ""),
                explanation=str(_pick(d, "explanation") or ""), remediation=str(_pick(d, "remediation") or ""),
                source=source, confidence=Confidence.parse(_pick(d, "confidence"), Confidence.UNKNOWN),
                language=_pick(d, "language") or detect_language(file_path),
                rule_id=(str(_pick(d, "rule_id")) if _pick(d, "rule_id") else None))
    if assumed:
        f.extra["severity_assumed"] = True
    return f


def coerce_many(items: Iterable[Any], *, default_source: str = "legacy") -> tuple:
    """Convert many items; returns (findings, error_messages) - bad items are reported, not dropped silently."""
    ok, errors = [], []
    for idx, it in enumerate(items):
        try:
            ok.append(coerce_finding(it, default_source=default_source))
        except (TypeError, ValueError) as exc:
            errors.append(f"item {idx}: {exc}")
    return ok, errors


def from_syntax_error(exc: SyntaxError, file_path: Optional[str] = None, source: str = "python-ast") -> Finding:
    """Build a confirmed parser-error finding from an ast/compile SyntaxError."""
    fp = file_path or getattr(exc, "filename", None)
    line = _pos_int(getattr(exc, "lineno", None))
    col = _pos_int(getattr(exc, "offset", None)) if line else None
    text = (getattr(exc, "text", None) or "").rstrip("\n")
    f = Finding(title=f"{type(exc).__name__}: {exc.msg}", severity=Severity.HIGH, category=Category.SYNTAX,
                kind=Kind.PARSER_ERROR, file_path=fp, line=line, column=col, evidence=text, source=source,
                confidence=Confidence.CONFIRMED, language="python", rule_id=type(exc).__name__)
    from .knowledge import enrich
    enrich(f, type(exc).__name__)
    return f
