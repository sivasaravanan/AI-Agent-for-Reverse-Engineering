"""Structured finding model shared by all analyzers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass
class Finding:
    """One detected problem or observation.

    ``confidence`` and ``verification`` are deliberately explicit so that a
    heuristic warning is never presented as a confirmed compiler error.
    """

    rule_id: str
    severity: str
    category: str
    title: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    evidence: str = ""
    explanation: str = ""
    remediation: str = ""
    source: str = ""
    confidence: str = "low"  # confirmed | high | medium | low
    verification: str = "unverified-heuristic"
    id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
