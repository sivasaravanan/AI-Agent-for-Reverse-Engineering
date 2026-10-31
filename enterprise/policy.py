"""Policy gates: a small JSON policy decides pass/fail for a project. Deterministic, no code execution."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from enterprise.licenses import COPYLEFT, LicenseInfo
from models.analysis_result import AnalysisResult


class PolicyError(ValueError):
    pass


@dataclass
class Policy:
    name: str = "default"
    max_critical: int = 0
    max_high: int = 0
    max_medium: Optional[int] = None
    forbid_rules: List[str] = field(default_factory=list)          # rule ids that always fail (prefix match with *)
    forbid_secrets: bool = True                                     # any finding whose category/rule indicates a hard-coded secret
    forbid_licenses: List[str] = field(default_factory=list)        # SPDX ids
    forbid_copyleft: bool = False
    require_project_license: bool = False
    max_hotspots: Optional[int] = None
    min_confidence_for_gate: str = "low"                            # findings below this confidence are listed but do not fail
    allow_rules: List[str] = field(default_factory=list)            # waived rule ids (recorded in the result, never hidden)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyResult:
    passed: bool
    policy: str
    violations: List[Dict[str, Any]] = field(default_factory=list)
    waived: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def default_policy() -> Policy:
    return Policy()


_CONF = {"low": 0, "medium": 1, "high": 2, "confirmed": 3}


def load_policy(path) -> Policy:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"Policy file could not be read ({exc}).")
    if not isinstance(data, dict):
        raise PolicyError("Policy must be a JSON object.")
    known = set(Policy.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise PolicyError(f"Unknown policy key(s): {', '.join(sorted(unknown))}")
    p = Policy()
    for k, v in data.items():
        exp = type(getattr(p, k))
        if k in ("max_medium", "max_hotspots") and v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise PolicyError(f"{k} must be a non-negative integer or null")
        elif k in ("max_critical", "max_high"):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise PolicyError(f"{k} must be a non-negative integer")
        elif exp is list and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise PolicyError(f"{k} must be a list of strings")
        elif exp is bool and not isinstance(v, bool):
            raise PolicyError(f"{k} must be true or false")
        if k == "min_confidence_for_gate" and v not in _CONF:
            raise PolicyError("min_confidence_for_gate must be low, medium, high or confirmed")
        setattr(p, k, v)
    return p


def _match(rule: str, patterns: List[str]) -> bool:
    return any(rule == p or (p.endswith("*") and rule.startswith(p[:-1])) for p in patterns)


def _is_secret(f) -> bool:
    s = f"{f.rule_id} {f.category}".lower()
    return "secret" in s or "hardcoded-cred" in s or "hard-coded" in s


def evaluate_policy(result: AnalysisResult, policy: Policy, licenses: Optional[LicenseInfo] = None) -> PolicyResult:
    v: List[Dict[str, Any]] = []
    waived: List[Dict[str, Any]] = []
    floor = _CONF.get(policy.min_confidence_for_gate, 0)
    counted = []
    for f in result.findings:
        if _match(f.rule_id, policy.allow_rules):
            waived.append({"rule": f.rule_id, "file": f.file, "line": f.line, "reason": "waived by policy allow_rules"})
        elif _CONF.get(f.confidence, 0) >= floor:
            counted.append(f)
    n = {s: sum(1 for f in counted if f.severity == s) for s in ("critical", "high", "medium")}
    for sev, limit in (("critical", policy.max_critical), ("high", policy.max_high), ("medium", policy.max_medium)):
        if limit is not None and n[sev] > limit:
            v.append({"check": f"max_{sev}", "message": f"{n[sev]} {sev} finding(s); limit is {limit}", "count": n[sev]})
    for f in counted:
        if _match(f.rule_id, policy.forbid_rules):
            v.append({"check": "forbid_rules", "message": f"forbidden rule {f.rule_id}", "file": f.file, "line": f.line})
        elif policy.forbid_secrets and _is_secret(f):
            v.append({"check": "forbid_secrets", "message": f"hard-coded secret indicator ({f.rule_id})", "file": f.file, "line": f.line})
    if licenses is not None:
        ids = {p["license"] for p in licenses.project} | set(licenses.components.values())
        for lic in sorted(ids):
            if lic in policy.forbid_licenses or (policy.forbid_copyleft and lic in COPYLEFT):
                v.append({"check": "licenses", "message": f"licence {lic} is not allowed by policy"})
        if policy.require_project_license and not any(p["license"] not in ("unknown", "") for p in licenses.project):
            v.append({"check": "require_project_license", "message": "no recognisable project licence found"})
    elif policy.forbid_licenses or policy.forbid_copyleft or policy.require_project_license:
        v.append({"check": "licenses", "message": "licence checks requested but no licence scan was provided"})
    hot = len((result.code_map or {}).get("hotspots", []))
    if policy.max_hotspots is not None and hot > policy.max_hotspots:
        v.append({"check": "max_hotspots", "message": f"{hot} complexity hotspot(s); limit is {policy.max_hotspots}", "count": hot})
    return PolicyResult(passed=not v, policy=policy.name, violations=v, waived=waived,
                        summary={"critical": n["critical"], "high": n["high"], "medium": n["medium"], "hotspots": hot,
                                 "considered": len(counted), "waived": len(waived)})
