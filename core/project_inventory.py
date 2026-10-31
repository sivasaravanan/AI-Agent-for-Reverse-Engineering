"""Phase 2: directory tree, entry-point identification and configuration-file inventory.

Everything is derived from archive metadata and already-read text. No project command is run;
commands found in manifests (npm scripts, Dockerfile CMD) are only *recorded*.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import tomllib
from typing import Dict, List, Mapping, Optional, Sequence

from core.archive_manager import VENDOR_DIRS, ArchiveEntry
from models.project_info import ConfigFile, EntryPoint, TreeNode
from security.secret_scanner import redact

MAX_TREE_NODES = 3000
MAX_TREE_DEPTH = 12


# ------------------------------------------------------------------ tree
def build_tree(entries: Sequence[ArchiveEntry], max_nodes: int = MAX_TREE_NODES,
               max_depth: int = MAX_TREE_DEPTH) -> TreeNode:
    """Build a capped directory tree. Vendored folders are shown collapsed with their file counts."""
    root: Dict = {"name": "(archive root)", "path": "", "dir": True, "size": 0, "files": 0,
                  "vendored": False, "kids": {}}
    for e in entries:
        if e.skip_reason:
            continue
        parts = [p for p in e.name.split("/") if p]
        if not parts:
            continue
        node = root
        if not e.is_dir:
            root["files"] += 1
            root["size"] += e.size
        walked: List[str] = []
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            is_dir = (not last) or e.is_dir
            walked.append(part)
            kid = node["kids"].get(part)
            if kid is None:
                kid = node["kids"][part] = {"name": part, "path": "/".join(walked), "dir": is_dir,
                                            "size": 0, "files": 0, "vendored": False, "kids": {}}
            if not e.is_dir:
                kid["files"] += 1
                kid["size"] += e.size
            if is_dir and part.lower() in VENDOR_DIRS:
                kid["vendored"] = True
            node = kid
            if kid["vendored"]:
                break
    # breadth-first conversion with caps (iterative: no recursion limits)
    out_root = TreeNode(name=root["name"], path="", is_dir=True, size=root["size"], file_count=root["files"])
    queue = [(root, out_root, 0)]
    created = 1
    while queue:
        raw, out, depth = queue.pop(0)
        kids = sorted(raw["kids"].values(), key=lambda k: (not k["dir"], k["name"].lower()))
        if raw["vendored"] and out is not out_root:
            out.vendored = True
            out.truncated = bool(kids)
            continue
        for k in kids:
            if created >= max_nodes or depth + 1 > max_depth:
                out.truncated = True
                break
            child = TreeNode(name=k["name"], path=k["path"], is_dir=k["dir"], size=k["size"],
                             file_count=k["files"] if k["dir"] else 0, vendored=k["vendored"])
            out.children.append(child)
            created += 1
            queue.append((k, child, depth + 1))
    return out_root


# ------------------------------------------------------------------ entry points
_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*["']__main__["']""", re.M)
_CS_MAIN = re.compile(r"\bstatic\s+(?:async\s+)?(?:void|int|Task(?:<int>)?)\s+Main\s*\(")
_JAVA_MAIN = re.compile(r"public\s+static\s+void\s+main\s*\(\s*String")
_GO_MAIN = re.compile(r"^func\s+main\s*\(\s*\)", re.M)
_JS_NAMES = {"server.js", "app.js", "index.js", "main.js", "server.ts", "app.ts", "index.ts", "main.ts"}
_PY_NAMES = {"app.py", "main.py", "run.py", "server.py", "wsgi.py", "asgi.py"}


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _first(rx: "re.Pattern[str]", text: str):
    m = rx.search(text)
    return (_line_of(text, m.start()), m.group(0).strip()) if m else (None, "")


def find_entry_points(texts: Mapping[str, str], file_names: Sequence[str]) -> List[EntryPoint]:
    out: List[EntryPoint] = []

    def add(file, line, kind, lang, ev, conf):
        out.append(EntryPoint(file, line, kind, lang, redact(ev)[:160], conf))

    for name in file_names:
        base = posixpath.basename(name).lower()
        ext = posixpath.splitext(base)[1]
        text = texts.get(name)
        if text is None:
            continue
        if ext in (".py", ".pyw"):
            line, ev = _first(_PY_MAIN, text)
            if base == "__main__.py":
                add(name, 1, "python-package-main", "Python", "__main__.py", "strong")
            elif base == "manage.py" and "DJANGO_SETTINGS_MODULE" in text:
                add(name, 1, "django-manage", "Python", "manage.py (DJANGO_SETTINGS_MODULE)", "strong")
            elif line:
                add(name, line, "python-main-guard", "Python", ev, "strong")
            elif base in ("wsgi.py", "asgi.py"):
                add(name, 1, "wsgi-asgi-app", "Python", base, "moderate")
            elif base in _PY_NAMES:
                add(name, 1, "conventional-name", "Python", f"{base} (no __main__ guard)", "weak")
        elif ext in (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"):
            line, ev = _first(re.compile(r"\.listen\s*\("), text)
            if line:
                add(name, line, "server-listen", "JavaScript/TypeScript", ev, "moderate")
            elif base in _JS_NAMES:
                add(name, 1, "conventional-name", "JavaScript/TypeScript", base, "weak")
        elif ext == ".cs":
            line, ev = _first(_CS_MAIN, text)
            if line:
                add(name, line, "csharp-main", "C#", ev, "strong")
            line, ev = _first(re.compile(r"WebApplication\.CreateBuilder"), text)
            if line:
                add(name, line, "aspnet-host", "C#", ev, "strong")
            elif base == "program.cs":
                add(name, 1, "conventional-name", "C#", "Program.cs", "weak")
        elif ext == ".vb":
            line, ev = _first(re.compile(r"\bSub\s+Main\s*\(", re.I), text)
            if line:
                add(name, line, "vb-main", "VB.NET", ev, "strong")
        elif ext == ".java":
            line, ev = _first(_JAVA_MAIN, text)
            if line:
                add(name, line, "java-main", "Java", ev, "strong")
            line, ev = _first(re.compile(r"@SpringBootApplication"), text)
            if line:
                add(name, line, "spring-boot-app", "Java", ev, "strong")
        elif ext == ".go":
            if re.search(r"^package\s+main\b", text, re.M):
                line, ev = _first(_GO_MAIN, text)
                if line:
                    add(name, line, "go-main", "Go", ev, "strong")
        elif ext == ".php":
            if base == "index.php":
                add(name, 1, "conventional-name", "PHP", name, "moderate" if name.endswith("public/index.php") else "weak")
        elif base == "artisan":
            add(name, 1, "laravel-artisan", "PHP", "artisan", "moderate")
        if base == "package.json":
            _package_json_entries(name, text, add)
        elif base == "pyproject.toml":
            _pyproject_entries(name, text, add)
        elif base.endswith((".csproj", ".vbproj")):
            line, ev = _first(re.compile(r"<OutputType>\s*(?:Exe|WinExe)\s*</OutputType>"), text)
            if line:
                add(name, line, "dotnet-executable-project", ".NET", ev, "moderate")
        elif base == "dockerfile":
            for m in re.finditer(r"^(CMD|ENTRYPOINT)\s+(.+)$", text, re.M):
                add(name, _line_of(text, m.start()), f"container-{m.group(1).lower()}", "container", m.group(0), "moderate")
    return out


def _package_json_entries(name: str, text: str, add) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return

    def line(needle: str) -> Optional[int]:
        i = text.find(needle)
        return _line_of(text, i) if i >= 0 else None

    if isinstance(data.get("main"), str):
        add(name, line('"main"'), "package-main", "JavaScript/TypeScript", f'main: {data["main"]}', "strong")
    bins = data.get("bin")
    if isinstance(bins, str):
        add(name, line('"bin"'), "package-bin", "JavaScript/TypeScript", f"bin: {bins}", "strong")
    elif isinstance(bins, dict):
        for k, v in list(bins.items())[:10]:
            add(name, line('"bin"'), "package-bin", "JavaScript/TypeScript", f"bin {k}: {v}", "strong")
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for key in ("start", "dev", "serve"):
            if isinstance(scripts.get(key), str):
                add(name, line(f'"{key}"'), "npm-script (recorded, not run)", "JavaScript/TypeScript",
                    f"{key}: {scripts[key]}", "moderate")


def _pyproject_entries(name: str, text: str, add) -> None:
    try:
        data = tomllib.loads(text)
    except ValueError:
        return
    scripts = dict((data.get("project") or {}).get("scripts") or {})
    scripts.update((data.get("tool") or {}).get("poetry", {}).get("scripts") or {})
    for key, target in list(scripts.items())[:20]:
        i = text.find(key)
        add(name, _line_of(text, i) if i >= 0 else None, "console-script", "Python", f"{key} = {target}", "strong")


# ------------------------------------------------------------------ configuration inventory
_CONFIG_RULES = [
    (lambda b, p: b.startswith(".env"), "environment file"),
    (lambda b, p: fnmatch.fnmatch(b, "appsettings*.json"), ".NET app settings"),
    (lambda b, p: b in ("web.config", "app.config"), ".NET config"),
    (lambda b, p: fnmatch.fnmatch(b, "application*.yml") or fnmatch.fnmatch(b, "application*.yaml")
        or fnmatch.fnmatch(b, "application*.properties"), "Spring config"),
    (lambda b, p: fnmatch.fnmatch(b, "docker-compose*"), "docker compose"),
    (lambda b, p: b == "dockerfile", "Dockerfile"),
    (lambda b, p: b in ("settings.py", "config.py", "config.js", "config.json", "settings.json", "config.yml",
                        "config.yaml", "config.php", "database.php", "database.yml", "knexfile.js"), "application config"),
    (lambda b, p: b == "pyproject.toml" or b in ("setup.cfg", "tox.ini", "pytest.ini", ".flake8"), "tooling config"),
    (lambda b, p: b in ("tsconfig.json", "jsconfig.json", ".eslintrc", ".eslintrc.json", ".prettierrc", "vite.config.js",
                        "vite.config.ts", "webpack.config.js"), "tooling config"),
    (lambda b, p: b.endswith((".ini", ".cfg", ".conf", ".properties")), "configuration file"),
    (lambda b, p: p.startswith((".github/workflows/", ".gitlab-ci")) or b == ".gitlab-ci.yml", "CI workflow"),
    (lambda b, p: b.endswith((".json", ".yml", ".yaml", ".xml", ".toml"))
        and any(seg in ("config", "conf", "settings", "configs") for seg in p.split("/")[:-1]), "configuration file"),
]


def inventory_config_files(entries: Sequence[ArchiveEntry]) -> List[ConfigFile]:
    out: List[ConfigFile] = []
    for e in entries:
        if e.is_dir or e.skip_reason or e.vendored:
            continue
        base, low = e.basename.lower(), e.name.lower()
        for test, kind in _CONFIG_RULES:
            if test(base, low):
                out.append(ConfigFile(e.name, kind, e.size))
                break
    out.sort(key=lambda c: (c.kind, c.path))
    return out
