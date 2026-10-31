"""Phase 9: plain-text preview of an analysis (what the report view shows inside the desktop app)."""
from __future__ import annotations

from collections import Counter
from typing import List

from models.analysis_result import AnalysisResult
from models.finding import SEVERITIES, SEVERITY_RANK
from security.secret_scanner import redact


def build_text_report(result: AnalysisResult, max_findings: int = 100) -> str:
    a = result.archive
    out: List[str] = [
        f"ANALYSIS REPORT - {result.archive_name}", "=" * 60,
        f"Analyzed: {result.analyzed_at}   Tool v{result.tool_version}   Duration: {result.duration_seconds}s",
        f"Files: {a.file_count:,}  Folders: {a.dir_count:,}  Uncompressed: {a.total_uncompressed:,} bytes",
        f"Text files scanned: {a.scanned_text_files:,}   Skipped entries: {a.skipped_total:,}", ""]
    out.append("LANGUAGES")
    out += [f"  {l.language:<12} {l.files:>5} files  {l.percent_of_bytes:>5}%  ({l.confidence})" for l in result.languages] or ["  none"]
    out += ["", "FRAMEWORKS / RUNTIMES"]
    out += [f"  {d.name:<28} {d.kind:<20} ({d.confidence})" for d in result.frameworks] or ["  none"]
    out += ["", "DATABASE INDICATORS (a driver alone is not proof of use)"]
    out += [f"  {d.name:<28} ({d.confidence})  {d.note}" for d in result.databases] or ["  none"]
    out += ["", "DEPENDENCY MANIFESTS"]
    out += [f"  {m.path}  [{m.ecosystem}/{m.kind}]  {len(m.dependencies)} dependencies" for m in result.manifests] or ["  none"]
    st = result.structure
    if st is not None:
        out += ["", "ENTRY POINTS"]
        out += [f"  {e.file}:{e.line or '-'}  {e.kind}  ({e.confidence})" for e in st.entry_points[:30]] or ["  none identified"]
        out += ["", "CONFIGURATION WARNINGS"]
        out += [f"  [{w.severity}] {w.code}: {w.message}" for w in st.warnings[:40]] or ["  none"]
    counts = Counter(f.severity for f in result.findings)
    out += ["", "FINDINGS  (" + ", ".join(f"{s}: {counts[s]}" for s in SEVERITIES if counts[s]) + ")" if result.findings else "FINDINGS  (none)"]
    shown = sorted(result.findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.file or "", f.line or 0))[:max_findings]
    for f in shown:
        loc = f"{f.file or ''}:{f.line}" if f.line else (f.file or "")
        out.append(f"  {f.id} [{f.severity}] {f.title}  @ {loc}  ({f.confidence}/{f.verification})")
        if f.evidence:
            out.append(f"      {redact(f.evidence)[:140]}")
    if len(result.findings) > len(shown):
        out.append(f"  ... {len(result.findings) - len(shown)} more (see the JSON/HTML report)")
    if result.warnings:
        out += ["", "WARNINGS"] + [f"  - {w}" for w in result.warnings]
    out += ["", "KNOWN LIMITATIONS"] + [f"  - {x}" for x in result.limitations]
    if st is not None:
        out += [f"  - {x}" for x in st.limitations]
    return "\n".join(out) + "\n"
