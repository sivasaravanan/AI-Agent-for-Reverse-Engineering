"""Reports and delivery (Phase 10), bridging the app's AnalysisResult to the ``report_delivery`` package.

* ``analysis_to_report_input`` - converts an AnalysisResult (+ approved runs, AI notes, patches) to the plug-in's input.
* ``write_delivery_reports``   - HTML/JSON/Markdown + dependency, static-analysis, security, build/test, inventory reports.
* ``build_package``            - secret-scanned, checksummed delivery ZIP of this application (never untrusted projects).
Nothing is invented: sections without data are reported as "not provided"/"skipped" by the plug-in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import report_delivery as rd
from models.analysis_result import AnalysisResult
from security.secret_scanner import redact

# Intentional fake secrets used by tests/fixtures; they are scanned but allowed (documented in the manifest).
DEFAULT_SECRET_ALLOWLIST = ("tests/*", "samples/*", "sample_project.zip", "docs/samples/*", "database_analysis/*",
                            "findings_plugin/tests/*", "scripts/make_sample_project.py")
DEFAULT_ALLOWED_ARCHIVES = ("sample_project.zip", "samples/*.zip", "findings_plugin/tests/fixtures/*.log")   # kept despite data-file rules
APP_KNOWN_ISSUES = [
    "Not verified on Windows; developed and tested on Linux (Python 3.12, Tk under Xvfb).",
    "Container isolation (Phase 4) was tested with a fake runtime; run `python -m plugins.build_execution check` once on your machine.",
    "AI providers were tested with a fake transport only; no real Ollama/OpenAI/OpenRouter call was made during development.",
    "External analyzers (ruff, pyright, eslint, tsc, javac, dotnet, sqlfluff) run only if installed; nothing is installed for you.",
    "The notebook has many tabs; some labels are shortened by Tk on small screens.",
]


def _finding(f) -> Dict[str, Any]:
    d = f.to_dict()
    d["verification_status"] = d.pop("verification", "unverified")
    d["rule"] = d.get("rule_id", "")
    for k in ("evidence", "explanation"):
        d[k] = redact(str(d.get(k) or ""))
    return d


def analysis_to_report_input(result: AnalysisResult) -> Dict[str, Any]:
    a = result.archive
    st = result.structure
    db = result.database_analysis or {}
    db_ids = {d.get("id") for d in db.get("findings", [])}
    findings = [_finding(f) for f in result.findings if not f.source.startswith("phase5")]   # DB findings come via database_analysis
    deps = [{"name": d.name, "version": d.spec, "ecosystem": d.ecosystem, "file": d.file, "line": d.line, "scope": d.scope}
            for m in result.manifests for d in m.dependencies]
    sa = result.static_analysis or {}
    static_results = [{"analyzer": r.get("analyzer"), "version": r.get("version"), "command": " ".join(map(str, r.get("command", []))),
                       "exit_code": r.get("exit_code"), "stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""),
                       "kind": r.get("status")} for r in sa.get("tool_runs", [])]
    runs = [{"command": " ".join(r.get("argv", [])), "status": r.get("status"), "exit_code": r.get("exit_code"),
             "duration_s": r.get("duration_s"), "stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""),
             "network": r.get("network_enabled"), "image": r.get("image")} for r in result.execution_results]
    ex = result.execution or {}
    bt_reason = None
    if not runs:
        iso = ex.get("isolation", {})
        bt_reason = ("No approved run was executed. " + (iso.get("reason") or "Execution requires explicit approval and a container runtime."))
    next_steps: List[str] = []
    if result.ai_notes:
        next_steps.append(f"{len(result.ai_notes)} AI-generated note(s) exist; they are unverified hypotheses - confirm with tests before acting.")
    for p in result.patches:
        level = p.get("verification", {}).get("level")
        next_steps.append(f"Patch {p['patch'].get('id')} on {p['patch'].get('file')}: verification level '{level}'.")
    inventory = {"totals": {"files": a.file_count, "folders": a.dir_count, "uncompressed_bytes": a.total_uncompressed,
                            "text_files_scanned": a.scanned_text_files, "skipped_entries": a.skipped_total,
                            "languages": ", ".join(f"{l.language} ({l.files})" for l in result.languages),
                            "frameworks": ", ".join(d.name for d in result.frameworks),
                            "databases": ", ".join(f"{d.name} ({d.confidence})" for d in result.databases)},
                 "files": []}
    if st is not None:
        inventory["entry_points"] = [f"{e.file}:{e.line or '-'} ({e.kind})" for e in st.entry_points]
    return {"project": {"name": Path(result.archive_name).stem, "archive": result.archive_name, "analyzed_at": result.analyzed_at,
                        "tool_version": result.tool_version},
            "inventory": inventory, "dependencies": deps,
            "static_analysis": {"results": static_results,
                                "skipped_reason": None if static_results else "Language analyzers were not run for this analysis."},
            "build_test": {"runs": runs, "skipped_reason": bt_reason}, "findings": findings,
            "database_analysis": db, "limitations": list(result.limitations) + list(result.warnings),
            "known_issues": [], "next_steps": next_steps}


def write_delivery_reports(result: AnalysisResult, out_dir) -> Dict[str, str]:
    """Writes analysis_report.html/.json, analysis_summary.md and the section reports into ``out_dir``."""
    return rd.build_reports(analysis_to_report_input(result), out_dir)


def build_package(root, out_dir, *, version: Optional[str] = None, run_tests: bool = False, reports_dir=None,
                  fail_on_secret: bool = False, known_issues: Sequence[str] = APP_KNOWN_ISSUES,
                  test_timeout: int = 900) -> Path:
    """Package the application at ``root`` into ``out_dir``/<name>-<version>.zip and return the path.
    ``run_tests`` runs the DELIVERED application's own test suite and stores the real result as evidence."""
    from core import __version__
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ver = version or __version__
    zip_path = out_dir / f"ai_software_engineering_agent-{ver}.zip"
    opts = rd.PackageOptions(version=ver, name="ai_software_engineering_agent", reports_dir=reports_dir,
                             known_issues=list(known_issues), secret_scan_allowlist=DEFAULT_SECRET_ALLOWLIST,
                             allow_archives=DEFAULT_ALLOWED_ARCHIVES, fail_on_secret=fail_on_secret,
                             exclude_dirs=("data", "merge_incoming", "_to_delete"))
    if run_tests:
        opts.evidence = rd.collect_test_evidence(root, timeout=test_timeout)
    rd.build_delivery_package(root, zip_path, opts)
    return zip_path
