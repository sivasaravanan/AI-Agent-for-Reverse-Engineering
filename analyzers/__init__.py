"""Language analyzers (Phase 3). Use ``run_static_analysis`` for a whole archive or the per-language classes."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from analyzers.base import BaseAnalyzer, PluginBackedAnalyzer, ToolResult, convert_finding
from analyzers.dotnet_analyzer import DotNetAnalyzer
from analyzers.java_analyzer import JavaAnalyzer
from analyzers.javascript_analyzer import JavaScriptAnalyzer
from analyzers.python_analyzer import PythonAnalyzer
from analyzers.sql_analyzer import SqlAnalyzer
from models.finding import Finding

ANALYZER_CLASSES = (PythonAnalyzer, JavaScriptAnalyzer, DotNetAnalyzer, JavaAnalyzer, SqlAnalyzer)


@dataclass
class StaticOutcome:
    findings: List[Finding] = field(default_factory=list)
    tool_runs: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    languages_found: Dict[str, int] = field(default_factory=dict)
    tools: Dict[str, Optional[str]] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings], "tool_runs": self.tool_runs, "notes": self.notes,
                "languages_found": self.languages_found, "tools": self.tools, "error": self.error}


def run_static_analysis(source, options=None, progress: Optional[Callable[[str], None]] = None,
                        cancel: Optional[threading.Event] = None,
                        skip_duplicates_of: Optional[List[Finding]] = None) -> StaticOutcome:
    """Run the Phase 3 plugin on a ZIP/folder. Never raises: failures are recorded in ``error``."""
    from plugins.static_analysis import ArchiveRejected, Options, analyze, available_tools
    out = StaticOutcome()
    options = options or Options()
    try:
        out.tools = available_tools(options)
        rep = analyze(source, options, progress=progress, cancel=cancel)
    except (ArchiveRejected, FileNotFoundError) as exc:
        out.error = f"Static-analysis plugin could not read the source: {exc}"
        return out
    except Exception as exc:  # noqa: BLE001 - optional phase must never break the main analysis
        out.error = f"Static-analysis plugin failed: {type(exc).__name__}: {exc}"
        return out
    seen = {(f.file, f.line) for f in (skip_duplicates_of or []) if f.rule_id == "py-syntax"}
    for pf in rep.findings:
        if pf.kind == "parser_error" and pf.language == "python" and (pf.file, pf.line) in seen:
            out.notes.append(f"Duplicate parser error at {pf.file}:{pf.line} already reported by the Phase 1 syntax check.")
            continue
        out.findings.append(convert_finding(pf))
    out.tool_runs = [t.to_dict() for t in rep.tool_runs]
    out.notes.extend(rep.notes)
    out.languages_found = dict(rep.languages_found)
    return out


def all_analyzers(options=None) -> List[BaseAnalyzer]:
    return [c(options) for c in ANALYZER_CLASSES]
