"""Dependency-manifest parsers. Text is parsed only - nothing is installed or executed."""
from __future__ import annotations

import json
import re
from typing import Iterator, List, Optional, Tuple

from .context import FileContext, manifest_kind

Dep = Tuple[str, str, int, str, bool]  # name, version, line, ecosystem, dev

_PEP508_ITEM = re.compile(r"""^\s*["']([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*([<>=!~^;][^"']*)?["']\s*,?\s*(?:#.*)?$""")
_POETRY = re.compile(r"""^\s*([A-Za-z0-9_.\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{[^}]*version\s*=\s*"([^"]*)")""")
_QUOTED_REQ = re.compile(r"""["']([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*([<>=!~][^"']*)?["']""")
_GRADLE = re.compile(r"""["']([\w.\-]+):([\w.\-]+)(?::([^"']+))?["']""")
_CSPROJ = re.compile(r"""<Package(?:Reference|Version)\b[^>]*?Include\s*=\s*"([^"]+)"(?:[^>]*?Version\s*=\s*"([^"]*)")?""", re.I)
_PKGCFG = re.compile(r"""<package\b[^>]*?\bid\s*=\s*"([^"]+)"(?:[^>]*?\bversion\s*=\s*"([^"]*)")?""", re.I)
_GOMOD = re.compile(r"""^\s*(?:require\s+)?([\w.\-~]+\.[\w.\-~]+/[\w.\-/~]+)\s+(v\d[\w.\-+]*)""")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _find_line(lines: List[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return start + 1


def _requirements(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.lines, 1):
        s = l.strip()
        if not s or s.startswith(("#", "-", "git+", "http")):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(.*)", s)
        if m:
            yield m.group(1).lower(), m.group(2).split("#")[0].split(";")[0].strip(), n, "pip", False


def _pyproject(ctx: FileContext) -> Iterator[Dep]:
    section = ""
    for n, l in enumerate(ctx.lines, 1):
        s = l.strip()
        hdr = re.match(r"^\[([^\]\"']+)\]$", s)
        if hdr:
            section = hdr.group(1).lower()
            continue
        dev = "dev" in section
        if re.match(r"^(?:[\w\-]+\s*=\s*)?\[", s) and "dependencies" in s.lower() and "]" in s:
            for m in _QUOTED_REQ.finditer(s):
                yield m.group(1).lower(), (m.group(2) or "").strip(), n, "pip", dev
            continue
        m = _PEP508_ITEM.match(l)
        if m:
            yield m.group(1).lower(), (m.group(2) or "").strip(), n, "pip", dev
            continue
        if "dependencies" in section:
            p = _POETRY.match(l)
            if p:
                yield p.group(1).lower(), (p.group(2) or p.group(3) or p.group(4) or ""), n, "pip", dev


def _pipfile(ctx: FileContext) -> Iterator[Dep]:
    section = ""
    for n, l in enumerate(ctx.lines, 1):
        s = l.strip()
        h = re.match(r"^\[([^\]]+)\]$", s)
        if h:
            section = h.group(1).lower()
            continue
        if section in ("packages", "dev-packages"):
            m = re.match(r"""^([A-Za-z0-9_.\-]+)\s*=\s*(.*)$""", s)
            if m:
                yield m.group(1).lower(), m.group(2).strip('"\' '), n, "pip", section == "dev-packages"


def _setup_py(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.code, 1):
        for m in _QUOTED_REQ.finditer(l):
            yield m.group(1).lower(), (m.group(2) or "").strip(), n, "pip", False


def _setup_cfg(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.lines, 1):
        m = re.match(r"^\s+([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*([<>=!~].*)?$", l)
        if m:
            yield m.group(1).lower(), (m.group(2) or "").strip(), n, "pip", False


def _package_json(ctx: FileContext) -> Iterator[Dep]:
    try:
        data = json.loads(ctx.text)
    except ValueError:
        for n, l in enumerate(ctx.lines, 1):
            m = re.match(r"""^\s*"([@\w./\-]+)"\s*:\s*"([^"]*)"\s*,?\s*$""", l)
            if m:
                yield m.group(1).lower(), m.group(2), n, "npm", False
        return
    if not isinstance(data, dict):
        return
    for sec, dev in (("dependencies", False), ("devDependencies", True), ("optionalDependencies", False),
                     ("peerDependencies", False)):
        block = data.get(sec)
        if not isinstance(block, dict):
            continue
        start = _find_line(ctx.lines, f'"{sec}"') - 1
        for name, ver in block.items():
            yield str(name).lower(), str(ver), _find_line(ctx.lines, f'"{name}"', start), "npm", dev


def _pom(ctx: FileContext) -> Iterator[Dep]:
    for m in re.finditer(r"<(dependency|plugin)>(.*?)</\1>", ctx.text, re.S):
        body = m.group(2)
        art = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        if not art:
            continue
        grp = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", body)
        ver = re.search(r"<version>\s*([^<\s]+)\s*</version>", body)
        name = f"{(grp.group(1) if grp else '')}:{art.group(1)}".lower()
        dev = bool(re.search(r"<scope>\s*test\s*</scope>", body))
        yield name, ver.group(1) if ver else "", _line_of(ctx.text, m.start(2) + art.start()), "maven", dev


def _gradle(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.code, 1):
        for m in _GRADLE.finditer(l):
            dev = bool(re.match(r"\s*(?:test|androidTest)\w*", l))
            yield f"{m.group(1)}:{m.group(2)}".lower(), m.group(3) or "", n, "gradle", dev


def _csproj(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.code, 1):
        for m in _CSPROJ.finditer(l):
            yield m.group(1).lower(), m.group(2) or "", n, "nuget", False


def _packages_config(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.code, 1):
        for m in _PKGCFG.finditer(l):
            yield m.group(1).lower(), m.group(2) or "", n, "nuget", False


def _composer(ctx: FileContext) -> Iterator[Dep]:
    try:
        data = json.loads(ctx.text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    for sec, dev in (("require", False), ("require-dev", True)):
        block = data.get(sec)
        if isinstance(block, dict):
            start = _find_line(ctx.lines, f'"{sec}"') - 1
            for name, ver in block.items():
                yield str(name).lower(), str(ver), _find_line(ctx.lines, f'"{name}"', start), "composer", dev


def _gomod(ctx: FileContext) -> Iterator[Dep]:
    for n, l in enumerate(ctx.code, 1):
        m = _GOMOD.match(l)
        if m:
            yield m.group(1).lower(), m.group(2), n, "go", False


_PARSERS = {
    "requirements": _requirements, "pyproject": _pyproject, "pipfile": _pipfile, "setup_py": _setup_py,
    "setup_cfg": _setup_cfg, "package_json": _package_json, "pom": _pom, "gradle": _gradle,
    "csproj": _csproj, "packages_config": _packages_config, "composer": _composer, "gomod": _gomod,
}


def parse_manifest(ctx: FileContext) -> List[Dep]:
    kind: Optional[str] = manifest_kind(ctx.path)
    parser = _PARSERS.get(kind or "")
    if not parser:
        return []
    try:
        return list(parser(ctx))
    except Exception:  # noqa: BLE001 - a malformed manifest must never abort the analysis
        return []
