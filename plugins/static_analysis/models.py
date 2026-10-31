"""Data models for the Phase 3 static-analysis plugin (no GUI / no host-app imports)."""
from __future__ import annotations

import hashlib
import html
import json
import platform
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SEVERITIES = ("critical", "high", "medium", "low", "info")

# What the finding actually IS. A heuristic is never presented as a compiler error.
KINDS = (
    "parser_error",      # the language parser rejected the file
    "compiler_error",    # a real compiler reported an error
    "compiler_warning",  # a real compiler reported a warning
    "type_error",        # a type checker (e.g. pyright) reported an error
    "lint_warning",      # a linter rule
    "heuristic",         # pattern-based guess, NOT confirmed
)

ALL_LANGUAGES = ("python", "javascript", "typescript", "csharp", "vbnet", "java", "sql")


@dataclass
class Options:
    languages: set = field(default_factory=lambda: set(ALL_LANGUAGES))
    timeout_s: int = 180                 # per external tool invocation
    max_file_bytes: int = 2_000_000      # files larger than this are skipped (and reported)
    max_files: int = 20_000
    max_findings_per_tool: int = 2_000
    approved_tools: set = field(default_factory=set)   # e.g. {"dotnet"}; needed for tools that run project build logic
    use_pyright: bool = True
    sql_dialect: str = "ansi"
    tool_paths: dict = field(default_factory=dict)     # {"ruff": r"C:\tools\ruff.exe"} overrides PATH lookup


@dataclass
class Finding:
    analyzer: str
    kind: str
    severity: str
    message: str
    file: str = ""
    line: Optional[int] = None
    column: Optional[int] = None
    rule: str = ""
    language: str = ""
    confidence: str = "high"            # high | medium | low
    verification: str = "tool-reported" # never "confirmed-fixed" etc. - this plugin only reports
    evidence: str = ""
    analyzer_version: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raw = f"{self.analyzer}|{self.rule}|{self.file}|{self.line}|{self.column}|{self.message}"
            self.id = "SA-" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolRun:
    analyzer: str
    status: str   # ok | findings | unavailable | skipped | timeout | cancelled | error
    command: list = field(default_factory=list)
    version: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    note: str = ""
    files_checked: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisReport:
    source: str
    started: str = ""
    finished: str = ""
    host_python: str = platform.python_version()
    host_os: str = platform.platform()
    options: dict = field(default_factory=dict)
    languages_found: dict = field(default_factory=dict)   # language -> file count
    findings: list = field(default_factory=list)
    tool_runs: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    limitations: list = field(default_factory=list)

    def summary(self) -> dict:
        by_sev: dict = {}
        by_kind: dict = {}
        for f in self.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        return {"total": len(self.findings), "by_severity": by_sev, "by_kind": by_kind}

    def to_dict(self) -> dict:
        return {
            "source": self.source, "started": self.started, "finished": self.finished,
            "host_python": self.host_python, "host_os": self.host_os,
            "options": self.options, "languages_found": self.languages_found,
            "summary": self.summary(),
            "findings": [f.to_dict() for f in self.findings],
            "tool_runs": [t.to_dict() for t in self.tool_runs],
            "notes": self.notes, "limitations": self.limitations,
        }

    def write_json(self, path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def write_html(self, path) -> Path:
        e = html.escape
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        s = self.summary()
        rows = "".join(
            f"<tr><td>{e(f.severity)}</td><td>{e(f.kind)}</td><td>{e(f.file)}</td>"
            f"<td>{'' if f.line is None else f.line}</td><td>{e(f.rule)}</td><td>{e(f.analyzer)}</td>"
            f"<td>{e(f.confidence)}</td><td>{e(f.message)}</td></tr>" for f in self.findings)
        runs = "".join(
            f"<tr><td>{e(t.analyzer)}</td><td>{e(t.status)}</td><td>{e(t.version)}</td>"
            f"<td>{'' if t.exit_code is None else t.exit_code}</td><td>{e(' '.join(map(str, t.command)))}</td>"
            f"<td>{e(t.note)}</td></tr>" for t in self.tool_runs)
        lim = "".join(f"<li>{e(x)}</li>" for x in self.limitations)
        notes = "".join(f"<li>{e(x)}</li>" for x in self.notes)
        doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Static analysis report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
td,th{{border:1px solid #ccc;padding:4px 8px;font-size:13px;vertical-align:top}}th{{background:#f0f0f0;text-align:left}}</style></head><body>
<h1>Static analysis report</h1><p>Source: {e(self.source)}<br>Started: {e(self.started)} &middot; Finished: {e(self.finished)}<br>
Host: Python {e(self.host_python)} on {e(self.host_os)}</p>
<p><b>{s['total']}</b> findings &mdash; by severity: {e(json.dumps(s['by_severity']))}; by kind: {e(json.dumps(s['by_kind']))}</p>
<h2>Tools run</h2><table><tr><th>Analyzer</th><th>Status</th><th>Version</th><th>Exit</th><th>Command</th><th>Note</th></tr>{runs}</table>
<h2>Findings</h2><table><tr><th>Severity</th><th>Kind</th><th>File</th><th>Line</th><th>Rule</th><th>Analyzer</th><th>Confidence</th><th>Message</th></tr>{rows}</table>
<h2>Notes</h2><ul>{notes}</ul><h2>Limitations</h2><ul>{lim}</ul></body></html>"""
        p.write_text(doc, encoding="utf-8")
        return p
