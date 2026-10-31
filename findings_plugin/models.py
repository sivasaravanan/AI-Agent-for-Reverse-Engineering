"""Structured finding model (Phase 6).

Design rules enforced here:
* line/column are Optional - never invented. None means "unknown".
* An AI hypothesis can never be created as CONFIRMED/STRONG or VERIFIED.
* Verification status changes only through FindingStore.set_verification()
  which demands evidence text (e.g. a test run reference).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEV_RANK[self]

    @classmethod
    def parse(cls, value: Any, default: Optional["Severity"] = None) -> "Severity":
        if isinstance(value, cls):
            return value
        key = str(value).strip().lower() if value is not None else ""
        try:
            return cls(key)
        except ValueError:
            if default is not None:
                return default
            raise


_SEV_RANK = {Severity.INFO: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}


class Kind(str, Enum):
    """What produced the finding - keeps heuristics apart from compiler facts."""
    PARSER_ERROR = "parser_error"
    COMPILER_ERROR = "compiler_error"
    LINT_WARNING = "lint_warning"
    HEURISTIC = "heuristic"
    RUNTIME_ERROR = "runtime_error"
    TEST_FAILURE = "test_failure"
    AI_HYPOTHESIS = "ai_hypothesis"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    STRONG = "strong"
    WEAK = "weak"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: Any, default: Optional["Confidence"] = None) -> "Confidence":
        if isinstance(value, cls):
            return value
        key = str(value).strip().lower() if value is not None else ""
        alias = {"high": "strong", "certain": "confirmed", "medium": "weak", "low": "weak"}
        try:
            return cls(alias.get(key, key))
        except ValueError:
            if default is not None:
                return default
            raise


class Verification(str, Enum):
    NOT_VERIFIED = "not_verified"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"


class Category:
    """Well-known category names (plain strings so analyzers can add their own)."""
    SYNTAX = "syntax"
    RUNTIME = "runtime"
    TEST = "test"
    SECURITY = "security"
    DEPENDENCY = "dependency"
    CONFIGURATION = "configuration"
    DATABASE = "database"
    MAINTENANCE = "maintenance"
    GENERAL = "general"


def _coerce(enum_cls, value):
    if isinstance(value, enum_cls):
        return value
    return enum_cls(str(value).strip().lower())


@dataclass
class Finding:
    title: str
    severity: Severity = Severity.MEDIUM
    category: str = Category.GENERAL
    kind: Kind = Kind.HEURISTIC
    file_path: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    evidence: str = ""
    explanation: str = ""
    remediation: str = ""
    source: str = "unknown"
    confidence: Confidence = Confidence.UNKNOWN
    verification: Verification = Verification.NOT_VERIFIED
    language: Optional[str] = None
    rule_id: Optional[str] = None
    debug_steps: list = field(default_factory=list)
    trace_id: Optional[str] = None
    extra: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        self.severity = Severity.parse(self.severity)
        self.kind = _coerce(Kind, self.kind)
        self.confidence = Confidence.parse(self.confidence)
        self.verification = _coerce(Verification, self.verification)
        self.title = (self.title or "").strip()
        if not self.title:
            raise ValueError("Finding.title must not be empty")
        self.category = (self.category or Category.GENERAL).strip().lower()
        if self.file_path:
            self.file_path = self.file_path.replace("\\", "/")
        else:
            self.file_path = None
        for name in ("line", "column"):
            v = getattr(self, name)
            if v is not None:
                v = int(v)
                if v < 1:
                    raise ValueError(f"Finding.{name} must be >= 1 or None (got {v})")
                setattr(self, name, v)
        if self.line is None and self.column is not None:
            raise ValueError("column given without line")
        self.debug_steps = list(self.debug_steps or [])
        if self.kind is Kind.AI_HYPOTHESIS:
            if self.confidence in (Confidence.CONFIRMED, Confidence.STRONG):
                raise ValueError("AI hypotheses cannot be created with confirmed/strong confidence")
            if self.verification is Verification.VERIFIED:
                raise ValueError("AI hypotheses cannot be created as verified")
        if not self.id:
            self.id = self.fingerprint()

    @property
    def is_detected(self) -> bool:
        """True when produced by a parser/tool/log - False for AI hypotheses."""
        return self.kind is not Kind.AI_HYPOTHESIS

    @property
    def location(self) -> str:
        if not self.file_path:
            return ""
        s = self.file_path
        if self.line:
            s += f":{self.line}"
            if self.column:
                s += f":{self.column}"
        return s

    def fingerprint(self) -> str:
        first_ev = (self.evidence or "").strip().splitlines()[0] if (self.evidence or "").strip() else ""
        raw = "|".join([self.kind.value, self.rule_id or "", self.file_path or "",
                        str(self.line or ""), self.title, self.source, first_ev])
        return "F-" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "severity": self.severity.value,
            "category": self.category, "kind": self.kind.value, "file_path": self.file_path,
            "line": self.line, "column": self.column, "evidence": self.evidence,
            "explanation": self.explanation, "remediation": self.remediation,
            "source": self.source, "confidence": self.confidence.value,
            "verification": self.verification.value, "language": self.language,
            "rule_id": self.rule_id, "debug_steps": list(self.debug_steps),
            "trace_id": self.trace_id, "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


def make_ai_finding(title: str, *, explanation: str = "", remediation: str = "",
                    source: str = "ai", severity: Severity = Severity.LOW,
                    category: str = Category.GENERAL, file_path: Optional[str] = None,
                    line: Optional[int] = None, related_to: Optional[str] = None,
                    model: Optional[str] = None, **kw) -> Finding:
    """Factory for Phase 7: always WEAK confidence, never verified."""
    extra = dict(kw.pop("extra", {}) or {})
    if related_to:
        extra["related_to"] = related_to
    if model:
        extra["model"] = model
    return Finding(title=title, severity=severity, category=category, kind=Kind.AI_HYPOTHESIS,
                   file_path=file_path, line=line, explanation=explanation, remediation=remediation,
                   source=source, confidence=Confidence.WEAK, extra=extra, **kw)
