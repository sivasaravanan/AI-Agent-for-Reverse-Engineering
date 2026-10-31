"""Stage C: enterprise / audit / compliance - policy gates, licence + SBOM, tamper-evident audit log, trends, portfolio scans, CI.

Static only. Licence detection is a text heuristic and is NOT legal advice; unknown licences are reported as unknown.
"""
from enterprise.audit import AuditLog
from enterprise.licenses import LicenseInfo, detect_license, scan_licenses
from enterprise.policy import Policy, PolicyError, PolicyResult, default_policy, evaluate_policy, load_policy
from enterprise.sbom import build_sbom
from enterprise.sarif import to_sarif
from enterprise.trends import project_trend
from enterprise.portfolio import PortfolioResult, scan_portfolio
from enterprise.ci import run_ci

__all__ = ["AuditLog", "LicenseInfo", "detect_license", "scan_licenses", "Policy", "PolicyError", "PolicyResult", "default_policy",
           "evaluate_policy", "load_policy", "build_sbom", "to_sarif", "project_trend", "PortfolioResult", "scan_portfolio", "run_ci"]
