"""One-click "Run now": pick what to start, work out what it needs, and prepare it so the preview can open at once.

Everything here is read-only analysis or an *approved* image build; nothing from the project is executed on the host.

* ``pick_entry``     - window program first, then web app, then console script (or the file the user named).
* ``third_party``    - packages the script (and the project files it imports) needs, from the code itself, minus the standard
                       library and the project's own modules; versions are taken from requirements.txt when listed there.
* ``make_env_plan``  - a small cached image ``ase-env-<hash>`` = base image + ``pip install`` of exactly those packages. It is
                       built once (network ON for that build only, shown to the user); later runs are offline and instant.
* ``missing_module`` - reads a run's error output and names the package to add (``ModuleNotFoundError``), for the one-time retry.
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, List, Optional

from plugins.build_execution.discovery import DEFAULT_IMAGES, SourceView
from plugins.build_execution.run_targets import ProjectView
from plugins.build_execution.sandbox import python_closure

from .imagebuilder import BuildPlan, _env_dir  # noqa: F401  (re-exported for the UI)
from .models import Entry, PreviewOptions

PIP_NAME = {
    "cv2": "opencv-python-headless", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "serial": "pyserial", "Crypto": "pycryptodome",
    "skimage": "scikit-image", "OpenSSL": "pyOpenSSL", "jwt": "PyJWT", "attr": "attrs", "git": "GitPython", "fitz": "PyMuPDF",
    "docx": "python-docx", "pptx": "python-pptx", "magic": "python-magic", "usb": "pyusb", "wx": "wxPython", "zmq": "pyzmq",
    "MySQLdb": "mysqlclient", "psycopg2": "psycopg2-binary", "ruamel": "ruamel.yaml", "Xlib": "python-xlib", "google": "protobuf",
}
WINDOWS_ONLY = {"win32api", "win32con", "win32gui", "win32com", "win32file", "win32process", "pythoncom", "pywintypes", "winreg",
                "_winreg", "msvcrt", "winsound", "wmi", "comtypes", "_winapi"}
PRESENT_IN_GUI_IMAGE = {"tkinter", "_tkinter"}
_OK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:[=<>~!]=?[A-Za-z0-9.*+!_\-]+(?:,[=<>~!]=?[A-Za-z0-9.*+!_\-]+)*)?$")
MAX_PACKAGES = 25


def pick_entry(entries: List[Entry], script: str = "") -> Optional[Entry]:
    """The entry "Run now" starts: the named script if given, otherwise window program > web app > console script > anything."""
    if script:
        s = script[2:] if script.startswith("./") else script
        for e in entries:
            if e.file == s and e.kind in ("gui", "web", "console"):
                return e
    for kind in ("gui", "web", "console"):
        for e in entries:
            if e.kind == kind:
                return e
    return entries[0] if entries else None


def _top_imports(text: str) -> set:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError):
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            out.add(n.module.split(".")[0])
    return out


def _requirement_lines(sv: SourceView) -> dict:
    """{normalised name: full requirement line} from the root requirements.txt (URLs, options and includes are ignored)."""
    out = {}
    for raw in (sv.read("requirements.txt") or "").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith(("-", "git+", "http")) or ";" in line:
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._\-]*", line)
        if m and _OK_NAME.match(line.replace(" ", "")):
            out[re.sub(r"[-_.]+", "-", m.group(0)).lower()] = line.replace(" ", "")
    return out


def third_party(source, script: str) -> List[str]:
    """pip requirement strings the script needs. Empty for non-Python entries; never raises."""
    script = script[2:] if script.startswith("./") else script
    if not script.endswith(".py"):
        return []
    try:
        sv = ProjectView(source)
        files = set(sv.names)
        closure = python_closure(script, files, sv.read) if script in files else {script}
        local = {PurePosixPath(n).parts[0].removesuffix(".py") for n in files}
        local |= {PurePosixPath(n).stem for n in files if n.endswith(".py")}
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        names = set()
        for f in closure:
            if f.endswith(".py"):
                names |= _top_imports(sv.read(f) or "")
        reqs = _requirement_lines(sv)
        pkgs = {}
        for n in sorted(names):
            if n in stdlib or n in local or n in WINDOWS_ONLY or n in PRESENT_IN_GUI_IMAGE or n.startswith("_") or n == "__future__":
                continue
            pip = PIP_NAME.get(n, n)
            key = re.sub(r"[-_.]+", "-", pip).lower()
            pkgs[key] = reqs.get(key) or pip                      # a pinned version from requirements.txt wins
        return [p for p in pkgs.values() if _OK_NAME.match(p)][:MAX_PACKAGES]
    except Exception:  # noqa: BLE001 - analysis only; a failure means "nothing found", the run then reports the real error
        return []


def missing_module(stderr: str) -> str:
    """Top-level module named by the last ModuleNotFoundError / ImportError in an error text, else ''."""
    hits = re.findall(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]", stderr or "")
    if not hits:
        return ""
    top = hits[-1].split(".")[0]
    return "" if top in WINDOWS_ONLY else top


def pip_for_module(mod: str) -> str:
    pip = PIP_NAME.get(mod, mod)
    return pip if _OK_NAME.match(pip) else ""


def env_tag(base: str, packages: List[str]) -> str:
    h = hashlib.sha256((base + "\x00" + "\x00".join(sorted(packages))).encode()).hexdigest()[:12]
    return f"ase-env-{h}:latest"


def make_env_plan(options: PreviewOptions, base: str, packages: List[str], exe: str, backend: str, allow_empty: bool = False) -> BuildPlan:
    """Build plan for 'base image + pip install packages'. The Dockerfile is written to a private folder; the user approves its hash."""
    pk = sorted({p for p in packages if _OK_NAME.match(p)})
    if not pk and not allow_empty:
        raise ValueError("No packages to install.")
    tag = env_tag(base, pk)
    folder = _env_dir(tag)
    folder.mkdir(parents=True, exist_ok=True)
    text = f"FROM {base}\n" + (("RUN pip install --no-cache-dir --disable-pip-version-check " + " ".join(f"'{p}'" for p in pk) + "\n") if pk else "")
    (folder / "Dockerfile").write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    argv = [exe, "build", "--tag", tag, "--file", str(folder / "Dockerfile"), str(folder)]
    risks = [("Downloads these packages from the Python package index (network ENABLED for this one-time build only): " + ", ".join(pk))
             if pk else f"Downloads the base image {base} (network ENABLED for this one-time step only)",
             "Package install scripts run inside the image build, not on your computer; the project itself is not part of the build"]
    return BuildPlan(exe=exe, backend=backend, tag=tag, argv=argv, dockerfile_sha256=sha, risks=risks)


@dataclass
class QuickPrep:
    """Result of the read-only analysis behind one "Run now" click."""
    entry: Optional[Entry] = None
    packages: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    needs_runtime_start: bool = False
    needs_gui_image: bool = False
    needs_env_image: bool = False
    env_plan: Optional[BuildPlan] = None
    gui_plan: Optional[BuildPlan] = None
    image: str = ""
    display: bool = False


def analyse(source, entries: List[Entry], options: PreviewOptions, script: str = "",
            status_fn: Optional[Callable] = None, image_fn: Optional[Callable] = None) -> QuickPrep:
    """Decide the entry, the image and every step "Run now" will take. Reads only; never starts or builds anything."""
    from .imagebuilder import make_build_plan
    from .runtimes import find_runtime, image_present, status
    status_fn, image_fn = status_fn or status, image_fn or image_present
    q = QuickPrep()
    q.entry = pick_entry(entries, script)
    s1 = script[2:] if script.startswith("./") else script
    if s1.endswith(".py") and (q.entry is None or q.entry.file != s1):
        from .discovery import custom_entry                  # the user named a file the scan did not list: run exactly that
        q.entry = custom_entry(["python", s1], kind="console", title=f"python {s1}")
    if q.entry is None:
        q.problems.append("No runnable file was found in this project. Choose a file with 'File...' or type a command.")
        return q
    st = status_fn(options)
    exe = st.get("executable") or find_runtime(options)[1]
    if not exe:
        q.problems.append("No container runtime (Docker, Podman, nerdctl or Finch) is installed, so nothing can be shown safely. "
                          "Install Docker Desktop or Podman once; then this button does the rest.")
        return q
    if not st["available"]:
        q.needs_runtime_start = True
        q.steps.append(f"Start {st.get('backend') or 'the container runtime'} (it is installed but not running)")
    backend = st.get("backend") or find_runtime(options)[0] or ""
    e = q.entry
    q.display = e.kind == "gui"
    if e.ecosystem == "python" and e.kind == "console":
        try:
            from .discovery import entry_uses_gui
            q.display = bool(entry_uses_gui(source, e))
        except Exception:  # noqa: BLE001
            pass
    base = options.gui_image if q.display else options.images.get("python") or DEFAULT_IMAGES["python"]
    have = lambda img: bool(st["available"] and image_fn(exe, img))                    # noqa: E731
    if q.display and st["available"] and not have(base):
        q.needs_gui_image = True
    elif q.display and not st["available"]:
        q.needs_gui_image = True                                                      # unknown until the runtime is up: assume yes
    if q.needs_gui_image:
        try:
            q.gui_plan = make_build_plan(options, tag=base)
            q.steps.append("Build the GUI preview image once (Xvfb + Tk, needs internet, a few minutes)")
        except RuntimeError as exc:
            q.problems.append(str(exc))
    q.packages = third_party(source, e.file) if e.ecosystem == "python" and e.file else []
    q.image = base
    if q.packages:
        q.env_plan = make_env_plan(options, base, q.packages, exe, backend)
        q.image = q.env_plan.tag
        if not have(q.image):
            q.needs_env_image = True
            q.steps.append("Install " + ", ".join(q.packages) + " into a cached image (once; later runs are instant)")
    elif not q.display and e.ecosystem == "python" and st["available"] and not have(base):
        q.env_plan = make_env_plan(options, base, [], exe, backend, allow_empty=True)     # the base image itself is missing: fetch it once
        q.image, q.needs_env_image = q.env_plan.tag, True
        q.steps.append(f"Download the Python image {base} once (needs internet)")
    q.steps.append(f"Run {' '.join(e.argv)} in a disposable container (network off, files are a scratch copy)")
    q.steps.append("Show the " + {"gui": "window", "web": "web page"}.get(e.kind, "console output") + " here as soon as it appears")
    return q


# ------------------------------------------------------------------------------------------------ files the program asked for
_FILE_PATTERNS = (
    r"No such file or directory: ['\"](.+?)['\"]",
    r"FileNotFoundError[^\n]*?['\"]([^'\"\n]+)['\"]",
    r"can't open file ['\"](.+?)['\"]",
    r"couldn't open \"(.+?)\"",
    r"TemplateNotFound: ([^\s'\"]+)",
    r"No file '(.+?)' found",
    r"Image file ['\"](.+?)['\"] not found",
    r"cannot identify image file ['\"](.+?)['\"]",
    r"unable to open [a-z ]*file:? ?['\"](.+?)['\"]",
)


def _module_paths(mod: str, names: set) -> List[str]:
    """Project files that define the dotted module ``a.b`` (a/b.py or a/b/__init__.py, plus the packages' __init__ files)."""
    parts = mod.split(".")
    out: List[str] = []
    for k in range(len(parts), 0, -1):
        base = "/".join(parts[:k])
        for cand in (f"{base}.py", f"{base}/__init__.py"):
            if cand in names:
                out.append(cand)
        if out:
            break
    for j in range(1, len(parts)):
        init = "/".join(parts[:j]) + "/__init__.py"
        if init in names and out:
            out.append(init)
    return out


def needed_files(text: str, names: List[str], entry_dir: str = "") -> dict:
    """What a failed run says it could not find, matched against the project's file list.
    {'files': [rel...], 'dirs': [rel...], 'absent': [requested names that are not in the project]} - all read-only; nothing is copied here."""
    nameset = set(names)
    dirs_all = {str(PurePosixPath(n).parent) for n in names}
    for n in names:
        p = PurePosixPath(n).parent
        while str(p) not in (".", ""):
            dirs_all.add(str(p))
            p = p.parent
    files: List[str] = []
    dirs: List[str] = []
    absent: List[str] = []
    asked: List[str] = []
    for pat in _FILE_PATTERNS:
        asked += [m for m in re.findall(pat, text or "")]
    for mod in re.findall(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]", text or ""):
        local = _module_paths(mod, nameset)
        if local:
            files += [f for f in local if f not in files]
    for raw in dict.fromkeys(asked):
        p = raw.replace("\\", "/").strip()
        for pre in ("/work/", "./"):
            if p.startswith(pre):
                p = p[len(pre):]
        if not p or p.startswith("/") or ".." in p.split("/") or len(p) > 240:
            continue
        p = p.rstrip("/")
        cands = [p, f"{entry_dir}/{p}" if entry_dir else p]
        hit = next((c for c in cands if c in nameset), None)
        if hit:
            if hit not in files:
                files.append(hit)
            continue
        dhit = next((c for c in cands if c in dirs_all), None)
        if dhit:
            if dhit not in dirs:
                dirs.append(dhit)
            continue
        tail = "/" + p
        by_suffix = [n for n in names if n.endswith(tail)] or [n for n in names if PurePosixPath(n).name == PurePosixPath(p).name]
        if len(by_suffix) == 1:
            files.append(by_suffix[0])
        elif len(by_suffix) > 1:
            files += [x for x in by_suffix[:3] if x not in files]
        else:
            absent.append(p)
    return {"files": files[:30], "dirs": dirs[:10], "absent": absent[:10]}
