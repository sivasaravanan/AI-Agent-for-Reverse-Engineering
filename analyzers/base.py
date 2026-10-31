"""Analyzer plugin interface (Phase 3).

``BaseAnalyzer`` is the contract for language analyzers. ``PluginBackedAnalyzer`` implements it on top of the
self-contained ``plugins.static_analysis`` package (AST, Ruff, Pyright, ESLint, tsc, dotnet, javac, sqlfluff).

Honesty rules: a heuristic is never labelled a compiler error; missing tools are reported as unavailable (never
installed silently); verification is always "tool-reported".
"""
from __future__ import annotations

import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

from models.finding import Finding

# plugin finding kind -> (main-app category, main-app confidence for a non-heuristic result)
KIND_CATEGORY = {
    "parser_error": "syntax", "compiler_error": "compile", "compiler_warning": "compile",
    "type_error": "type-check", "lint_warning": "lint", "heuristic": "heuristic",
}
CONFIDENCE_MAP = {"high": "high", "medium": "medium", "low": "low"}


def _scratch_dir():
    """Temporary copies go to the scratch folder chosen in Storage & clean-up (None = the system temp folder)."""
    try:
        from plugins.build_execution.storage import temp_root
        return temp_root()
    except Exception:  # noqa: BLE001 - never a reason to fail
        return None
@dataclass
class ToolResult:
    analyzer: str
    version: str = ""
    command: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    findings: List[Finding] = field(default_factory=list)
    kind: str = "heuristic"   # parser | compiler | lint | heuristic
    status: str = "ok"        # ok | findings | unavailable | skipped | timeout | cancelled | error
    note: str = ""


def convert_finding(pf: Any) -> Finding:
    """Plugin Finding -> main-app Finding (keeps analyzer, command-kind and tool-reported verification)."""
    kind = getattr(pf, "kind", "heuristic")
    label = {"parser_error": "Parser error", "compiler_error": "Compiler error", "compiler_warning": "Compiler warning",
             "type_error": "Type-checker error", "lint_warning": "Lint warning", "heuristic": "Heuristic"}.get(kind, kind)
    return Finding(
        rule_id=f"{pf.analyzer}:{pf.rule}" if pf.rule else pf.analyzer,
        severity=pf.severity if pf.severity in ("critical", "high", "medium", "low", "info") else "info",
        category=KIND_CATEGORY.get(kind, "static-analysis"),
        title=f"[{label}] {pf.message}"[:200], file=pf.file or None, line=pf.line, column=pf.column,
        evidence=(pf.evidence or pf.message)[:500],
        explanation=f"Reported by {pf.analyzer}" + (f" {pf.analyzer_version}" if pf.analyzer_version else "") +
                    f" as a {label.lower()}." + (" Dependencies are not installed, so unresolved names may be false positives."
                                                   if pf.confidence == "medium" else ""),
        remediation="Open the file at the reported line and address the diagnostic; re-run the analyzer to confirm.",
        source=f"phase3:{pf.analyzer}", confidence=CONFIDENCE_MAP.get(pf.confidence, "low"),
        verification="tool-reported" if kind != "heuristic" else "unverified-heuristic", id="")


class BaseAnalyzer(ABC):
    name: str = ""
    languages: Sequence[str] = ()          # display names (match core.language_detector)

    @abstractmethod
    def is_available(self) -> bool:
        """Check that any external tool exists BEFORE invoking it."""

    def version(self) -> str:
        return ""

    @abstractmethod
    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        """Analyze already-read source text. Must not install tools or run project code."""


def _safe_rel(name: str) -> Optional[PurePosixPath]:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or ":" in p.parts[0]:
        return None
    return p


class PluginBackedAnalyzer(BaseAnalyzer):
    """Runs the Phase 3 plugin for one language family."""
    plugin_languages: Sequence[str] = ()    # plugin ids: python, javascript, typescript, csharp, vbnet, java, sql
    tools: Sequence[str] = ()               # external tools that enable extra checks
    builtin: bool = False                   # True if useful results exist without any external tool

    def __init__(self, options: Any = None) -> None:
        self.options = options

    def _plugin_options(self):
        from plugins.static_analysis import Options
        o = self.options if isinstance(self.options, Options) else Options()
        o.languages = set(self.plugin_languages)
        return o

    def is_available(self) -> bool:
        if self.builtin:
            return True
        from plugins.static_analysis import available_tools
        t = available_tools(self._plugin_options())
        return any(t.get(x) for x in self.tools)

    def run_on_source(self, source, progress=None, cancel=None) -> ToolResult:
        from plugins.static_analysis import analyze
        rep = analyze(source, self._plugin_options(), progress=progress, cancel=cancel)
        findings = [convert_finding(f) for f in rep.findings]
        runs = rep.tool_runs
        res = ToolResult(analyzer=self.name, findings=findings,
                         command="; ".join(" ".join(map(str, r.command)) for r in runs if r.command),
                         version=", ".join(f"{r.analyzer} {r.version}".strip() for r in runs if r.version),
                         stdout="\n".join(r.stdout for r in runs if r.stdout)[:20000],
                         stderr="\n".join(r.stderr for r in runs if r.stderr)[:20000],
                         exit_code=next((r.exit_code for r in runs if r.exit_code is not None), None),
                         kind="compiler" if any(f.category == "compile" for f in findings) else "lint",
                         status="findings" if findings else "ok",
                         note="; ".join(r.note for r in runs if r.note))
        res.runs = runs  # type: ignore[attr-defined]  (raw ToolRun list for reports)
        return res

    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        """Write the already-read text to a private temp folder (safe relative names only) and analyze it."""
        tmp = Path(tempfile.mkdtemp(prefix="ase_an_", dir=_scratch_dir()))
        try:
            for name, text in files.items():
                rel = _safe_rel(name)
                if rel is None:
                    continue
                dest = tmp.joinpath(*rel.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            return self.run_on_source(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
