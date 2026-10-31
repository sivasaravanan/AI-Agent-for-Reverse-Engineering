"""Find what can be started in a project (ZIP or folder). READ-ONLY: nothing is extracted or executed.

Entries are suggestions from manifests and source text; the user reviews the exact command before anything runs.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

from plugins.build_execution.discovery import ECOSYSTEM_ENV, SourceView, discover_candidates
from plugins.build_execution.run_targets import ProjectView      # a SourceView that never walks into virtual environments

from .models import Entry

GUI_IMPORTS = re.compile(r"^\s*(?:from|import)\s+(tkinter|customtkinter|ttkbootstrap|PyQt5|PyQt6|PySide2|PySide6|wx|kivy|pygame|pyglet|arcade|turtle)\b", re.M)
WEB_RULES = (                                      # (regex, framework, default port)
    (re.compile(r"^\s*(?:from|import)\s+streamlit\b", re.M), "streamlit", 8501),
    (re.compile(r"^\s*(?:from|import)\s+gradio\b", re.M), "gradio", 7860),
    (re.compile(r"^\s*(?:from|import)\s+nicegui\b", re.M), "nicegui", 8080),
    (re.compile(r"^\s*(?:from|import)\s+fastapi\b", re.M), "fastapi", 8000),
    (re.compile(r"^\s*(?:from|import)\s+flask\b", re.M), "flask", 5000),
    (re.compile(r"^\s*(?:from|import)\s+http\.server\b", re.M), "http.server", 8000),
    (re.compile(r"^\s*(?:from|import)\s+(?:aiohttp|bottle|tornado|sanic|quart|uvicorn|cherrypy)\b", re.M), "web", 8000),
)
MAIN_GUARD = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]""", re.M)
COMMON_MAIN = ("main.py", "app.py", "run.py", "server.py", "start.py", "manage.py", "wsgi.py", "gui.py", "__main__.py")
MAX_PY_FILES = 400
MAX_ENTRIES = 40
PORT_RE = re.compile(r"\bport\s*=\s*(\d{2,5})\b")
RUN_LIKE = re.compile(r"^(?:run|start|launch)[_-]\w+\.py$", re.I)      # run_hub.py, start_app.py ... (launchers without a main guard)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_./\-]+$")


def _env(eco: str) -> dict:
    return dict(ECOSYSTEM_ENV.get(eco, {}))


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".")


def _port(text: str, default: int) -> int:
    m = PORT_RE.search(text)
    if m and 1024 <= int(m.group(1)) <= 65535:
        return int(m.group(1))
    return default


def _local_gui_import(src: SourceView, names: List[str], text: str) -> Optional[str]:
    """Name of the GUI toolkit when this file imports a LOCAL module/package that uses one (e.g. main.py -> diffstudio -> tkinter).
    Files are only read and parsed, never imported."""
    tops = {(n[:-3] if "/" not in n and n.endswith(".py") else n.split("/")[0]) for n in names}
    mods = sorted(set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", text, re.M)) & tops)
    for m in mods[:8]:
        files = [n for n in names if n.endswith(".py") and (n == m + ".py" or n.startswith(m + "/"))][:80]
        for f in files:
            g = GUI_IMPORTS.search(src.read(f, 200_000) or "")
            if g:
                return g.group(1)
    return None


def entry_uses_gui(source, entry) -> bool:
    """True when the file this start option runs (or a local module it imports) uses a GUI toolkit. Read-only parsing."""
    if entry.ecosystem != "python" or not entry.argv:
        return False
    argv = list(entry.argv)
    f = (entry.file or "").strip()
    text = ""
    try:
        src = ProjectView(source)
    except Exception:  # noqa: BLE001
        return False
    try:
        if "-m" in argv and argv.index("-m") + 1 < len(argv):
            mod = argv[argv.index("-m") + 1].split(".")[0]
            text = f"import {mod}\n"
        else:
            f = f or next((a for a in argv[1:] if a.endswith(".py")), "")
            f = f[2:] if f.startswith("./") else f
            text = src.read(f, 2_000_000) or "" if f else ""
        return bool(text) and bool(GUI_IMPORTS.search(text) or _local_gui_import(src, src.names, text))
    finally:
        src.close()


def project_uses_gui(source) -> bool:
    """True when any Python file of the project imports a GUI toolkit (used to give test runs a virtual screen)."""
    try:
        src = ProjectView(source)
    except Exception:  # noqa: BLE001
        return False
    try:
        for n in [n for n in src.names if n.endswith(".py")][:300]:
            if GUI_IMPORTS.search(src.read(n, 200_000) or ""):
                return True
    finally:
        src.close()
    return False


def _python_entries(src: SourceView, names: List[str]) -> List[Entry]:
    out: List[Entry] = []
    pys = [n for n in names if n.endswith(".py") and not re.search(r"(^|/)(test_[^/]*|[^/]*_test|conftest|setup)\.py$", n)
           and "/tests/" not in n and not n.startswith("tests/")]
    pys.sort(key=lambda n: (n.count("/"), n not in COMMON_MAIN and n.rsplit("/", 1)[-1] not in COMMON_MAIN, n))
    has_req = any(n.lower() == "requirements.txt" for n in names)
    seen = 0
    for n in pys:
        if seen >= MAX_PY_FILES or len(out) >= MAX_ENTRIES:
            break
        seen += 1
        text = src.read(n, 200_000) or ""
        if len(text) > 150_000:                        # long file (the read is cut at 200 KB of bytes): the `if __name__ == "__main__"` block sits at the very end
            text = src.read(n, 2_000_000) or text
        guarded = bool(MAIN_GUARD.search(text))
        base = n.rsplit("/", 1)[-1]
        app_obj = bool(re.search(r"^(\w+)\s*=\s*(?:Flask|FastAPI)\(", text, re.M)) or bool(re.search(r"^\s*(?:import|from)\s+streamlit\b", text, re.M))
        if not guarded and base not in COMMON_MAIN and not app_obj and not RUN_LIKE.match(base):
            continue
        if not SAFE_NAME.match(n):
            continue
        env = _env("python")
        install = ["pip", "install", "--no-input", "-q", "--target", "/tmp/ase-site", "-r", "requirements.txt"] if has_req else None
        if install:
            env["PYTHONPATH"] = "/tmp/ase-site"
        line = (text[:MAIN_GUARD.search(text).start()].count("\n") + 1) if guarded else 1
        gui = GUI_IMPORTS.search(text)
        web = next(((fw, p) for rx, fw, p in WEB_RULES if rx.search(text)), None)
        if base == "manage.py" and "django" in text.lower():
            out.append(Entry(f"python:{n}:django", f"Django dev server ({n})", "web", "python",
                             ["python", n, "runserver", "0.0.0.0:8000"], n, line, 8000, "Django development server",
                             env, bool(install), install))
        elif web and web[0] == "streamlit":
            out.append(Entry(f"python:{n}:streamlit", f"Streamlit app ({n})", "web", "python",
                             ["python", "-m", "streamlit", "run", n, "--server.address", "0.0.0.0", "--server.port", "8501",
                              "--server.headless", "true"], n, line, 8501, "Streamlit app on port 8501", env, bool(install), install))
        elif web and web[0] == "fastapi" and (m := re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.M)) and n.endswith(".py"):
            out.append(Entry(f"python:{n}:uvicorn", f"FastAPI via uvicorn ({_module_of(n)}:{m.group(1)})", "web", "python",
                             ["python", "-m", "uvicorn", f"{_module_of(n)}:{m.group(1)}", "--host", "0.0.0.0", "--port", "8000"],
                             n, line, 8000, "FastAPI app on port 8000 (uvicorn must be in the image or installed)", env,
                             bool(install), install))
        elif web and web[0] == "flask" and (m := re.search(r"^(\w+)\s*=\s*Flask\(", text, re.M)):
            out.append(Entry(f"python:{n}:flask", f"Flask app ({_module_of(n)}:{m.group(1)})", "web", "python",
                             ["python", "-m", "flask", "--app", f"{_module_of(n)}:{m.group(1)}", "run", "--host", "0.0.0.0", "--port", "5000"],
                             n, line, 5000, "Flask app on port 5000", env, bool(install), install))
        elif web:
            env2 = dict(env, HOST="0.0.0.0", GRADIO_SERVER_NAME="0.0.0.0", PORT=str(_port(text, web[1])))
            out.append(Entry(f"python:{n}:web", f"{web[0].title()} app ({n})", "web", "python", ["python", n], n, line,
                             _port(text, web[1]), f"Web app; the program must listen on 0.0.0.0:{_port(text, web[1])}",
                             env2, bool(install), install,
                             notes=["Programs that bind 127.0.0.1 only are not reachable through the published port."]))
        elif gui:
            out.append(Entry(f"python:{n}:gui", f"{gui.group(1)} app ({n})", "gui", "python", ["python", n], n, line, None,
                             "Desktop GUI shown on a virtual screen (needs the preview GUI image)", env, bool(install), install))
        elif base == "__main__.py" and "/" in n:
            mod = n.rsplit("/", 1)[0].replace("/", ".")
            out.append(Entry(f"python:-m {mod}", f"python -m {mod}", "console", "python", ["python", "-m", mod], n, line, None,
                             "Package run as a module", env, bool(install), install))
        else:
            out.append(Entry(f"python:{n}", f"python {n}", "console", "python", ["python", n], n, line, None,
                             "Console program", env, bool(install), install))
            lib = _local_gui_import(src, names, text) if "/" not in n else None
            if lib:                                        # additive: the console row above stays; this one adds the virtual screen
                out.append(Entry(f"python:{n}:gui-via-imports", f"{lib} app via imports ({n})", "gui", "python", ["python", n], n, line,
                                 None, "Desktop GUI (a module imported by this file uses " + lib + ") shown on a virtual screen",
                                 env, bool(install), install,
                                 notes=["This file itself does not import the GUI toolkit; it was found through the project's own modules."]))
    return out


def _node_entries(src: SourceView, names: List[str]) -> List[Entry]:
    out: List[Entry] = []
    pj = next((n for n in names if n == "package.json"), None)
    if not pj:
        return out
    try:
        data = json.loads(src.read(pj, 400_000) or "{}")
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    env = dict(_env("node"), HOST="0.0.0.0", HOSTNAME="0.0.0.0")
    install = ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"]
    webby = any(k in deps for k in ("express", "next", "vite", "react-scripts", "koa", "fastify", "@nestjs/core", "nuxt", "http-server", "serve"))
    port = 5173 if "vite" in deps else 3000
    for name in ("start", "dev", "serve"):
        if name in scripts:
            argv = ["npm", "run", name] + (["--", "--host", "0.0.0.0"] if "vite" in deps and name in ("dev", "serve") else [])
            e = dict(env, PORT=str(port))
            out.append(Entry(f"node:npm-{name}", f"npm run {name}", "web" if webby else "console", "node", argv, pj, 0,
                             port if webby else None, f"scripts.{name}: {str(scripts[name])[:80]}", e, True, install,
                             risks=["npm install runs package lifecycle scripts inside the container"],
                             notes=["Dependency install needs the network."]))
    main = data.get("main") if isinstance(data.get("main"), str) else ""
    if main and SAFE_NAME.match(main) and main in names:
        out.append(Entry(f"node:{main}", f"node {main}", "web" if webby else "console", "node", ["node", "./" + main], main, 0,
                         port if webby else None, "package.json main", dict(env, PORT=str(port)), True, install))
    return out


def _dotnet_entries(src: SourceView, names: List[str]) -> List[Entry]:
    out: List[Entry] = []
    for n in names:
        if n.lower().endswith(".csproj") and SAFE_NAME.match(n):
            text = src.read(n, 100_000) or ""
            web = "Microsoft.NET.Sdk.Web" in text
            if "<OutputType>Exe</OutputType>" in text or web:
                env = dict(_env("dotnet"), ASPNETCORE_URLS="http://0.0.0.0:8080") if web else _env("dotnet")
                out.append(Entry(f"dotnet:{n}", f"dotnet run ({n})", "web" if web else "console", "dotnet",
                                 ["dotnet", "run", "--project", "./" + n], n, 0, 8080 if web else None,
                                 "ASP.NET app on port 8080" if web else ".NET console program", env, True,
                                 notes=["Restoring packages needs the network."]))
    return out


def _static_entries(names: List[str]) -> List[Entry]:
    if "index.html" in names:
        return [Entry("static:http-server", "Static site (python http.server)", "web", "python",
                      ["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"], "index.html", 0, 8000,
                      "Serves the project folder on port 8000", _env("python"))]
    return []


def discover_entries(source) -> Tuple[List[Entry], List[str]]:
    """(entries, notes). Raises zipfile.BadZipFile / FileNotFoundError for bad sources, like the Phase 4 discovery."""
    src = ProjectView(source)
    notes: List[str] = []
    try:
        names = src.names
        entries: List[Entry] = []
        entries += _python_entries(src, names)
        entries += _node_entries(src, names)
        entries += _dotnet_entries(src, names)
        entries += _static_entries(names)
        if not entries:
            notes.append("No runnable entry point was found. Use a custom command.")
    finally:
        src.close()
    try:                                                   # test commands come from the Phase 4 discovery (same rules)
        cands, tnotes = discover_candidates(source)
        notes += list(tnotes)
        for c in cands:
            is_test = "test" in f"{c.id} {c.description}".lower()      # build / lint commands of Build & Test are start options here too
            entries.append(Entry(c.id, c.display, "test" if is_test else "console", c.ecosystem, list(c.argv), c.source_file, c.source_line or 0, None,
                                 c.description, dict(c.env), c.needs_network, None, list(c.risks), list(c.notes)))
    except Exception as exc:  # noqa: BLE001 - test discovery problems never hide the runnable entries
        notes.append(f"Test discovery failed: {type(exc).__name__}")
    return entries[:MAX_ENTRIES * 2], notes


def custom_entry(argv: List[str], kind: str = "console", ecosystem: str = "custom", port: Optional[int] = None,
                 title: str = "") -> Entry:
    """A user-typed command. It runs inside the container only, never through a host shell."""
    if not argv or any((not isinstance(a, str)) or "\x00" in a for a in argv):
        raise ValueError("Empty or invalid command.")
    eco = ecosystem if ecosystem != "custom" else ("python" if argv[0] in ("python", "python3") else
                                                  "node" if argv[0] in ("node", "npm", "npx") else
                                                  "dotnet" if argv[0] == "dotnet" else "custom")
    if eco == "python" and argv[0] == "python3":
        argv = ["python"] + list(argv[1:])
    return Entry("custom:" + argv[0], title or " ".join(argv), kind, eco, list(argv), "", 0, port, "User-supplied command",
                 _env(eco), False, None, ["The command was typed by the user and is not derived from the project."])
