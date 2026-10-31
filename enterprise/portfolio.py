"""Portfolio scan: analyse every project in a folder (zip/tar/sub-folder each), apply one policy, export CSV/JSON/HTML + SBOMs."""
from __future__ import annotations

import csv
import html
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.analysis_orchestrator import AnalysisCancelled, analyze_archive
from core.input_adapter import TAR_SUFFIXES
from education.reports import csv_safe
from enterprise.audit import AuditLog
from enterprise.licenses import scan_licenses
from enterprise.policy import Policy, default_policy, evaluate_policy
from enterprise.sbom import build_sbom
from security.archive_validation import safe_display

MAX_PROJECTS = 500


@dataclass
class PortfolioResult:
    projects: List[Dict[str, Any]] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)
    policy: str = ""

    @property
    def failed(self) -> int:
        return sum(1 for p in self.projects if p["status"] != "pass")


def discover_projects(folder) -> List[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError("The portfolio path must be a folder.")
    out = []
    for p in sorted(root.iterdir()):
        n = p.name.lower()
        if p.is_symlink() or n.startswith("."):
            continue
        if p.is_dir() or n.endswith(".zip") or n.endswith(tuple(TAR_SUFFIXES)):
            out.append(p)
    if not out:
        raise ValueError("No project archives or folders were found in that folder.")
    if len(out) > MAX_PROJECTS:
        raise ValueError(f"Too many projects ({len(out)}); the limit is {MAX_PROJECTS}.")
    return out


def scan_portfolio(folder, out_dir, policy: Optional[Policy] = None, audit: Optional[AuditLog] = None, actor: str = "system",
                   progress: Optional[Callable[[float, str], None]] = None, cancel: Optional[threading.Event] = None) -> PortfolioResult:
    policy = policy or default_policy()
    projects = discover_projects(folder)
    out = Path(out_dir)
    (out / "sbom").mkdir(parents=True, exist_ok=True)
    res = PortfolioResult(policy=policy.name)
    for i, p in enumerate(projects):
        if cancel is not None and cancel.is_set():
            raise AnalysisCancelled()
        if progress:
            progress(i / len(projects), f"Scanning {safe_display(p.name)}")
        row: Dict[str, Any] = {"project": safe_display(p.name), "status": "error", "critical": 0, "high": 0, "medium": 0, "findings": 0,
                               "licenses": "", "violations": "", "error": ""}
        try:
            r = analyze_archive(p)
            lic = scan_licenses(p)
            pr = evaluate_policy(r, policy, lic)
            sbom = build_sbom(r, lic)
            sp = out / "sbom" / f"{i + 1:03d}_{''.join(c if c.isalnum() or c in '-_' else '_' for c in p.stem)[:60]}.cdx.json"
            sp.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
            row.update(status="pass" if pr.passed else "fail", critical=pr.summary["critical"], high=pr.summary["high"],
                       medium=pr.summary["medium"], findings=len(r.findings), components=len(sbom["components"]),
                       licenses=", ".join(sorted({x["license"] for x in lic.project})) or "none found",
                       violations="; ".join(v["message"] for v in pr.violations[:5]), sbom=sp.name)
        except AnalysisCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken project never stops the portfolio
            row["error"] = safe_display(str(exc))[:200]
        res.projects.append(row)
        if audit:
            audit.append("portfolio.scan", actor, row["project"], {"status": row["status"], "high": row["high"], "policy": policy.name})
    cols = ["project", "status", "critical", "high", "medium", "findings", "licenses", "violations", "error"]
    with open(out / "portfolio.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in res.projects:
            w.writerow([csv_safe(r.get(c, "")) for c in cols])
    (out / "portfolio.json").write_text(json.dumps({"policy": policy.to_dict(), "projects": res.projects}, indent=2), encoding="utf-8")
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in res.projects)
    (out / "portfolio.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Portfolio scan</title><h1>Portfolio scan</h1>"
        f"<p>Policy: {html.escape(policy.name)}. {len(res.projects)} project(s), {res.failed} not passing. Static analysis only; licences are heuristic, not legal advice.</p>"
        f"<table border=1 cellpadding=4><tr>{''.join(f'<th>{c}</th>' for c in cols)}</tr>{rows}</table>", encoding="utf-8")
    res.files = {k: str(out / v) for k, v in {"csv": "portfolio.csv", "json": "portfolio.json", "html": "portfolio.html"}.items()}
    if progress:
        progress(1.0, "Done")
    return res
