"""Plug-in adapter - the only module the host app needs.

    import report_delivery as rd
    paths = rd.run({"action": "reports", "input": {...}, "out_dir": "reports"})
    pkg   = rd.run({"action": "package", "root": ".", "out_zip": "delivery.zip", "run_tests": True})
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from . import __version__
from .evidence import collect_test_evidence
from .packager import PackageOptions, build_delivery_package
from .reports import build_reports

PLUGIN_INFO: Dict[str, Any] = {
    "name": "report_delivery", "display_name": "Reports and Delivery", "version": __version__, "phase": 10,
    "entry_point": "run", "actions": ["reports", "package", "all"],
    "capabilities": ["html-report", "json-report", "markdown-summary", "dependency-report", "static-analysis-report",
                     "security-report", "build-test-report", "environment-report", "delivery-package", "secret-scan", "sha256-manifest"],
    "executes_project_code": False, "network_access": False, "requires": [],
    "notes": "run_tests=True executes only the delivered application's own test suite, never uploaded projects.",
}


def run(request: Mapping[str, Any]) -> Dict[str, Any]:
    """request: action = reports | package | all.
    reports : input (dict), out_dir, [collect_env, probe_tools, title]
    package : root, out_zip, [run_tests, test_command, options (PackageOptions or dict), reports_dir]
    all     : both; reports are written to out_dir and included in the package."""
    action = request.get("action", "all")
    if action not in PLUGIN_INFO["actions"]:
        raise ValueError(f"Unknown action {action!r}; use one of {PLUGIN_INFO['actions']}")
    out: Dict[str, Any] = {}
    reports_dir: Optional[str] = request.get("reports_dir")
    if action in ("reports", "all"):
        if "input" not in request or "out_dir" not in request:
            raise ValueError("'reports' needs 'input' and 'out_dir'")
        out["reports"] = build_reports(request["input"], request["out_dir"], collect_env=request.get("collect_env", True),
                                       probe_tools=request.get("probe_tools", True), title=request.get("title"))
        reports_dir = str(request["out_dir"])
    if action in ("package", "all"):
        if "root" not in request or "out_zip" not in request:
            raise ValueError("'package' needs 'root' and 'out_zip'")
        opts = request.get("options")
        opts = opts if isinstance(opts, PackageOptions) else PackageOptions(**(opts or {}))
        if reports_dir and not opts.reports_dir:
            opts.reports_dir = reports_dir
        if request.get("run_tests") and not opts.evidence:
            opts.evidence = collect_test_evidence(request["root"], request.get("test_command"), request.get("test_timeout", 600))
            out["test_evidence"] = {k: opts.evidence[k] for k in ("command", "status", "exit_code", "duration_s")}
        out["package"] = build_delivery_package(request["root"], request["out_zip"], opts)
    return out


def register(registry: Any) -> None:
    name = PLUGIN_INFO["name"]
    for meth in ("register_analyzer", "register", "add_analyzer"):
        fn = getattr(registry, meth, None)
        if callable(fn):
            fn(name, run)
            return
    if isinstance(registry, dict):
        registry[name] = run
        return
    if isinstance(registry, list):
        registry.append({**PLUGIN_INFO, "callable": run})
        return
    raise TypeError("Unsupported registry type; call report_delivery.run(...) directly.")
