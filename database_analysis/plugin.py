"""Plug-in adapter: the only module the host application needs to know about.

Three integration styles (pick one):
    1. Library call : database_analysis.run("project.zip") -> DatabaseAnalysisResult
    2. Registry     : database_analysis.register(registry)   (duck-typed, see below)
    3. Dict context : database_analysis.plugin.analyze({"zip_path": ...}) -> plain dict
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from . import __version__
from .analyzer import DatabaseAnalyzer
from .models import DatabaseAnalysisResult
from .options import AnalysisOptions
from .sources import FileProvider

PLUGIN_INFO: Dict[str, Any] = {
    "name": "database_analysis",
    "display_name": "Database Analysis",
    "version": __version__,
    "phase": 5,
    "entry_point": "run",
    "capabilities": ["database-detection", "connection-config", "orm-entities", "migrations",
                     "sql-schema", "dependency-drivers", "secret-redaction"],
    "executes_project_code": False,
    "network_access": False,
    "requires": [],
}


class DatabaseAnalysisPlugin:
    """Object-style adapter, convenient for hosts that keep a list of analyzer objects."""
    name = PLUGIN_INFO["name"]
    version = __version__

    def __init__(self, options: Optional[AnalysisOptions] = None, extra_detectors=None):
        self._analyzer = DatabaseAnalyzer(options, extra_detectors)

    def analyze_zip(self, path) -> DatabaseAnalysisResult:
        return self._analyzer.analyze_zip(path)

    def analyze_directory(self, path) -> DatabaseAnalysisResult:
        return self._analyzer.analyze_directory(path)

    def analyze_files(self, files: Mapping[str, Union[str, bytes]]) -> DatabaseAnalysisResult:
        return self._analyzer.analyze_files(files)

    def analyze_provider(self, provider: FileProvider) -> DatabaseAnalysisResult:
        return self._analyzer.analyze(provider)


def run(source: Union[str, os.PathLike, Mapping[str, Any], FileProvider],
        options: Optional[AnalysisOptions] = None, extra_detectors=None) -> DatabaseAnalysisResult:
    """source: a .zip path, a folder path, a {path: content} mapping, or a FileProvider."""
    plugin = DatabaseAnalysisPlugin(options, extra_detectors)
    if isinstance(source, FileProvider):
        return plugin.analyze_provider(source)
    if isinstance(source, Mapping):
        return plugin.analyze_files(source)
    if os.path.isdir(source):
        return plugin.analyze_directory(source)
    return plugin.analyze_zip(source)


def to_findings(result: DatabaseAnalysisResult) -> List[Dict[str, Any]]:
    """Findings as plain dicts using the Phase 6 field names (id, severity, category, file, line,
    column, evidence, explanation, remediation, source, confidence, verification_status)."""
    return [dict(id=f.id, severity=f.severity, category=f.category, file=f.file, line=f.line, column=f.column,
                 evidence=f.evidence, explanation=f.explanation, remediation=f.remediation, source=f.source,
                 confidence=f.confidence, verification_status=f.verification_status, rule=f.rule,
                 title=f.title, database=f.db) for f in result.findings]


def analyze(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Dict-in / dict-out adapter. context keys: zip_path | root_dir | files, optional options."""
    if context.get("zip_path"):
        src: Any = context["zip_path"]
    elif context.get("root_dir"):
        src = context["root_dir"]
    elif context.get("files") is not None:
        src = context["files"]
    else:
        raise ValueError("context needs one of: zip_path, root_dir, files")
    res = run(src, context.get("options"))
    out = res.to_dict()
    out["findings_phase6"] = to_findings(res)
    return out


def register(registry: Any) -> None:
    """Register with a host registry. Supported shapes (first match wins):
    registry.register_analyzer(name, callable) | registry.register(name, callable) |
    registry.add_analyzer(name, callable) | a plain dict | a list (appends PLUGIN_INFO + callable)."""
    name = PLUGIN_INFO["name"]
    for meth in ("register_analyzer", "register", "add_analyzer"):
        fn: Optional[Callable] = getattr(registry, meth, None)
        if callable(fn):
            fn(name, run)
            return
    if isinstance(registry, dict):
        registry[name] = run
        return
    if isinstance(registry, list):
        registry.append({**PLUGIN_INFO, "callable": run})
        return
    raise TypeError("Unsupported registry type; call database_analysis.run(...) directly instead.")
