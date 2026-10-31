"""Start an installed-but-stopped container runtime (Docker / Podman) when a run needs it.

Rules: only fixed commands (no shell, no user text); only when the runtime executable is already installed - nothing is ever
installed or downloaded here (a missing Podman machine is reported, not created); a run is still refused unless a runtime
really answers afterwards. Docker Desktop / a Podman machine can take a minute: progress is reported and a cancel Event is honoured.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import Options
from .sandbox import CREATE_NO_WINDOW, _cli, isolation_status

DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (Windows)


def _windows_docker_desktop() -> Optional[str]:
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for rel in ("Docker/Docker/Docker Desktop.exe", "Programs/Docker/Docker/Docker Desktop.exe"):
            p = Path(base) / rel
            if p.exists():
                return str(p)
    return None


def start_actions(backend: str, exe: str, system: Optional[str] = None) -> List[Dict]:
    """Ordered list of start attempts: {'label', 'argv', 'detach', 'timeout'}. Pure function (easy to test)."""
    system = system or platform.system()
    acts: List[Dict] = []
    if backend == "docker":
        if system == "Windows":
            acts.append({"label": "docker desktop start", "argv": [exe, "desktop", "start"], "detach": False, "timeout": 120})
            dd = _windows_docker_desktop()
            if dd:
                acts.append({"label": "Docker Desktop", "argv": [dd], "detach": True, "timeout": 0})
        elif system == "Darwin":
            acts.append({"label": "Docker Desktop", "argv": ["open", "-g", "-a", "Docker"], "detach": False, "timeout": 20})
        else:
            acts.append({"label": "systemctl --user start docker", "argv": ["systemctl", "--user", "start", "docker"], "detach": False, "timeout": 60})
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                acts.append({"label": "systemctl start docker", "argv": ["systemctl", "start", "docker"], "detach": False, "timeout": 60})
            elif shutil.which("sudo"):
                acts.append({"label": "sudo -n systemctl start docker", "argv": ["sudo", "-n", "systemctl", "start", "docker"],
                             "detach": False, "timeout": 60})
    elif backend == "podman":
        if system in ("Windows", "Darwin"):
            acts.append({"label": "podman machine start", "argv": [exe, "machine", "start"], "detach": False, "timeout": 180})
        else:
            acts.append({"label": "systemctl --user start podman.socket", "argv": ["systemctl", "--user", "start", "podman.socket"],
                         "detach": False, "timeout": 30})
    return acts


def _podman_machine_missing(exe: str) -> bool:
    rc, out, _ = _cli([exe, "machine", "list", "--format", "json"], 20)
    if rc != 0:
        return False
    try:
        return len(json.loads(out or "[]")) == 0
    except ValueError:
        return False


def _manual_hint(backend: str, system: str) -> str:
    if backend == "docker":
        return {"Windows": "Open Docker Desktop and wait until it says 'Engine running'.",
                "Darwin": "Open Docker Desktop and wait until it says 'Engine running'."}.get(
            system, "Run: sudo systemctl start docker   (or start your rootless Docker service).")
    return ("Run: podman machine start   (first time: podman machine init)." if system in ("Windows", "Darwin")
            else "Check: podman info   (Podman on Linux needs no daemon; the error above says what is wrong).")


def ensure_runtime(options: Optional[Options] = None, progress: Optional[Callable[[str], None]] = None,
                   cancel: Optional[threading.Event] = None, wait_s: float = 120.0, actions: Optional[List[Dict]] = None,
                   poll_s: float = 2.0, system: Optional[str] = None) -> Dict:
    """Return an ``isolation_status`` dict, trying to start the runtime first when it is installed but not answering.
    The result adds ``started`` (we launched something), ``attempts`` (what we tried) and, on failure, a manual ``hint``."""
    options = options or Options()
    say = progress or (lambda m: None)
    cancel = cancel or threading.Event()
    system = system or platform.system()
    st = isolation_status(options)
    st.update(started=False, attempts=[])
    if st["available"] or not st.get("executable"):
        return st                                               # ready, or not installed (we never install anything)
    backend, exe = st["backend"], st["executable"]
    if backend == "podman" and system in ("Windows", "Darwin") and _podman_machine_missing(exe):
        st["hint"] = "No Podman machine exists yet. Run once: podman machine init   (it downloads a VM image), then try again."
        st["attempts"].append("podman machine list: none")
        return st
    todo = actions if actions is not None else start_actions(backend, exe, system)
    for act in todo:
        if cancel.is_set():
            break
        say(f"{backend} is installed but not running - starting it ({act['label']})...")
        try:
            if act.get("detach"):
                kw = {"creationflags": DETACHED, "close_fds": True} if os.name == "nt" else {"start_new_session": True}
                subprocess.Popen(act["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                rc = 0
            else:
                rc, _, err = _cli(act["argv"], act.get("timeout", 60))
        except OSError as exc:
            rc, err = None, str(exc)
        st["attempts"].append(f"{act['label']}: {'launched' if rc == 0 else 'failed'}")
        if rc != 0:
            continue
        st["started"] = True
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait_s and not cancel.is_set():
            cur = isolation_status(options)
            if cur["available"]:
                cur.update(started=True, attempts=st["attempts"])
                say(f"{backend} is ready ({int(time.monotonic() - t0)}s).")
                return cur
            say(f"Waiting for {backend} to become ready... {int(time.monotonic() - t0)}s")
            cancel.wait(poll_s)
    final = isolation_status(options)
    final.update(started=st["started"], attempts=st["attempts"])
    if not final["available"]:
        final["hint"] = _manual_hint(backend, system)
        final["reason"] = (final.get("reason") or "") + " | Automatic start did not work (" + ("; ".join(st["attempts"]) or "no start action") \
            + "). " + final["hint"]
    return final
