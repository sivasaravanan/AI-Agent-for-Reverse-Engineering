"""CycloneDX 1.5 SBOM (JSON) built from the manifests the analysis already found. Static: nothing is installed or resolved."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core import __version__
from enterprise.licenses import LicenseInfo
from models.analysis_result import AnalysisResult

_PURL = {"python": "pypi", "pip": "pypi", "pypi": "pypi", "npm": "npm", "node": "npm", "javascript": "npm", "maven": "maven",
         "nuget": "nuget", ".net": "nuget", "composer": "composer", "php": "composer", "go": "golang", "cargo": "cargo", "rubygems": "gem"}


def _version(spec: str) -> str:
    m = re.search(r"(\d+(?:\.\d+){0,3}(?:[-+.\w]*)?)", spec or "")
    return m.group(1) if m and re.match(r"^\s*(==|=|~=|\^|~|>=)?\s*v?\d", spec or "") else ""


def build_sbom(result: AnalysisResult, licenses: Optional[LicenseInfo] = None, serial: Optional[str] = None) -> Dict[str, Any]:
    comps, seen = [], set()
    for m in result.manifests:
        for d in m.dependencies:
            key = (m.ecosystem.lower(), d.name.lower(), d.spec)
            if key in seen:
                continue
            seen.add(key)
            eco = _PURL.get(m.ecosystem.lower(), "generic")
            ver = _version(d.spec)
            comp: Dict[str, Any] = {"type": "library", "name": d.name, "bom-ref": f"{eco}:{d.name}:{ver or d.spec or 'unspecified'}",
                                    "purl": f"pkg:{eco}/{d.name}" + (f"@{ver}" if ver else ""),
                                    "properties": [{"name": "ase:declared-in", "value": m.path}, {"name": "ase:version-spec", "value": d.spec or ""},
                                                   {"name": "ase:scope", "value": d.scope}]}
            if ver:
                comp["version"] = ver
            lic = (licenses.components.get(f"{eco}:{d.name}") if licenses else None)
            comp["licenses"] = [{"license": {"id": lic}}] if lic and re.match(r"^[A-Za-z0-9.\-+]+$", lic) else \
                               ([{"license": {"name": lic}}] if lic else [{"license": {"name": "NOASSERTION"}}])
            comps.append(comp)
    meta: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "tools": [{"vendor": "AI Software Engineering Agent", "name": "ase", "version": __version__}],
                            "component": {"type": "application", "name": result.archive_name or "project", "bom-ref": "root"}}
    if licenses and licenses.project:
        meta["component"]["licenses"] = [{"license": {"name": p["license"]}} for p in licenses.project[:5]]
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": serial or f"urn:uuid:{uuid.uuid4()}", "version": 1,
            "metadata": meta, "components": comps,
            "properties": [{"name": "ase:note", "value": "Built from declared manifests only; transitive dependencies appear only if a lockfile lists them."}]}
