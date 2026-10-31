"""Tk-free formatting of a finding's detail view (unit-testable, reused by the UI)."""
from __future__ import annotations

from typing import Optional

from .models import Finding, Kind, Verification


def format_detail(f: Finding, related: Optional[list] = None) -> list:
    """Return [(heading, body)] sections. Headings starting with '!' are notices."""
    out = []
    if f.kind is Kind.AI_HYPOTHESIS:
        out.append(("!Notice", "AI-generated hypothesis - an unverified suggestion, not a detected fault."))
    out.append(("Summary", " | ".join([
        f"Severity: {f.severity.value}", f"Category: {f.category}", f"Kind: {f.kind.value}",
        f"Confidence: {f.confidence.value}", f"Verification: {f.verification.value}",
        f"Language: {f.language or 'unknown'}"])))
    out.append(("Location", f.location or "unknown (not reported by the source)"))
    if f.evidence:
        out.append(("Evidence", f.evidence))
    if f.explanation:
        out.append(("Explanation", f.explanation))
    if f.remediation:
        out.append(("Suggested remediation", f.remediation))
    if f.debug_steps:
        out.append(("Suggested debugging steps", "\n".join(f"{i}. {s}" for i, s in enumerate(f.debug_steps, 1))))
    src = [f"Source: {f.source}"]
    if f.rule_id:
        src.append(f"Rule: {f.rule_id}")
    if f.extra.get("guidance_source"):
        src.append(f"Guidance: {f.extra['guidance_source']}")
    if f.extra.get("occurrences", 1) > 1:
        src.append(f"Occurrences in log: {f.extra['occurrences']}")
    if f.extra.get("frame_is_external"):
        src.append("Location is inside library/runtime code; the failing call may originate elsewhere.")
    if f.extra.get("severity_assumed"):
        src.append("Severity was not supplied by the source and was assumed to be medium.")
    if f.extra.get("log_name"):
        src.append(f"Log: {f.extra['log_name']} (line {f.extra.get('log_line', '?')})")
    out.append(("Origin", "\n".join(src)))
    if f.verification is not Verification.NOT_VERIFIED and f.extra.get("verification_evidence"):
        out.append(("Verification evidence", str(f.extra["verification_evidence"])))
    if related:
        out.append(("Related findings", "\n".join(f"{r.id}  [{why}]  {r.title}" for r, why in related)))
    return out
