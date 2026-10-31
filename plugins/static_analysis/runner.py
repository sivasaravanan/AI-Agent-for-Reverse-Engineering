"""Orchestrator: accepts a ZIP or directory, discovers files, runs adapters, returns an AnalysisReport."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .base import Adapter, Context, find_tool
from .js_adapter import JavaScriptAdapter
from .models import AnalysisReport, Options, ToolRun
from .other_adapters import DotNetAdapter, JavaAdapter, SqlAdapter
from .python_adapter import PythonAdapter
from .safe_extract import ArchiveRejected, safe_extract

__all__ = ["analyze", "available_tools", "register_adapter", "ArchiveRejected"]

ADAPTERS: list = [PythonAdapter, JavaScriptAdapter, DotNetAdapter, JavaAdapter, SqlAdapter]

EXT_LANG = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
            ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".cs": "csharp",
            ".vb": "vbnet", ".java": "java", ".sql": "sql"}
SKIP_DIRS = {"node_modules", ".git", ".hg", ".svn", "venv", ".venv", "env", "__pycache__", "bin", "obj",
             "dist", "build", "site-packages", ".idea", ".vs", "target"}
TOOLS = ("ruff", "pyright", "eslint", "tsc", "dotnet", "javac", "sqlfluff")

LIMITATIONS = [
    "Static analysis only: no uploaded project code is built or run (except an explicitly approved 'dotnet build').",
    "Findings are tool output or heuristics, not proof of runtime behaviour; verification status is 'tool-reported'.",
    "Project dependencies are never installed, so unresolved-import/symbol errors may be false positives (lower confidence).",
    "Project-supplied linter/type-checker configuration is deliberately ignored so it cannot execute code.",
    "Tool output captured in the report may echo source lines; review before sharing reports externally.",
]


def _scratch_dir():
    """Temporary copies go to the scratch folder chosen in Storage & clean-up (None = the system temp folder)."""
    try:
        from plugins.build_execution.storage import temp_root
        return temp_root()
    except Exception:  # noqa: BLE001 - never a reason to fail
        return None
def register_adapter(cls) -> None:
    """Add a third-party adapter class (subclass of base.Adapter)."""
    if cls not in ADAPTERS:
        ADAPTERS.append(cls)


def available_tools(options: Optional[Options] = None) -> dict:
    options = options or Options()
    return {t: find_tool(t, options) for t in TOOLS}


def _discover(root: Path, options: Options) -> tuple:
    files: dict = {}
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()]
        for fn in filenames:
            lang = EXT_LANG.get(os.path.splitext(fn)[1].lower())
            p = Path(dirpath) / fn
            if lang and lang in options.languages and not p.is_symlink():
                total += 1
                if total > options.max_files:
                    truncated = True
                    break
                files.setdefault(lang, []).append(p)
        if truncated:
            break
    return files, truncated


def _descend_single_root(root: Path) -> Path:
    entries = [e for e in root.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def analyze(source, options: Optional[Options] = None, progress: Optional[Callable[[str], None]] = None,
            cancel: Optional[threading.Event] = None) -> AnalysisReport:
    """Analyze a .zip archive or a directory. Never raises for tool failures; raises ArchiveRejected for
    unsafe/invalid archives and FileNotFoundError for a missing source."""
    options = options or Options()
    cancel = cancel or threading.Event()
    log = progress or (lambda m: None)
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {src}")
    report = AnalysisReport(source=str(src), started=datetime.now(timezone.utc).isoformat(),
                            options={"languages": sorted(options.languages), "timeout_s": options.timeout_s,
                                     "approved_tools": sorted(options.approved_tools),
                                     "use_pyright": options.use_pyright, "sql_dialect": options.sql_dialect},
                            limitations=list(LIMITATIONS))
    scratch_root = Path(tempfile.mkdtemp(prefix="sa_plugin_", dir=_scratch_dir()))
    try:
        if src.is_dir():
            root = src.resolve()
        else:
            log("Validating and extracting archive (source files only)...")
            extract_dir = scratch_root / "project"
            report.notes.extend(safe_extract(src, extract_dir))
            root = _descend_single_root(extract_dir.resolve())
        files, truncated = _discover(root, options)
        report.languages_found = {k: len(v) for k, v in sorted(files.items())}
        if truncated:
            report.notes.append(f"File discovery stopped at {options.max_files} files.")
        if not files:
            report.notes.append("No files for the selected languages were found.")
        tool_scratch = scratch_root / "tools"
        tool_scratch.mkdir()
        ctx = Context(root=root, files=files, options=options, scratch=tool_scratch, cancel=cancel, log=log)
        for cls in ADAPTERS:
            if cancel.is_set():
                report.notes.append("Cancelled by user; results are partial.")
                break
            adapter: Adapter = cls()
            if not any(files.get(l) for l in adapter.languages):
                continue
            log(f"Running {adapter.name} analyzers...")
            try:
                found, runs = adapter.run(ctx)
            except Exception as exc:  # one adapter failing must not hide the others
                found, runs = [], [ToolRun(analyzer=adapter.name, status="error",
                                           note=f"Adapter crashed: {type(exc).__name__}: {exc}",
                                           stderr=traceback.format_exc()[-4000:])]
            report.findings.extend(found)
            report.tool_runs.extend(runs)
        order = {s: i for i, s in enumerate(("critical", "high", "medium", "low", "info"))}
        report.findings.sort(key=lambda f: (order.get(f.severity, 9), f.file, f.line or 0))
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)
        report.finished = datetime.now(timezone.utc).isoformat()
        log("Done.")
    return report
