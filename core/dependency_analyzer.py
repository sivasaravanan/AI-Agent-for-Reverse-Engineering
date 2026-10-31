"""Dependency-manifest identification and (light) parsing.

Parsing is purely textual (json/tomllib/regex). No package manager is invoked
and nothing is installed.
"""
from __future__ import annotations

import json
import posixpath
import re
import tomllib
from typing import Dict, List, Optional, Tuple

from models.analysis_result import Dependency

_EXACT: Dict[str, Tuple[str, str]] = {
    "package.json": ("javascript", "manifest"),
    "package-lock.json": ("javascript", "lockfile"),
    "yarn.lock": ("javascript", "lockfile"),
    "pnpm-lock.yaml": ("javascript", "lockfile"),
    "tsconfig.json": ("typescript", "config"),
    "pyproject.toml": ("python", "manifest"),
    "setup.py": ("python", "manifest"),
    "setup.cfg": ("python", "manifest"),
    "pipfile": ("python", "manifest"),
    "pipfile.lock": ("python", "lockfile"),
    "poetry.lock": ("python", "lockfile"),
    "pom.xml": ("java", "manifest"),
    "go.mod": ("go", "manifest"),
    "go.sum": ("go", "lockfile"),
    "composer.json": ("php", "manifest"),
    "composer.lock": ("php", "lockfile"),
    "packages.config": ("dotnet", "manifest"),
    "gemfile": ("ruby", "manifest"),
    "dockerfile": ("container", "build"),
    "docker-compose.yml": ("container", "build"),
    "docker-compose.yaml": ("container", "build"),
}


def identify_manifest(path: str) -> Optional[Tuple[str, str]]:
    """Return (ecosystem, kind) when the path is a known manifest, else None."""
    base = posixpath.basename(path).lower()
    if base in _EXACT:
        return _EXACT[base]
    if re.fullmatch(r"requirements[\w.\-]*\.txt", base):
        return ("python", "manifest")
    if base.endswith((".csproj", ".vbproj")):
        return ("dotnet", "manifest")
    if base.endswith(".sln"):
        return ("dotnet", "build")
    if base.endswith((".gradle", ".gradle.kts")):
        return ("java", "manifest")
    return None


def _line_of(text: str, needle: str) -> Optional[int]:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else None


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:\[[^\]]*\])?\s*([<>=!~].*)?$")


def _parse_requirement(spec: str) -> Optional[Tuple[str, str]]:
    spec = spec.split("#", 1)[0].split(";", 1)[0].strip()
    if not spec or spec.startswith(("-", "git+", "http")):
        return None
    m = _REQ.match(spec)
    return (m.group(1), (m.group(2) or "").strip()) if m else None


def _py_requirements(path: str, text: str) -> List[Dependency]:
    deps = []
    for no, line in enumerate(text.splitlines(), 1):
        parsed = _parse_requirement(line)
        if parsed:
            deps.append(Dependency(parsed[0], parsed[1], path, no, "python"))
    return deps


def _pyproject(path: str, text: str, base: str) -> List[Dependency]:
    data = tomllib.loads(text)  # raises ValueError subclass on malformed input
    deps: List[Dependency] = []

    def add(raw: str, scope: str) -> None:
        parsed = _parse_requirement(raw)
        if parsed:
            deps.append(Dependency(parsed[0], parsed[1], path, _line_of(text, parsed[0]), "python", scope))

    if base == "pipfile":
        for scope, key in (("runtime", "packages"), ("dev", "dev-packages")):
            for name, ver in (data.get(key) or {}).items():
                deps.append(Dependency(name, ver if isinstance(ver, str) else json.dumps(ver), path,
                                       _line_of(text, name), "python", scope))
        return deps
    project = data.get("project") or {}
    for raw in project.get("dependencies") or []:
        add(raw, "runtime")
    for group, items in (project.get("optional-dependencies") or {}).items():
        for raw in items:
            add(raw, f"optional:{group}")
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for name, ver in (poetry.get("dependencies") or {}).items():
        if name.lower() != "python":
            spec = ver if isinstance(ver, str) else str((ver or {}).get("version", ""))
            deps.append(Dependency(name, spec, path, _line_of(text, name), "python"))
    return deps


def _package_json(path: str, text: str) -> List[Dependency]:
    data = json.loads(text)
    deps: List[Dependency] = []
    if not isinstance(data, dict):
        return deps
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev"),
                       ("peerDependencies", "peer"), ("optionalDependencies", "optional")):
        section = data.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                deps.append(Dependency(name, str(ver), path, _line_of(text, f'"{name}"'), "javascript", scope))
    return deps


def _composer(path: str, text: str) -> List[Dependency]:
    data = json.loads(text)
    deps: List[Dependency] = []
    if not isinstance(data, dict):
        return deps
    for key, scope in (("require", "runtime"), ("require-dev", "dev")):
        section = data.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                deps.append(Dependency(name, str(ver), path, _line_of(text, f'"{name}"'), "php", scope))
    return deps


def _line_no(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _dotnet(path: str, text: str) -> List[Dependency]:
    out = []
    for m in re.finditer(r'<PackageReference\s[^>]*?Include\s*=\s*"([^"]+)"(?:[^>]*?Version\s*=\s*"([^"]*)")?', text):
        out.append(Dependency(m.group(1), m.group(2) or "", path, _line_no(text, m.start()), "dotnet"))
    for m in re.finditer(r'<package\s[^>]*?id\s*=\s*"([^"]+)"(?:[^>]*?version\s*=\s*"([^"]*)")?', text):
        out.append(Dependency(m.group(1), m.group(2) or "", path, _line_no(text, m.start()), "dotnet"))
    return out


def _maven(path: str, text: str) -> List[Dependency]:
    out = []
    pat = (r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>"
           r"(?:\s*<version>([^<]+)</version>)?")
    for m in re.finditer(pat, text):
        out.append(Dependency(f"{m.group(1).strip()}:{m.group(2).strip()}", (m.group(3) or "").strip(),
                              path, _line_no(text, m.start()), "java"))
    return out


def _gradle(path: str, text: str) -> List[Dependency]:
    out = []
    pat = (r"\b(implementation|api|compile|testImplementation|runtimeOnly|compileOnly|testCompile)"
           r"\s*\(?\s*['\"]([^'\":]+):([^'\":]+)(?::([^'\"]+))?['\"]")
    for m in re.finditer(pat, text):
        scope = "dev" if m.group(1).lower().startswith("test") else "runtime"
        out.append(Dependency(f"{m.group(2)}:{m.group(3)}", m.group(4) or "", path,
                              _line_no(text, m.start()), "java", scope))
    return out


def _gomod(path: str, text: str) -> List[Dependency]:
    out, in_block = [], False
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if in_block:
            m = re.match(r"([\w.\-/]+)\s+(v[\w.\-+]+)", s)
        else:
            m = re.match(r"require\s+([\w.\-/]+)\s+(v[\w.\-+]+)", s)
        if m:
            out.append(Dependency(m.group(1), m.group(2), path, no, "go"))
    return out


def parse_dependencies(path: str, text: str) -> List[Dependency]:
    """Parse dependency declarations. Raises ValueError for malformed JSON/TOML."""
    base = posixpath.basename(path).lower()
    if re.fullmatch(r"requirements[\w.\-]*\.txt", base):
        return _py_requirements(path, text)
    if base in ("pyproject.toml", "pipfile"):
        return _pyproject(path, text, base)
    if base == "package.json":
        return _package_json(path, text)
    if base == "composer.json":
        return _composer(path, text)
    if base.endswith((".csproj", ".vbproj")) or base == "packages.config":
        return _dotnet(path, text)
    if base == "pom.xml":
        return _maven(path, text)
    if base.endswith((".gradle", ".gradle.kts")):
        return _gradle(path, text)
    if base == "go.mod":
        return _gomod(path, text)
    return []
