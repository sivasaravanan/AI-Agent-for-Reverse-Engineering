"""Detect what is already installed - always before offering to install anything."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from prerequisites.catalog import BY_ID, CATALOG, CONTAINER_RUNTIMES, Prereq
from prerequisites.env import Env

READY, INSTALLED, MISSING, UNSUPPORTED = "ready", "installed", "missing", "unsupported"


@dataclass
class Status:
    id: str
    name: str
    category: str
    state: str                 # ready | installed (found but not usable yet) | missing | unsupported
    version: str = ""
    path: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def _version(env: Env, exe_path: str, args: Tuple[str, ...]) -> str:
    rc, out, err = env.quick([exe_path, *args], 15)
    text = (out or err).strip()
    return text.splitlines()[0].strip()[:120] if rc is not None and text else ""


def _wsl(env: Env) -> Status:
    p = BY_ID["wsl2"]
    if env.platform != "windows":
        return Status(p.id, p.name, p.category, UNSUPPORTED, detail="Windows only.")
    exe = env.which("wsl")
    if not exe:
        return Status(p.id, p.name, p.category, MISSING, detail="wsl.exe not found (Windows too old or feature disabled).")
    rc, out, err = env.quick([exe, "--status"], 20)
    text = (out + err).replace("\x00", "")
    if rc == 0 and "not installed" not in text.lower():
        return Status(p.id, p.name, p.category, READY, path=exe, detail=text.strip().splitlines()[0][:120] if text.strip() else "")
    return Status(p.id, p.name, p.category, MISSING, path=exe, detail=text.strip()[:200] or "WSL is not set up.")


def _python(env: Env) -> Status:
    p = BY_ID["python"]
    if env.python and not env.frozen:
        return Status(p.id, p.name, p.category, READY, _version(env, env.python, ("--version",)), env.python, "The interpreter running this app.")
    exe = env.python or env.which("python3") or env.which("python")
    if exe:
        return Status(p.id, p.name, p.category, READY, _version(env, exe, ("--version",)), exe)
    return Status(p.id, p.name, p.category, MISSING, detail="No Python on PATH (needed only for the private Ruff/SQLFluff tools).")


def detect(prereq: Prereq, env: Env) -> Status:
    if prereq.id == "wsl2":
        return _wsl(env)
    if prereq.id == "python":
        return _python(env)
    if env.platform not in prereq.methods and prereq.category != "analyzer":
        # Not installable here by this app, but it may still be present; check below, otherwise mark missing with a hint.
        pass
    exe_path = ""
    for exe in prereq.exes:
        hit = env.which(exe)
        if hit:
            exe_path = hit
            break
    if not exe_path:
        return Status(prereq.id, prereq.name, prereq.category, MISSING)
    ver = _version(env, exe_path, prereq.version_args)
    if prereq.category == "container-runtime":
        rc, out, err = env.quick([exe_path, "info"], 30)
        if rc == 0:
            return Status(prereq.id, prereq.name, prereq.category, READY, ver, exe_path)
        hint = ("Start Docker Desktop and wait until it reports 'running'." if prereq.id == "docker" else
                "Run 'podman machine init' and 'podman machine start' (Windows/macOS), or check the service.")
        return Status(prereq.id, prereq.name, prereq.category, INSTALLED, ver, exe_path,
                      f"Installed but not usable yet: {(err or out).strip()[:160]} {hint}".strip())
    return Status(prereq.id, prereq.name, prereq.category, READY, ver or "found", exe_path)


def detect_all(env: Env, ids: Optional[Iterable[str]] = None) -> List[Status]:
    wanted = list(ids) if ids is not None else [p.id for p in CATALOG]
    return [detect(BY_ID[i], env) for i in wanted if i in BY_ID and not (BY_ID[i].id == "wsl2" and env.platform != "windows")]


def container_ready(statuses: Iterable[Status]) -> Tuple[bool, str]:
    for s in statuses:
        if s.id in CONTAINER_RUNTIMES and s.state == READY:
            return True, s.id
    return False, ""


def container_installed(statuses: Iterable[Status]) -> Optional[Status]:
    return next((s for s in statuses if s.id in CONTAINER_RUNTIMES and s.state == INSTALLED), None)
