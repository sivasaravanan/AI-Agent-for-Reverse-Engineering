"""Run targets: HOW A PROJECT CAN BE STARTED - the root entry file and any file that can be run on its own.

READ-ONLY, like the rest of discovery: file names and text are inspected (Python via ``ast.parse``, never imported or executed);
nothing is extracted or run. Every result is a normal ``Candidate``, so it goes through the same plan -> approval -> disposable
container path as test/build commands (project code never runs on this computer).

``discover_all(source)`` = the existing build/test candidates + run targets, with virtual-environment folders (``env/``, ``.venv/``,
anything holding a ``pyvenv.cfg``) left out of every scan. ``discover_candidates`` itself is unchanged, so automatic consumers
(patch verification, reports) keep using test/build commands only and never pick a run target by accident.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import sys
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from .discovery import ECOSYSTEM_ENV, LIFECYCLE, SUSPICIOUS, SKIP_DIRS, SourceView, _discover, _line_of, _rel
from .models import Candidate

MAX_SCAN_FILES = 400
MAX_TARGETS = 40
PY_ENTRY_PRIORITY = ("main.py", "app.py", "run.py", "__main__.py", "cli.py", "manage.py", "start.py", "server.py", "index.py")
NODE_ENTRY_NAMES = ("index.js", "server.js", "app.js", "main.js", "index.mjs", "server.mjs", "index.cjs")
GUI_MODULES = {"tkinter": "tkinter", "customtkinter": "customtkinter", "PySide6": "PySide6", "PySide2": "PySide2", "PyQt5": "PyQt5",
               "PyQt6": "PyQt6", "wx": "wxPython", "kivy": "Kivy", "flet": "Flet", "pygame": "pygame", "nicegui": "NiceGUI (web UI)",
               "streamlit": "Streamlit (web UI)", "flask": "Flask (web server)", "fastapi": "FastAPI (web server)",
               "uvicorn": "uvicorn (web server)", "django": "Django (web server)"}
WINDOW_GUIS = {"tkinter", "customtkinter", "PySide6", "PySide2", "PyQt5", "PyQt6", "wx", "kivy", "flet", "pygame"}
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
NPM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.\-]*$")
TEST_PATH = re.compile(r"(^|/)(tests?|testing|__tests__|docs?|examples?_?tests?)/|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$", re.I)
NOT_RUNNABLE = re.compile(r"(^|/)(setup\.py|__init__\.py|conftest\.py|noxfile\.py|fabfile\.py)$|/migrations/|/site-packages/", re.I)
MAIN_BLOCK = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.M)


# ---------------------------------------------------------------- virtual environments are not project code
def _is_venv_marker(name: str) -> bool:
    return PurePosixPath(name).name == "pyvenv.cfg"


class ProjectView(SourceView):
    """SourceView that also leaves out virtual-environment folders (any folder that contains ``pyvenv.cfg``)."""

    venvs: List[str]

    def __init__(self, source):
        self.venvs = []
        super().__init__(source)
        if self._zip is not None:                      # archives: find venv roots from the file list, then drop everything below them
            roots = sorted({str(PurePosixPath(n).parent) for n in self.names if _is_venv_marker(n)})
            roots = [r for r in roots if r != "."]
            if roots:
                self.venvs = roots
                self.names = [n for n in self.names if not any(n.startswith(r + "/") for r in roots)]

    def _walk(self) -> list:                           # folders: never descend into a venv (they can hold tens of thousands of files)
        out: list = []
        root = self.path
        for d, dirs, files in os.walk(root, followlinks=False):
            keep = []
            for x in dirs:
                p = os.path.join(d, x)
                if x in SKIP_DIRS or os.path.islink(p):
                    continue
                if os.path.isfile(os.path.join(p, "pyvenv.cfg")):
                    self.venvs.append(os.path.relpath(p, root).replace(os.sep, "/"))
                    continue
                keep.append(x)
            dirs[:] = keep
            for f in files:
                p = os.path.join(d, f)
                if not os.path.islink(p):
                    out.append(os.path.relpath(p, root).replace(os.sep, "/"))
                if len(out) >= 50_000:
                    return out
        return out


# ---------------------------------------------------------------- Python
def _py_facts(text: str) -> Dict[str, object]:
    """Top-level imports and GUI/server libraries from the syntax tree (the code is never executed)."""
    mods: Set[str] = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {"imports": mods, "parsed": False}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return {"imports": mods, "parsed": True}


def _local_modules(names: List[str]) -> Set[str]:
    out: Set[str] = set()
    for n in names:
        parts = PurePosixPath(n).parts
        if len(parts) == 1 and n.endswith(".py"):
            out.add(parts[0][:-3])
        elif len(parts) >= 2 and parts[0] not in ("src",):
            out.add(parts[0])
        if len(parts) >= 2 and parts[0] == "src":
            out.add(parts[1][:-3] if parts[1].endswith(".py") else parts[1])
    return out


def _py_candidates(src: ProjectView, names: List[str], root_files: Set[str], notes: List[str]) -> Tuple[List[Candidate], List[Candidate]]:
    """(root-entry candidates, other candidates)."""
    env = dict(ECOSYSTEM_ENV["python"])
    pys = [n for n in names if n.lower().endswith(".py") and not TEST_PATH.search(n) and not NOT_RUNNABLE.search(n)]
    local = _local_modules(names)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    root_hits: List[Tuple[int, Candidate]] = []
    other: List[Candidate] = []
    seen_ids: Set[str] = set()
    has_req = "requirements.txt" in root_files
    entry_summary: List[str] = []

    def add(bucket, cand: Candidate, rank: int = 0):
        if cand.id in seen_ids:
            return
        seen_ids.add(cand.id)
        bucket.append((rank, cand) if bucket is root_hits else cand)

    for n in sorted(pys, key=lambda x: (x.count("/"), x))[:MAX_SCAN_FILES]:
        text = src.read(n, 300_000)
        if text is None:
            continue
        base = PurePosixPath(n).name
        is_root = "/" not in n
        has_main = bool(MAIN_BLOCK.search(text))
        named_entry = is_root and base in PY_ENTRY_PRIORITY
        if not (has_main or named_entry):
            continue
        facts = _py_facts(text)
        imports: Set[str] = facts["imports"]  # type: ignore[assignment]
        third = sorted(m for m in imports if m not in stdlib and m not in local and not m.startswith("_"))
        guis = sorted({GUI_MODULES[m] for m in imports if m in GUI_MODULES})
        window = any(m in WINDOW_GUIS for m in imports)
        risks = ["Project code executes inside the container"]
        cnotes: List[str] = []
        if not has_main and named_entry:
            cnotes.append(f"{base} has no 'if __name__ == \"__main__\"' block: running it may only define things and exit.")
        if window:
            risks.append("Opens a window: the sandbox has no display, so it waits until the timeout (use the syntax check instead)")
            cnotes.append("This program opens a window (" + ", ".join(guis) + "). The sandbox has no display, so it cannot be shown "
                          "and will wait until the timeout; use the syntax check or a command-line entry instead.")
        elif guis:
            cnotes.append("Starts a long-running service (" + ", ".join(guis) + "): it runs until the timeout and is then stopped.")
        if third:
            cnotes.append("Imports packages that are not part of Python's standard library (" + ", ".join(third[:8]) +
                          ("..." if len(third) > 8 else "") + "). The stock image does not contain them and the network is off: "
                          "expect ImportError" + (", or use the 'install requirements' variant." if has_req else "."))
        kind = "root entry file" if is_root else "runnable file"
        desc = f"Run {n} ({kind}" + (", GUI" if window else "") + ")"
        cand = Candidate(f"python:run:{n}", "python", ["python", _rel(n)], desc, n, None, "entry-point", False, risks, cnotes, dict(env))
        rank = (PY_ENTRY_PRIORITY.index(base) if named_entry else len(PY_ENTRY_PRIORITY)) + (0 if is_root else 100)
        (add(root_hits, cand, rank) if is_root else add(other, cand))
        if is_root:
            entry_summary.append(f"{n}" + (" (opens a window)" if window else ""))
        if third and has_req:
            line = f"pip install --no-input --target /tmp/deps -r requirements.txt && PYTHONPATH=/tmp/deps python {shlex.quote(_rel(n))}"
            cand2 = Candidate(f"python:pip-run:{n}", "python", ["sh", "-c", line], f"Install requirements, then run {n}", n, None, "entry-point",
                              True, ["Network access is required (explicit approval)",
                                     "pip may build source distributions, which run arbitrary setup code (inside the container)",
                                     "Project code executes inside the container"], list(cnotes[:1] if window else []), dict(env))
            (add(root_hits, cand2, rank + 0.5) if is_root else add(other, cand2))
        if len(seen_ids) >= MAX_TARGETS:
            break

    # python -m <package>  (package/__main__.py at the project root)
    for n in sorted(names):
        parts = PurePosixPath(n).parts
        if len(parts) == 2 and parts[1] == "__main__.py" and IDENT.match(parts[0]) and parts[0] not in ("tests", "test"):
            add(root_hits, Candidate(f"python:module:{parts[0]}", "python", ["python", "-m", parts[0]], f"Run the package ({parts[0]}/__main__.py)",
                                     n, None, "entry-point", False, ["Project code executes inside the container"], [], dict(env)), 5)
            entry_summary.append(f"{parts[0]}/__main__.py (python -m {parts[0]})")

    # console scripts declared in pyproject.toml
    if "pyproject.toml" in root_files:
        try:
            import tomllib
            data = tomllib.loads(src.read("pyproject.toml") or "")
            scripts = dict((data.get("project") or {}).get("scripts") or {})
            scripts.update(((data.get("tool") or {}).get("poetry") or {}).get("scripts") or {})
        except Exception:  # noqa: BLE001 - malformed TOML only means "no scripts"
            scripts = {}
        for name, target in list(scripts.items())[:10]:
            m = re.match(r"^\s*([\w.]+)\s*:\s*(\w+)\s*$", str(target))
            if not m or not DOTTED.match(m.group(1)) or not IDENT.match(m.group(2)) or not re.match(r"^[\w.\-]+$", str(name)):
                continue
            code = f"import sys; from {m.group(1)} import {m.group(2)}; sys.exit({m.group(2)}())"
            add(other, Candidate(f"python:script:{name}", "python", ["python", "-c", code], f"Run the console script '{name}' ({target})",
                                 "pyproject.toml", _line_of(src.read("pyproject.toml") or "", str(name)), "manifest", False,
                                 ["Project code executes inside the container"],
                                 ["The package is not installed in the image; it is imported from the project folder."], dict(env)))

    # Django
    if "manage.py" in root_files:
        add(other, Candidate("python:django-test", "python", ["python", "./manage.py", "test"], "Run the Django test suite", "manage.py", None, "convention", False,
                             ["Project code executes inside the container"], ["Django must be present in the image; the stock image does not include it."], dict(env)))

    if pys:
        add(other, Candidate("python:compileall", "python", ["python", "-m", "compileall", "-q", "."],
                             "Syntax-check every .py file (compiles only; the program is not run)", "", None, "convention", False,
                             ["Writes .pyc files into the disposable copy only; project code does not run"], [], dict(env)))
    if entry_summary:
        notes.append("Python entry points: " + ", ".join(entry_summary[:8]) + ("..." if len(entry_summary) > 8 else "") + ".")
    root_sorted = [c for _r, c in sorted(root_hits, key=lambda t: (t[0], t[1].id))]
    return root_sorted, other


# ---------------------------------------------------------------- Node
def _node_candidates(src: ProjectView, names: List[str], root_files: Set[str], notes: List[str]) -> List[Candidate]:
    if "package.json" not in root_files:
        return []
    text = src.read("package.json") or ""
    try:
        pkg = json.loads(text)
        scripts = pkg.get("scripts") or {}
        if not isinstance(scripts, dict):
            raise ValueError
    except (ValueError, AttributeError):
        return []
    env = dict(ECOSYSTEM_ENV["node"])
    out: List[Candidate] = []
    base = ["Project scripts execute inside the container"]
    for k in LIFECYCLE:
        if k in scripts:
            base.append(f"Lifecycle hook '{k}' runs automatically: {str(scripts[k])[:120]}")
    has_lock = "package-lock.json" in root_files
    main = pkg.get("main") if isinstance(pkg.get("main"), str) else ""
    main = main.lstrip("./") if main and ".." not in main and not main.startswith("/") else ""
    entry = main if main in names else next((e for e in NODE_ENTRY_NAMES if e in root_files), "")
    if entry:
        out.append(Candidate(f"node:run:{entry}", "node", ["node", _rel(entry)], f"Run {entry} (root entry file)", entry, None, "entry-point", False,
                             ["Project code executes inside the container"],
                             ["node_modules is not copied; without network, required packages are missing."], dict(env)))
        notes.append(f"Node entry point: {entry}.")
    if "start" in scripts:
        risk = base + ([f"Script 'start' contains '{m.group(1)}'"] if (m := SUSPICIOUS.search(str(scripts['start']))) else [])
        out.append(Candidate("node:npm-start", "node", ["npm", "start"], "Run the 'start' script (servers run until the timeout)", "package.json",
                             _line_of(text, '"start"'), "manifest", False, risk, ["node_modules is not copied; without network, dependencies are missing."], dict(env)))
        install = "npm ci --ignore-scripts" if has_lock else "npm install --ignore-scripts"
        out.append(Candidate("node:npm-install-start", "node", ["sh", "-c", install + " && npm start"], "Install dependencies (install hooks disabled), then run 'start'",
                             "package.json", _line_of(text, '"start"'), "manifest", True, ["Network access is required (explicit approval)"] + risk, [], dict(env)))
    for name, cmd in list(scripts.items())[:40]:
        if name in ("test", "build", "start") or name in LIFECYCLE or not NPM_NAME.match(str(name)):
            continue
        if len([c for c in out if c.id.startswith("node:npm-run-")]) >= 10:
            break
        risk = base + ([f"Script '{name}' contains '{m.group(1)}': {str(cmd)[:100]}"] if (m := SUSPICIOUS.search(str(cmd))) else [])
        out.append(Candidate(f"node:npm-run-{name}", "node", ["npm", "run", str(name)], f"Run the '{name}' script", "package.json",
                             _line_of(text, f'"{name}"'), "manifest", False, risk, [], dict(env)))
    return out


# ---------------------------------------------------------------- .NET
def _dotnet_candidates(src: ProjectView, names: List[str], notes: List[str]) -> List[Candidate]:
    projs = sorted((n for n in names if n.lower().endswith((".csproj", ".vbproj", ".fsproj"))), key=lambda n: (n.count("/"), n))[:20]
    env = dict(ECOSYSTEM_ENV["dotnet"])
    out: List[Candidate] = []
    desktop: List[str] = []
    for p in projs:
        t = src.read(p, 200_000) or ""
        is_exe = bool(re.search(r"<OutputType>\s*(Win)?Exe\s*</OutputType>", t, re.I)) or "Microsoft.NET.Sdk.Web" in t
        windows_only = bool(re.search(r"<UseWindowsForms>\s*true|<UseWPF>\s*true|<TargetFramework[s]?>[^<]*-windows", t, re.I)) or \
            bool(re.search(r"<OutputType>\s*WinExe", t, re.I))
        if not is_exe:
            continue
        if windows_only:
            desktop.append(p)
            continue
        stem = PurePosixPath(p).stem
        risks = ["MSBuild executes project-defined targets and build events inside the container", "Project code executes inside the container"]
        out.append(Candidate(f"dotnet:run:{stem}", "dotnet", ["dotnet", "run", "--project", _rel(p), "--no-restore"], f"Run {p} (no package restore)", p, None,
                             "entry-point", False, risks, ["Packages are not restored offline: expect NETSDK1004 unless obj/ is present."], dict(env)))
        out.append(Candidate(f"dotnet:restore-run:{stem}", "dotnet", ["dotnet", "run", "--project", _rel(p)], f"Restore packages, then run {p}", p, None,
                             "entry-point", True, ["Network access is required (explicit approval)"] + risks, [], dict(env)))
    if desktop:
        notes.append("Windows desktop application(s) (" + ", ".join(desktop[:4]) + "): they cannot run in the Linux container; "
                     "use 'dotnet:build' to check that they compile.")
    return out


# ---------------------------------------------------------------- Java (single-file programs in the project root)
def _java_candidates(src: ProjectView, names: List[str], root_files: Set[str]) -> List[Candidate]:
    out: List[Candidate] = []
    javas = sorted(n for n in root_files if n.endswith(".java"))
    if not javas or "pom.xml" in root_files:
        return out
    for n in javas[:5]:
        stem = n[:-5]
        text = src.read(n, 200_000) or ""
        if not IDENT.match(stem) or re.search(r"^\s*package\s", text, re.M) or not re.search(r"public\s+static\s+void\s+main\s*\(", text):
            continue
        line = f"javac -d /tmp/out ./*.java && java -cp /tmp/out {stem}"
        out.append(Candidate(f"java:run:{stem}", "maven", ["sh", "-c", line], f"Compile the root .java files and run {stem} (root entry file)", n, None, "entry-point",
                             False, ["Project code executes inside the container"], ["Uses the JDK that ships with the Maven image."], dict(ECOSYSTEM_ENV["maven"])))
    return out


def _other_language_notes(root_files: Set[str], names: List[str], notes: List[str]) -> None:
    if "go.mod" in root_files or "main.go" in root_files:
        notes.append("Go project: no default image is configured. Set 'Image override' to a Go image (for example golang:1.22) and use the custom command  go run .")
    if "composer.json" in root_files or any(n in root_files for n in ("index.php", "artisan")):
        notes.append("PHP project: no default image is configured. Set 'Image override' to a PHP CLI image and use a custom command such as  php ./index.php")
    if "Cargo.toml" in root_files:
        notes.append("Rust project: no default image is configured. Set 'Image override' to a Rust image and use a custom command such as  cargo run")


# ---------------------------------------------------------------- public API
def discover_run_targets(src: ProjectView) -> Tuple[List[Candidate], List[Candidate], List[str]]:
    """(root-entry candidates, other run candidates, notes)."""
    names = src.names
    root_files = {n for n in names if "/" not in n}
    notes: List[str] = []
    root_py, other_py = _py_candidates(src, names, root_files, notes)
    node = _node_candidates(src, names, root_files, notes)
    net = _dotnet_candidates(src, names, notes)
    java = _java_candidates(src, names, root_files)
    _other_language_notes(root_files, names, notes)
    root_first = root_py + [c for c in node + java if c.id.startswith(("node:run:", "java:run:"))]
    rest = [c for c in other_py] + [c for c in node if c not in root_first] + net + [c for c in java if c not in root_first]
    return root_first[:MAX_TARGETS], rest[:MAX_TARGETS], notes


def discover_all(source) -> Tuple[List[Candidate], List[str]]:
    """Build/test commands (unchanged) plus run targets. Order: root entry files, build/test commands, other runnable files.
    Raises zipfile.BadZipFile / FileNotFoundError for bad sources, like ``discover_candidates``."""
    src = ProjectView(source)
    try:
        cands, notes = _discover(src)
        root_first, rest, run_notes = discover_run_targets(src)
        out_notes: List[str] = []
        if src.venvs:
            out_notes.append("Skipped virtual-environment folder(s): " + ", ".join(sorted(src.venvs)[:6]) + " (installed packages, not project code).")
        if root_first or rest:
            out_notes.append(f"{len(root_first)} root entry command(s) listed first, then build/test commands, then {len(rest)} other runnable command(s). "
                             "Everything runs only inside the container after you approve the plan.")
        # the 'no commands' message from the build/test discovery is wrong once run targets exist
        base_notes = [n for n in notes if not (n.startswith("No build/test commands could be derived") and (root_first or rest))]
        return root_first + cands + rest, out_notes + run_notes + base_notes
    finally:
        src.close()
