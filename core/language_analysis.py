"""Phase 3 integration: run language-specific static analyzers on the texts already read from the archive.

Nothing is extracted to the project folder; source text goes to a temporary folder that is deleted afterwards.
External tools are only invoked when found on PATH. Nothing is installed. Heuristics are never labelled compiler errors.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from analyzers.base import convert_finding, run_plugin
from models.finding import Finding
from security.secret_scanner import redact

# Extensions the analyzers care about + manifests needed by tsc / dotnet / etc.
ANALYZABLE_EXT = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".vb", ".java", ".sql",
                  ".csproj", ".vbproj", ".sln", ".props", ".targets", "tsconfig.json", "package.json")

Progress = Callable[[str], None]


def select_sources(texts: Mapping[str, str]) -> Dict[str, str]:
    return {n: t for n, t in texts.items() if n.lower().endswith(ANALYZABLE_EXT)}


def _redact_run(run: dict) -> dict:
    for key in ("stdout", "stderr", "note"):
        if run.get(key):
            run[key] = redact(run[key])
    run["command"] = [redact(str(c)) for c in run.get("command", [])]
    return run


def run_language_analysis(texts: Mapping[str, str], options=None, progress: Optional[Progress] = None,
                          cancel: Optional[threading.Event] = None) -> Tuple[List[Finding], Dict]:
    """Return (findings, summary). ``summary`` is JSON friendly and goes into AnalysisResult.language_analysis."""
    sources = select_sources(texts)
    if not any(n.lower().endswith(ANALYZABLE_EXT[:11]) for n in sources):
        return [], {"ran": False, "reason": "No Python, JavaScript/TypeScript, C#/VB.NET, Java or SQL source files.",
                    "tool_runs": [], "languages_found": {}, "notes": [], "limitations": [], "summary": {}}
    report = run_plugin(sources, None, options=options, progress=progress, cancel=cancel)
    findings = []
    for pf in report.findings:
        f = convert_finding(pf)
        f.evidence = redact(f.evidence)
        f.title = redact(f.title)
        findings.append(f)
    data = report.to_dict()
    summary = {
        "ran": True, "source": data["source"], "started": data["started"], "finished": data["finished"],
        "languages_found": data["languages_found"], "summary": data["summary"], "options": data["options"],
        "tool_runs": [_redact_run(t) for t in data["tool_runs"]], "notes": data["notes"],
        "limitations": data["limitations"],
    }
    return findings, summary


def dedupe_against_phase1(existing: List[Finding], new: List[Finding]) -> List[Finding]:
    """Drop plugin Python parser errors already reported by the Phase 1 ast check at the same file+line."""
    seen = {(f.file, f.line) for f in existing if f.rule_id == "py-syntax"}
    out = []
    for f in new:
        if f.category == "syntax" and f.rule_id.startswith("python-ast") and (f.file, f.line) in seen:
            continue
        out.append(f)
    return out
