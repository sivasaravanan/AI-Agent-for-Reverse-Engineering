"""CI gate: analyse one project, apply a policy, write SARIF + SBOM + JSON summary, return an exit code.

Exit codes: 0 pass, 1 policy failed, 2 error (could not analyse / bad policy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from core.analysis_orchestrator import analyze_archive
from enterprise.audit import AuditLog
from enterprise.licenses import scan_licenses
from enterprise.policy import PolicyError, default_policy, evaluate_policy, load_policy
from enterprise.sarif import to_sarif
from enterprise.sbom import build_sbom


def run_ci(source, out_dir, policy_path: Optional[str] = None, audit: Optional[AuditLog] = None, actor: str = "ci", echo=print) -> int:
    from core.archive_manager import ArchiveError
    try:
        policy = load_policy(policy_path) if policy_path else default_policy()
        r = analyze_archive(source)
        lic = scan_licenses(source)
    except (PolicyError, ArchiveError, ValueError, OSError) as exc:
        echo(f"ERROR: {exc}")
        return 2
    pr = evaluate_policy(r, policy, lic)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ci.sarif").write_text(json.dumps(to_sarif(r), indent=2), encoding="utf-8")
    (out / "sbom.cdx.json").write_text(json.dumps(build_sbom(r, lic), indent=2), encoding="utf-8")
    (out / "policy_result.json").write_text(json.dumps(pr.to_dict(), indent=2), encoding="utf-8")
    if audit:
        audit.append("ci.gate", actor, r.archive_name, {"passed": pr.passed, "policy": policy.name, "summary": pr.summary})
    echo(f"Policy '{policy.name}': {'PASS' if pr.passed else 'FAIL'}  (critical {pr.summary['critical']}, high {pr.summary['high']}, medium {pr.summary['medium']})")
    for v in pr.violations[:20]:
        echo(f"  - {v['message']}" + (f" [{v.get('file')}:{v.get('line')}]" if v.get("file") else ""))
    echo(f"Outputs: {out / 'ci.sarif'}, {out / 'sbom.cdx.json'}, {out / 'policy_result.json'}")
    return 0 if pr.passed else 1
