"""Database artifact analysis (Phase 5), backed by the self-contained ``database_analysis`` package.

Never connects to a database; secrets are masked by the package. Evidence levels (confirmed/strong/weak) are kept
as the package reports them - a driver or a name in a comment is never "confirmed".
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from models.finding import Finding

SEV = {"critical", "high", "medium", "low", "info"}


def analyze_database_artifacts(texts: Mapping[str, str], manifests=None, options: Any = None):
    """``texts`` = {archive path: already-read text}. Returns a ``database_analysis.DatabaseAnalysisResult``."""
    import database_analysis as dba
    return dba.run(dict(texts), options)


def to_main_findings(result) -> List[Finding]:
    import database_analysis as dba
    out: List[Finding] = []
    for d in dba.to_findings(result):
        sev = d["severity"] if d["severity"] in SEV else "info"
        out.append(Finding(
            rule_id=str(d.get("id") or d.get("rule") or "db"), severity=sev, category=str(d.get("category") or "database"),
            title=str(d.get("title") or d.get("rule") or "Database finding")[:200], file=d.get("file") or None,
            line=d.get("line"), column=d.get("column"), evidence=str(d.get("evidence") or "")[:500],
            explanation=str(d.get("explanation") or ""), remediation=str(d.get("remediation") or ""),
            source="phase5:database_analysis", confidence=str(d.get("confidence") or "low"),
            verification=str(d.get("verification_status") or "unverified-heuristic")))
    return out


def summarize(result) -> Dict[str, Any]:
    """JSON-safe dict for reports (already redacted by the package)."""
    return result.to_dict()


def html_fragment(result) -> str:
    import database_analysis as dba
    return dba.render_html_fragment(result)
