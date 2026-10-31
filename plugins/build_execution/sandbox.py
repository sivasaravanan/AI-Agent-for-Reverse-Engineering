"""Controlled execution inside a disposable Docker/Podman container.

Hard rules enforced here:
  * No approval matching the exact plan hash  -> refused.
  * No working container runtime              -> skipped. There is NO fallback to running on the host.
  * Images are never pulled automatically (--pull=never).
  * Network is 'none' unless the approved plan enables it.
  * Only a scratch COPY of the project is mounted; no host paths, no host env, no credentials.
"""
from __future__ import annotations

import codecs
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import storage as _storage
from .discovery import DEFAULT_IMAGES, SKIP_DIRS
from .events import BUS
from .models import Approval, Candidate, ExecutionResult, Limits, Options, Plan

CREATE_NO_WINDOW = 0x08000000
_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)


EXTRA_SKIP = {".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vs", ".gradle", ".hg", ".svn"}


class WorkspaceRejected(Exception):
    pass


# ------------------------------------------------------------------------------ small helpers
def _env() -> dict:
    return {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}


def _cli(cmd: list, timeout: float = 20) -> tuple:
    """Run a short runtime CLI command. Returns (returncode|None, stdout, stderr)."""
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout, env=_env(),
                           stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


def find_runtime(options: Options) -> tuple:
    """Return (backend_name, executable) or (None, None)."""
    names = ("docker", "podman") if options.backend == "auto" else (options.backend,)
    for n in names:
        override = options.tool_paths.get(n)
        exe = override if override and Path(override).exists() else shutil.which(n)
        if exe:
            return n, exe
    return None, None


def isolation_status(options: Optional[Options] = None) -> dict:
    """{'available': bool, 'backend', 'executable', 'version', 'reason'} - never raises."""
    options = options or Options()
    name, exe = find_runtime(options)
    if not exe:
        return {"available": False, "backend": None, "executable": None, "version": "",
                "reason": "No docker/podman executable found on PATH. Build/test execution is unavailable; "
                          "static analysis still works. Install Docker Desktop or Podman to enable it."}
    rc, out, err = _cli([exe, "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:
        return {"available": False, "backend": name, "executable": exe, "version": "",
                "reason": f"{name} is installed but not usable (is the daemon/VM running?): "
                          f"{(err or out).strip()[:300]}"}
    return {"available": True, "backend": name, "executable": exe, "version": out.strip(), "reason": ""}


def image_present(exe: str, image: str) -> bool:
    rc, _, _ = _cli([exe, "image", "inspect", image])
    return rc == 0


# ------------------------------------------------------------------------------ planning
def build_plan(source, candidate: Candidate, options: Optional[Options] = None, image: str = "",
               limits: Optional[Limits] = None) -> Plan:
    options = options or Options()
    limits = limits or Limits(**vars(options.limits))
    if candidate.needs_network and not limits.network:
        limits = Limits(**{**vars(limits), "network": True})   # the plan shows it; the user approves or refuses
    if candidate.min_memory_mb > limits.memory_mb or candidate.min_timeout_s > limits.timeout_s:
        # heavy installs (e.g. torch): the plan shows the raised limits; the user still approves or refuses them
        limits = Limits(**{**vars(limits), "memory_mb": max(limits.memory_mb, candidate.min_memory_mb),
                           "timeout_s": max(limits.timeout_s, candidate.min_timeout_s)})
    src = Path(source)
    if src.is_file():
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        source_id = "zip:" + h.hexdigest()
    else:
        source_id = "dir:" + str(src.resolve())
    img = image or options.images.get(candidate.ecosystem) or DEFAULT_IMAGES.get(candidate.ecosystem, "")
    if not img:
        raise ValueError(f"No container image configured for ecosystem '{candidate.ecosystem}'. "
                         "Pass image=... or set Options.images.")
    return Plan(candidate=candidate, image=img, limits=limits, source=str(src), source_id=source_id)


def container_command(exe: str, plan: Plan, workspace: Path, name: str) -> list:
    """The exact container-runtime command line (shown to the user in the approval dialog)."""
    lim = plan.limits
    cmd = [exe, "run", "--rm", "--name", name, "--pull=never",
           "--network", "bridge" if lim.network else "none",
           "--cpus", str(lim.cpus), "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m",
           "--pids-limit", str(lim.pids), "--ulimit", "nofile=1024:1024",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--read-only", "--tmpfs", f"/tmp:rw,exec,nosuid,size={lim.tmp_mb}m",
           "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "CI=1"]
    for k, v in sorted(plan.candidate.env.items()):
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--mount", f"type=bind,source={workspace},target=/work", "-w", "/work", plan.image]
    return cmd + list(plan.candidate.argv)


# ------------------------------------------------------------------------------ workspace copy
def _prepare_full(source, dest: Path, limits: Limits) -> list:
    """Copy the project (without .git/node_modules/venv/symlinks) into dest with traversal/size protection."""
    max_bytes = limits.max_workspace_mb * 1024 * 1024
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    notes, total = [], 0
    src = Path(source)

    def open_perm(p: Path, is_dir: bool, exec_bit: bool = False):
        try:
            os.chmod(p, 0o777 if is_dir else (0o777 if exec_bit else 0o666))  # container user is uid 65534
        except OSError:
            pass

    if src.is_dir():
        src = src.resolve()
        skip = SKIP_DIRS - {"bin", "obj", "target", "dist", "build"}
        sizes: dict = {}                                   # top-level entry -> bytes, only used to explain a size refusal

        def junk(parent: Path, name: str) -> bool:
            q = parent / name
            return name in skip or name in EXTRA_SKIP or q.is_symlink() or (q / "pyvenv.cfg").is_file()   # any virtualenv, whatever its name

        for d, dirs, files in os.walk(src, followlinks=False):
            dirs[:] = [x for x in dirs if not junk(Path(d), x)]
            out_dir = dest / Path(d).relative_to(src)
            out_dir.mkdir(parents=True, exist_ok=True)
            open_perm(out_dir, True)
            for f in files:
                p = Path(d) / f
                rel = p.relative_to(src)
                if p.is_symlink():
                    notes.append(f"skipped symlink: {rel.as_posix()}")
                    continue
                try:
                    st = p.stat()
                    total += st.st_size
                    sizes[rel.parts[0]] = sizes.get(rel.parts[0], 0) + st.st_size
                    if total > max_bytes:
                        big = ", ".join(f"{k} ({v // 1048576} MB)" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:3])
                        raise WorkspaceRejected(f"Project is larger than {limits.max_workspace_mb} MB (largest so far: {big}). "
                                                "Remove data/build folders from the project, or raise 'Max project MB' in App Preview "
                                                "(= 'Largest project copied in' in Storage & clean-up); or use Quick run / tick "
                                                "'Trim oversized projects' there to copy a trimmed project instead.")
                    shutil.copyfile(p, out_dir / f)
                    open_perm(out_dir / f, False, bool(st.st_mode & stat.S_IXUSR))
                except OSError as exc:                     # locked / unreadable / over-long path: skip the file, do not refuse the project
                    notes.append(f"skipped unreadable file {rel.as_posix()}: {type(exc).__name__}")
        return notes

    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WorkspaceRejected(f"Not a valid ZIP archive: {exc}") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > 50_000:
            raise WorkspaceRejected("Archive has too many entries.")
        for i in zf.infolist():
            n = i.filename.replace("\\", "/")
            parts = PurePosixPath(n).parts
            if "\x00" in i.filename or n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts:
                raise WorkspaceRejected(f"Unsafe path in archive: {i.filename!r}")
        if sum(i.file_size for i in infos) > max_bytes:
            raise WorkspaceRejected(f"Archive expands beyond {limits.max_workspace_mb} MB.")
        for i in infos:
            if i.file_size > 1_048_576 and i.compress_size and i.file_size / i.compress_size > 200:
                raise WorkspaceRejected(f"Suspicious compression ratio: {i.filename!r}")
        names = [i.filename.replace("\\", "/") for i in infos]
        tops = {PurePosixPath(n).parts[0] for n in names}
        strip = 1 if len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in names) else 0
        for i, n in zip(infos, names):
            parts = PurePosixPath(n).parts[strip:]
            mode = i.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                notes.append(f"skipped symlink: {n}")
                continue
            if i.flag_bits & 1:
                notes.append(f"skipped encrypted member: {n}")
                continue
            if set(parts[:-1]) & (SKIP_DIRS - {"bin", "obj", "target", "dist", "build"}):
                continue
            target = dest.joinpath(*parts)
            target.resolve().relative_to(dest)  # raises ValueError if it escaped
            target.parent.mkdir(parents=True, exist_ok=True)
            for anc in [target.parent] + list(target.parent.parents):
                if anc == dest.parent:
                    break
                open_perm(anc, True)
            written = 0
            with zf.open(i) as s, open(target, "wb") as o:
                for chunk in iter(lambda: s.read(65536), b""):
                    written += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise WorkspaceRejected("Archive expands beyond its declared size (zip bomb?).")
                    o.write(chunk)
            open_perm(target, False, bool(mode & 0o100))
    return notes


# ------------------------------------------------------------------------------ single-file scope
DATA_EXT = {".json", ".txt", ".cfg", ".ini", ".csv", ".yml", ".yaml", ".toml", ".md", ".sql", ".html", ".xml", ".tsv"}
NARROW_MAX_FILES = 800


def entry_script(argv) -> str:
    """Project-relative path of the Python script a command starts ('python ./dupescope.py -x' -> 'dupescope.py'), else ''."""
    a = [str(x) for x in (argv or [])]
    if not a or Path(a[0]).name.lower().rstrip("0123456789.").replace(".exe", "") not in ("python", "python3", "py"):
        return ""
    for x in a[1:]:
        if x == "-m":
            return ""
        if x.startswith("-"):
            continue
        if x.lower().endswith(".py"):
            x = x.replace("\\", "/")
            while x.startswith("./"):
                x = x[2:]
            return "" if x.startswith("/") or ".." in x.split("/") else x
        return ""
    return ""


def _imports_of(text: str, pkg_parts: tuple) -> list:
    """Module names (absolute dotted, or relative resolved against ``pkg_parts``) imported by a Python source text."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                keep = pkg_parts[:len(pkg_parts) - (node.level - 1)] if node.level - 1 <= len(pkg_parts) else ()
                base = ".".join([*keep, *([base] if base else [])])
            if base:
                out.append(base)
            out += [f"{base}.{a.name}" if base else a.name for a in node.names if a.name != "*"]
    return out


def python_closure(entry: str, files: set, read) -> set:
    """The entry script, the project-local modules it (transitively) imports, their package ``__init__`` files, and small data/config
    files that sit next to the entry or at the project root. ``files`` = project-relative posix paths; ``read(rel)`` -> text."""
    if entry not in files:
        return set()
    keep, todo = set(), [entry]
    roots = sorted({"", str(PurePosixPath(entry).parent) if "/" in entry else ""})

    def add_mod(dotted: str) -> None:
        parts = dotted.split(".")
        for root in roots:
            base = PurePosixPath(root) if root else PurePosixPath()
            for k in range(len(parts), 0, -1):
                pre = base.joinpath(*parts[:k])
                for cand in (f"{pre}.py", f"{pre}/__init__.py"):
                    cand = cand.lstrip("./") if cand.startswith("./") else cand
                    if cand in files and cand not in keep:
                        keep.add(cand)
                        todo.append(cand)
                for j in range(1, k):                                   # package __init__ files along the path
                    init = f"{base.joinpath(*parts[:j])}/__init__.py"
                    if init in files and init not in keep:
                        keep.add(init)
                        todo.append(init)
    keep.add(entry)
    while todo and len(keep) < NARROW_MAX_FILES:
        cur = todo.pop()
        parent = PurePosixPath(cur).parent
        pkg = tuple(x for x in parent.parts if x != ".")
        for mod in _imports_of(read(cur) or "", pkg):
            add_mod(mod)
    for f in files:                                                     # data/config next to the entry or at the root (not .env)
        d = str(PurePosixPath(f).parent)
        if (d in ("", ".", str(PurePosixPath(entry).parent))) and PurePosixPath(f).suffix.lower() in DATA_EXT:
            keep.add(f)
    return keep


def _junk(parent: Path, name: str) -> bool:
    skip = SKIP_DIRS - {"bin", "obj", "target", "dist", "build"}
    q = parent / name
    return name in skip or name in EXTRA_SKIP or q.is_symlink() or (q / "pyvenv.cfg").is_file()


def _prepare_narrow(source, dest: Path, limits: Limits, entry: str) -> Optional[list]:
    """Copy only the entry script, its local imports and small data files. None when that is not possible."""
    max_bytes = limits.max_workspace_mb * 1024 * 1024
    src = Path(source)
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    notes: list = []
    if src.is_dir():
        src = src.resolve()
        paths = {}
        for d, dirs, fs in os.walk(src, followlinks=False):
            dirs[:] = [x for x in dirs if not _junk(Path(d), x)]
            for f in fs:
                p = Path(d) / f
                if not p.is_symlink():
                    paths[p.relative_to(src).as_posix()] = p
        files = set(paths)

        def read(rel):
            try:
                return paths[rel].read_text(encoding="utf-8", errors="replace") if paths[rel].stat().st_size < 2_000_000 else ""
            except OSError:
                return ""
        keep = python_closure(entry, files, read)
        total = 0
        for rel in sorted(keep):
            p = paths[rel]
            if p.stat().st_size > 50 * 1024 * 1024 and rel != entry:
                continue
            total += p.stat().st_size
            if total > max_bytes:
                return None
            out = dest.joinpath(*rel.split("/"))
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, out)
            os.chmod(out, 0o666)
        for q in [dest, *[x for x in dest.rglob("*") if x.is_dir()]]:
            os.chmod(q, 0o777)
    else:
        try:
            zf = zipfile.ZipFile(src)
        except (zipfile.BadZipFile, OSError):
            return None
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            names = [i.filename.replace("\\", "/") for i in infos]
            if any("\x00" in n or n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in PurePosixPath(n).parts for n in names):
                return None
            tops = {PurePosixPath(n).parts[0] for n in names}
            strip = 1 if len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in names) else 0
            by = {}
            for i, n in zip(infos, names):
                parts = PurePosixPath(n).parts[strip:]
                mode = i.external_attr >> 16
                if (mode and stat.S_ISLNK(mode)) or i.flag_bits & 1 or not parts:
                    continue
                if set(parts[:-1]) & (SKIP_DIRS - {"bin", "obj", "target", "dist", "build"} | EXTRA_SKIP):
                    continue
                by["/".join(parts)] = i

            def read(rel):
                i = by[rel]
                if i.file_size > 2_000_000:
                    return ""
                with zf.open(i) as fh:
                    return fh.read(2_000_001).decode("utf-8", "replace")
            keep = python_closure(entry, set(by), read)
            total = 0
            for rel in sorted(keep):
                i = by[rel]
                total += i.file_size
                if total > max_bytes or (i.file_size > 1_048_576 and i.compress_size and i.file_size / i.compress_size > 200):
                    return None
                out = dest.joinpath(*rel.split("/"))
                out.resolve().relative_to(dest)
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(i) as s_, open(out, "wb") as o_:
                    shutil.copyfileobj(s_, o_)
                os.chmod(out, 0o666)
            for q in [dest, *[x for x in dest.rglob("*") if x.is_dir()]]:
                os.chmod(q, 0o777)
    if entry not in keep:
        raise WorkspaceRejected(f"'{entry}' was not found in the project, so there is nothing smaller to copy (check the file name)")
    notes.append(f"Project is larger than {limits.max_workspace_mb} MB, so only {entry} and {len(keep) - 1} related file(s) "
                 "(its local imports and small data/config files next to it) were copied into the container.")
    return notes


def prepare_workspace(source, dest: Path, limits: Limits, entry: str = "") -> list:
    """Copy the project into ``dest``. When it is too big and ``entry`` is a Python script, copy just that script's closure instead of refusing."""
    try:                                    # added: look first (fast scan), copy once; script runs in big projects are narrowed at once
        from . import quickprep
        quick_notes = quickprep.prepare(source, dest, limits, entry)
        if quick_notes is not None:
            return quick_notes
    except WorkspaceRejected:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception:  # noqa: BLE001 - the quick path must never be the reason a run fails: use the classic code below
        shutil.rmtree(dest, ignore_errors=True)
    try:
        return _prepare_full(source, dest, limits)
    except WorkspaceRejected as exc:
        too_big = "larger than" in str(exc) or ("expands beyond" in str(exc) and "zip bomb" not in str(exc))
        if not entry or not too_big:
            raise
        shutil.rmtree(dest, ignore_errors=True)
        try:
            narrowed = _prepare_narrow(source, dest, limits, entry)
        except WorkspaceRejected as exc2:
            shutil.rmtree(dest, ignore_errors=True)
            raise WorkspaceRejected(f"{exc} Narrowing to one script failed: {exc2}") from exc
        if narrowed is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise WorkspaceRejected(f"{exc} (copying only {entry} and its imports was not possible or still too large)") from exc
        return narrowed


# ------------------------------------------------------------------------------ execution
class _Capture:
    """Drains a pipe in a thread, keeps at most `cap` bytes, never blocks the child."""

    LIVE_CAP = 4 * 1024 * 1024          # the live preview stops after this much output per stream (the final result keeps its own cap)

    def __init__(self, stream, cap: int, on_text: Optional[Callable[[str], None]] = None):
        self.buf = bytearray()
        self.cap = cap
        self.truncated = False
        self._s = stream
        self._on_text = on_text
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._live = 0
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        try:
            while True:
                chunk = self._s.read1(65536)
                if not chunk:
                    break
                if self._on_text is not None:
                    if self._live < self.LIVE_CAP:
                        self._live += len(chunk)
                        self._safe_text(self._dec.decode(chunk))
                    elif self._live != -1:
                        self._live = -1
                        self._safe_text("\n[live preview stopped: output is very large; the final result keeps the capped output]\n")
                room = self.cap - len(self.buf)
                if room > 0:
                    self.buf += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass

    def _safe_text(self, text: str) -> None:
        try:
            if text:
                self._on_text(text)
        except Exception:  # noqa: BLE001 - a viewer problem never disturbs the child process handling
            pass

    def text(self) -> str:
        self.t.join(5)
        return bytes(self.buf).decode("utf-8", "replace")


_UNITS = {"b": 1 / 1048576, "kib": 1 / 1024, "kb": 1 / 1000, "mib": 1.0, "mb": 1.0, "gib": 1024.0, "gb": 1000.0}


def _mib(text: str) -> Optional[float]:
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1.0) if m else None
    except ValueError:
        return None


def _watch_container(exe: str, name: str, run_id: str, stop: threading.Event, interval: float = 2.0) -> None:
    """While the container runs, publish CPU/memory and its process list (read-only runtime queries)."""
    tick = 0
    while not stop.wait(interval):
        rc, out, _ = _cli([exe, "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}", name], 8)
        if rc == 0 and out.strip():
            cpu, _, rest = out.strip().splitlines()[0].partition("|")
            mem, _, pids = rest.partition("|")
            try:
                BUS.emit("stats", run_id, cpu=float(cpu.strip().rstrip("%") or 0), mem_mib=_mib(mem.split("/")[0]), mem_text=mem.strip(),
                         pids=pids.strip())
            except ValueError:
                pass
        if tick % 2 == 0 and not stop.is_set():
            rc, out, _ = _cli([exe, "top", name], 8)
            lines = [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []
            if len(lines) >= 1:
                header = lines[0].split()
                rows = [ln.split(None, max(len(header) - 1, 0)) for ln in lines[1:60]]
                BUS.emit("top", run_id, header=header, rows=rows)
        tick += 1


# ---- virtual screen snapshots (programs that run on the Xvfb screen of the GUI image) ---------------------------------------------
SCREEN_DISPLAY = ":99"                                   # the display the GUI wrapper (gui_screen.SCRIPT) and App Preview both start
_CAPTURE = (["import", "-silent", "-window", "root", "png:-"], ["import-im7.q16", "-silent", "-window", "root", "png:-"],
            ["import-im6.q16", "-silent", "-window", "root", "png:-"], ["magick", "import", "-silent", "-window", "root", "png:-"])
_PNG_MAGIC = b"\x89PNG"
_SCREEN_MAX = 8_000_000


def _snapshot(exe: str, name: str, timeout: float = 6.0) -> bytes:
    """One PNG of the container's virtual screen (``<runtime> exec ... import -window root png:-``), or b"" when there is none.
    Tries the known ImageMagick command names; a tool that exists but fails (no screen yet) is not retried under other names."""
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    for argv in _CAPTURE:
        try:
            p = subprocess.run([str(exe), "exec", "-e", f"DISPLAY={SCREEN_DISPLAY}", name] + argv, capture_output=True, timeout=timeout,
                               env=_env(), stdin=subprocess.DEVNULL, **kw)
        except (OSError, subprocess.TimeoutExpired):
            return b""
        if p.returncode == 0 and p.stdout[:4] == _PNG_MAGIC:
            return p.stdout
        text = (p.stderr or b"").decode("utf-8", "replace").lower()
        if not any(k in text for k in ("not found", "no such file", "executable file", "not installed")):
            return b""
    return b""


def _watch_screen(exe: str, name: str, run_id: str, stop: threading.Event, interval: float = 1.0) -> None:
    """While the container runs, publish ``screen`` events (PNG bytes) for the Live Run preview. Identical consecutive frames are
    published once; while nothing can be captured yet it polls gently instead of hammering the runtime."""
    last, fails = None, 0
    while not stop.is_set():
        png = _snapshot(exe, name)
        if png and len(png) < _SCREEN_MAX:
            fails = 0
            h = hash(png)
            if h != last:
                last = h
                BUS.emit("screen", run_id, png=png)
        else:
            fails += 1
        stop.wait(max(0.05, interval) if not fails else min(3.0, max(0.05, interval) * (2 ** min(fails, 3))))


def _is_gui_wrapped(argv) -> bool:
    """True for a command wrapped by ``gui_screen.apply_gui_screen`` (it starts Xvfb on :99, so there is a screen to look at)."""
    return len(argv) > 3 and list(argv[:2]) == ["sh", "-c"] and "Xvfb" in str(argv[2]) and f"DISPLAY={SCREEN_DISPLAY}" in str(argv[2]).replace(" ", "")


def run_plan(plan: Plan, approval: Optional[Approval], options: Optional[Options] = None,
             progress: Optional[Callable[[str], None]] = None,
             cancel: Optional[threading.Event] = None) -> ExecutionResult:
    """Run an APPROVED plan. Never raises; every outcome is an ExecutionResult."""
    options = options or Options()
    run_id = uuid.uuid4().hex[:8]
    _progress = progress or (lambda m: None)

    def log(m: str) -> None:
        _progress(m)
        BUS.emit("phase", run_id, text=m)
    cancel = cancel or threading.Event()
    BUS.register_cancel(run_id, cancel)
    c = plan.candidate
    res = ExecutionResult(status="error", candidate_id=c.id, argv=list(c.argv), image=plan.image,
                          network_enabled=plan.limits.network, limits=dict(vars(plan.limits)),
                          started=datetime.now(timezone.utc).isoformat())
    scratch = None
    name = ""
    exe = None
    t0 = time.monotonic()
    try:
        # 1. approval must match this exact plan
        if approval is None or approval.plan_hash != plan.plan_hash:
            res.status = "refused"
            res.notes.append("Refused: no approval for this exact command/image/limits/network setting.")
            return res
        if not c.argv or any((not isinstance(a, str)) or "\x00" in a for a in c.argv):
            res.status = "refused"
            res.notes.append("Refused: invalid command.")
            return res
        BUS.emit("start", run_id, title=c.display if hasattr(c, "display") else " ".join(c.argv), argv=list(c.argv), image=plan.image,
                 network=plan.limits.network, timeout_s=plan.limits.timeout_s, candidate=c.id)
        # 2. isolation must exist - there is deliberately no host fallback
        iso = isolation_status(options)
        if not iso["available"] and options.auto_start and iso.get("executable"):
            from .runtime_start import ensure_runtime        # installed but stopped: start it, never install it
            iso = ensure_runtime(options, progress=log, cancel=cancel)
            if iso.get("started") and iso["available"]:
                res.notes.append(f"{iso['backend']} was not running; it was started automatically for this run.")
        res.backend = iso.get("backend") or ""
        if not iso["available"]:
            res.status = "skipped"
            res.notes.append("Skipped, nothing was executed: " + iso["reason"])
            return res
        exe = iso["executable"]
        if not image_present(exe, plan.image):
            res.status = "skipped"
            res.notes.append(f"Skipped, nothing was executed: image '{plan.image}' is not available locally. "
                             f"Images are never pulled automatically; run: {iso['backend']} pull {plan.image}")
            return res
        # 3. disposable workspace copy
        scratch = _storage.new_scratch("be_ws_")
        workspace = scratch / "work"
        log("Copying project into a disposable workspace...")
        try:
            res.notes.extend(prepare_workspace(plan.source, workspace, plan.limits, entry=entry_script(plan.candidate.argv)))
            for n in res.notes:
                if n.startswith("Project is larger"):
                    _progress(n)
        except (WorkspaceRejected, ValueError, OSError) as exc:
            res.status = "refused"
            res.notes.append(f"Refused: {exc}")
            return res
        if "," in str(workspace):
            res.status = "refused"
            res.notes.append("Refused: scratch path contains a comma and cannot be mounted safely.")
            return res
        name = "be-" + uuid.uuid4().hex[:12]
        res.container_name = name
        cmd = container_command(exe, plan, workspace.resolve(), name)
        res.container_command = [str(x) for x in cmd]
        log(f"Running in container {name} (network {'ENABLED' if plan.limits.network else 'disabled'})...")
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=_env(), **kw)
        except OSError as exc:
            res.status = "error"
            res.notes.append(f"Could not start {iso['backend']}: {exc}")
            return res
        out = _Capture(proc.stdout, plan.limits.max_output_bytes, lambda t: BUS.emit("out", run_id, text=t))
        err = _Capture(proc.stderr, plan.limits.max_output_bytes, lambda t: BUS.emit("err", run_id, text=t))
        stop_watch = threading.Event()
        watcher = None
        if options.live_stats:
            watcher = threading.Thread(target=_watch_container, args=(exe, name, run_id, stop_watch), daemon=True)
            watcher.start()
        screen_watcher = None
        if _is_gui_wrapped(c.argv):                         # a window program on the virtual screen: Live Run shows its picture
            screen_watcher = threading.Thread(target=_watch_screen, args=(exe, name, run_id, stop_watch), daemon=True)
            screen_watcher.start()
        outcome = "done"
        while proc.poll() is None:
            if cancel.is_set():
                outcome = "cancelled"
            elif time.monotonic() - t0 > plan.limits.timeout_s:
                outcome = "timeout"
            if outcome != "done":
                _cli([exe, "kill", name], 20)
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    pass
                break
            time.sleep(0.1)
        stop_watch.set()
        if watcher is not None:
            watcher.join(3)
        if screen_watcher is not None:
            screen_watcher.join(3)
        res.stdout, res.stderr = out.text(), err.text()
        res.output_truncated = out.truncated or err.truncated
        code = proc.returncode
        if outcome == "timeout":
            res.status = "timeout"
            res.notes.append(f"Timed out after {plan.limits.timeout_s}s; the container was killed.")
        elif outcome == "cancelled":
            res.status = "cancelled"
            res.notes.append("Cancelled by user; the container was killed.")
        else:
            res.exit_code = code
            res.status = "completed" if code == 0 else "failed"
            if code == 125:
                res.status = "error"
                res.notes.append(f"{iso['backend']} could not start the container (see stderr).")
            elif code in (126, 127):
                res.notes.append("The command could not be started inside the image (not found / not executable).")
            elif code == 137:
                res.notes.append("Process was killed (exit 137) - commonly the memory limit was exceeded.")
        if res.output_truncated:
            res.notes.append(f"Output truncated to {plan.limits.max_output_bytes} bytes per stream.")
        return res
    except Exception as exc:  # the host application must never crash because a run failed
        res.status = "error"
        res.notes.append(f"Unexpected error: {type(exc).__name__}: {exc}")
        return res
    finally:
        BUS.unregister_cancel(run_id)
        try:
            if res.status in ("refused", "skipped", "error"):          # say WHY in the console, not only the status word
                for n in res.notes:
                    BUS.emit("err", run_id, text=f"{n}\n")
            BUS.emit("end", run_id, status=res.status, exit_code=res.exit_code, notes=list(res.notes),
                     duration_s=round(time.monotonic() - t0, 3))
        except Exception:  # noqa: BLE001
            pass
        if exe and name:
            _cli([exe, "rm", "-f", name], 20)          # best effort; --rm normally already did it
        if scratch is not None:
            res.workspace_cleaned, kept_note = _storage.finish_scratch(scratch)
            if kept_note:
                res.notes.append(kept_note)
        try:                                       # added: also clear every other leftover copy / stopped container of ours (setting)
            from . import cleanup as _cleanup
            _done = _cleanup.after_run(exe)
            if _done:
                res.notes.append("Cleared after the run: " + "; ".join(_done) + ".")
        except Exception:  # noqa: BLE001
            pass
        res.duration_s = round(time.monotonic() - t0, 3)
        res.finished = datetime.now(timezone.utc).isoformat()
        if options.log_dir:
            try:
                res.write_json(Path(options.log_dir) / f"{res.candidate_id.replace(':', '_')}_{res.started[:19].replace(':', '')}.json")
            except OSError:
                pass
