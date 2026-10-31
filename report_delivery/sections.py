"""Normalises the host's analysis data into report sections. Nothing is invented: a section that was not
provided is marked status='not-provided' and the reports say so explicitly."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping

SEV_ORDER = ["critical", "high", "medium", "low", "info"]
SEV_RANK = {s: i for i, s in enumerate(reversed(SEV_ORDER), 1)}
KINDS = {"parser_error", "compiler_error", "lint_warning", "heuristic", "runtime_error", "test_failure"}


def _list(v: Any) -> List[Any]:
    return list(v) if isinstance(v, (list, tuple)) else []


def _dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, Mapping) else {}


def normalize_input(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Accepts the loose dict produced by the orchestrator; returns a fully-keyed structure."""
    raw = dict(raw or {})
    findings = [dict(f) for f in _list(raw.get("findings")) if isinstance(f, Mapping)]
    db = _dict(raw.get("database_analysis"))
    seen = {(f.get("id"), f.get("file"), f.get("line"), f.get("title")) for f in findings}
    for f in _list(db.get("findings_phase6")) or _list(db.get("findings")):
        if isinstance(f, Mapping):
            d = dict(f)
            d.setdefault("source", "database_analysis")
            key = (d.get("id"), d.get("file"), d.get("line"), d.get("title"))
            if key not in seen:
                seen.add(key)
                findings.append(d)
    for f in findings:
        f["severity"] = str(f.get("severity") or "info").lower()
        f.setdefault("verification_status", "unverified")
    static = _dict(raw.get("static_analysis"))
    bt = _dict(raw.get("build_test"))
    return {
        "project": _dict(raw.get("project")),
        "inventory": _dict(raw.get("inventory")),
        "dependencies": [dict(d) for d in _list(raw.get("dependencies")) if isinstance(d, Mapping)],
        "static_analysis": {"results": [dict(r) for r in _list(static.get("results")) if isinstance(r, Mapping)],
                            "skipped_reason": static.get("skipped_reason")},
        "build_test": {"runs": [dict(r) for r in _list(bt.get("runs")) if isinstance(r, Mapping)],
                       "skipped_reason": bt.get("skipped_reason")},
        "findings": sorted(findings, key=lambda f: (-SEV_RANK.get(f["severity"], 0), str(f.get("file") or ""), f.get("line") or 0)),
        "database_analysis": db,
        "environment": _dict(raw.get("environment")),
        "limitations": [str(x) for x in _list(raw.get("limitations"))],
        "known_issues": [str(x) for x in _list(raw.get("known_issues"))],
        "extra_next_steps": [str(x) for x in _list(raw.get("next_steps"))],
    }


def section_status(n: Dict[str, Any]) -> Dict[str, str]:
    inv = "provided" if n["inventory"] else "not-provided"
    deps = "provided" if n["dependencies"] else "not-provided"
    sa = n["static_analysis"]
    static = "provided" if sa["results"] else ("skipped" if sa["skipped_reason"] else "not-provided")
    bt = n["build_test"]
    build = "provided" if bt["runs"] else ("skipped" if bt["skipped_reason"] else "not-provided")
    return {"inventory": inv, "dependencies": deps, "static_analysis": static, "build_test": build,
            "findings": "provided" if n["findings"] else "none-reported",
            "database_analysis": "provided" if n["database_analysis"] else "not-provided"}


def build_summary(n: Dict[str, Any]) -> Dict[str, Any]:
    sev = Counter(f["severity"] for f in n["findings"])
    ver = Counter(str(f.get("verification_status")) for f in n["findings"])
    runs = n["build_test"]["runs"]
    statuses = Counter(str(r.get("status") or ("passed" if r.get("exit_code") == 0 else "failed" if r.get("exit_code") is not None else "unknown")) for r in runs)
    return {"findings_total": len(n["findings"]), "findings_by_severity": {s: sev.get(s, 0) for s in SEV_ORDER},
            "findings_by_verification": dict(ver), "dependencies_total": len(n["dependencies"]),
            "static_analyzers_run": len(n["static_analysis"]["results"]),
            "build_test_runs": dict(statuses), "sections": section_status(n)}


def recommend_next_steps(n: Dict[str, Any]) -> List[str]:
    """Rule-based and traceable to the data; every step names why it is recommended."""
    steps: List[str] = []
    st = section_status(n)
    sev = Counter(f["severity"] for f in n["findings"])
    if sev.get("critical") or sev.get("high"):
        steps.append(f"Review the {sev.get('critical', 0)} critical and {sev.get('high', 0)} high severity findings first.")
    unverified = [f for f in n["findings"] if str(f.get("verification_status", "")).lower().startswith(("unverified", "static-detected", "detected"))]
    if unverified:
        steps.append(f"{len(unverified)} finding(s) are detected-but-unverified; confirm them manually or with tests before acting.")
    if st["build_test"] == "not-provided":
        steps.append("No build/test results were provided: run the project's tests in an isolated environment (Phase 4) to verify behaviour.")
    elif st["build_test"] == "skipped":
        steps.append(f"Build/test was skipped ({n['build_test']['skipped_reason']}); no runtime verification exists yet.")
    else:
        failed = [r for r in n["build_test"]["runs"] if str(r.get("status")) in ("failed", "timeout", "error") or (r.get("exit_code") not in (0, None))]
        if failed:
            steps.append(f"{len(failed)} build/test run(s) did not succeed; fix the root cause and re-run.")
    if st["static_analysis"] == "not-provided":
        steps.append("No language-specific static analysis results were provided (Phase 3); only heuristic findings are available.")
    if any(f.get("rule", "").startswith("DB-SEC-001") or "credential" in str(f.get("title", "")).lower() for f in n["findings"]):
        steps.append("Rotate any exposed database credentials and move secrets to environment variables or a secret store.")
    if any(str(f.get("rule", "")).startswith("DB-CFG-006") for f in n["findings"]):
        steps.append("Document the environment variables the project expects (for example in a .env.example file).")
    steps += n["extra_next_steps"]
    if not steps:
        steps.append("No blocking issues were reported by the supplied data; keep tests and analysis in CI.")
    return steps
