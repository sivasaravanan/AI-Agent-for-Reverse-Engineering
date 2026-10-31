"""Licence detection (heuristic, not legal advice) from LICENSE files and package metadata inside the source, in memory."""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List

from core.input_adapter import prepared_input
from security.archive_validation import ArchiveLimits

# (SPDX id, regexes that must ALL match, lower-cased text). Order matters: more specific first.
_RULES = [
    ("AGPL-3.0", [r"gnu affero general public license", r"version 3"]),
    ("LGPL-3.0", [r"gnu lesser general public license", r"version 3"]),
    ("LGPL-2.1", [r"gnu lesser general public license", r"version 2\.1"]),
    ("GPL-3.0", [r"gnu general public license", r"version 3"]),
    ("GPL-2.0", [r"gnu general public license", r"version 2"]),
    ("Apache-2.0", [r"apache license", r"version 2\.0"]),
    ("MPL-2.0", [r"mozilla public license", r"2\.0"]),
    ("BSD-3-Clause", [r"redistribution and use in source and binary forms", r"neither the name"]),
    ("BSD-2-Clause", [r"redistribution and use in source and binary forms"]),
    ("ISC", [r"permission to use, copy, modify, and/or distribute this software"]),
    ("MIT", [r"permission is hereby granted, free of charge"]),
    ("Unlicense", [r"this is free and unencumbered software released into the public domain"]),
]
_SPDX_ALIASES = {"mit": "MIT", "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0", "apache2": "Apache-2.0", "bsd": "BSD-3-Clause",
                 "gpl": "GPL-3.0", "gplv3": "GPL-3.0", "gpl-3.0": "GPL-3.0", "gplv2": "GPL-2.0", "agpl": "AGPL-3.0", "isc": "ISC"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "MPL-2.0"}
_LIC_FILE = re.compile(r"(^|/)(licen[sc]e|copying|unlicense)(\.[a-z]+)?$", re.I)
MAX_FILE = 200_000


@dataclass
class LicenseInfo:
    project: List[Dict[str, str]] = field(default_factory=list)       # {file, license, how}
    components: Dict[str, str] = field(default_factory=dict)           # "ecosystem:name" -> SPDX/declared string
    notes: List[str] = field(default_factory=list)


def detect_license(text: str) -> str:
    """Return an SPDX id for well-known licence texts, or '' when unsure."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    for spdx, pats in _RULES:
        if all(re.search(p, t) for p in pats):
            return spdx
    return ""


def _norm(declared: str) -> str:
    d = (declared or "").strip()
    return _SPDX_ALIASES.get(d.lower(), d)[:80]


def scan_licenses(path, limits: ArchiveLimits | None = None) -> LicenseInfo:
    info = LicenseInfo()
    with prepared_input(path, limits or ArchiveLimits()) as prep:
        with zipfile.ZipFile(prep.zip_path) as z:
            for zi in z.infolist():
                n = zi.filename
                if zi.is_dir() or zi.file_size > MAX_FILE:
                    continue
                vendored = "node_modules/" in n or "vendor/" in n or "site-packages/" in n
                base = n.rsplit("/", 1)[-1].lower()
                try:
                    if _LIC_FILE.search(n) and not vendored:
                        lic = detect_license(z.read(n).decode("utf-8", "replace"))
                        info.project.append({"file": n, "license": lic or "unknown", "how": "licence text"})
                    elif base == "package.json":
                        d = json.loads(z.read(n).decode("utf-8", "replace"))
                        lic = d.get("license") if isinstance(d.get("license"), str) else ""
                        if isinstance(d.get("license"), dict):
                            lic = d["license"].get("type", "")
                        if vendored and d.get("name") and lic:
                            info.components[f"npm:{d['name']}"] = _norm(lic)
                        elif not vendored and lic:
                            info.project.append({"file": n, "license": _norm(lic), "how": "package.json"})
                    elif base == "composer.json" and not vendored:
                        d = json.loads(z.read(n).decode("utf-8", "replace"))
                        lic = d.get("license")
                        lic = ", ".join(lic) if isinstance(lic, list) else (lic or "")
                        if lic:
                            info.project.append({"file": n, "license": _norm(lic), "how": "composer.json"})
                    elif base in ("pyproject.toml", "setup.cfg", "setup.py") and not vendored:
                        m = re.search(r"""license\s*=\s*(?:\{\s*text\s*=\s*)?["']([^"'\n]{2,60})["']""", z.read(n).decode("utf-8", "replace"), re.I)
                        if m:
                            info.project.append({"file": n, "license": _norm(m.group(1)), "how": base})
                except (ValueError, KeyError, zipfile.BadZipFile, OSError):
                    info.notes.append(f"could not read {n}")
    if not info.project:
        info.notes.append("No project licence found - treat the code as all rights reserved until the owner states a licence.")
    info.notes.append("Declared dependency licences are only known for vendored packages that ship metadata; all others are 'NOASSERTION'. Not legal advice.")
    return info
