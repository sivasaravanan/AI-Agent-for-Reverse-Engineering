"""Runs approved setup actions. Only fixed catalog commands are accepted; output is streamed to the caller."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from setup_doctor import catalog as cat
from setup_doctor.catalog import Action
from setup_doctor.checks import CREATE_NO_WINDOW, SystemEnv, apply_known_paths

ProgressCb = Callable[[str], None]
ACTION_TIMEOUT_S = 3600
DAEMON_WAIT_S = 240
_EXE_OK = {"winget", "winget.exe", "brew", "docker", "docker.exe", "podman", "podman.exe", "docker desktop.exe", "wsl", "wsl.exe"}
_IMG_ARCHIVES = (".tar", ".tar.gz", ".tgz")


@dataclass
class ActionResult:
    id: str
    ok: bool
    returncode: Optional[int] = None
    seconds: float = 0.0
    message: str = ""
    tail: List[str] = field(default_factory=list)


def validate_action(a: Action, python_exe: Optional[str] = None) -> None:
    """Raise ValueError unless the action is one this module may ever run. Defence against tampered or invented actions."""
    argv = list(a.argv)
    if not argv or any((not isinstance(x, str)) or "\x00" in x or "\n" in x for x in argv):
        raise ValueError("Invalid command.")
    exe = Path(argv[0]).name.lower()
    python_exe = python_exe or SystemEnv.python
    if argv[0] == python_exe:
        if argv[1:4] == ["-m", "pip", "install"] and (len(argv) == 6 and argv[4] == "--user" and argv[5] in cat.PIP_PACKAGES):
            return
        if argv[1:4] == ["-m", "pip", "install"] and len(argv) == 5 and argv[4] in cat.PIP_PACKAGES:
            return
        raise ValueError("Only pip installs of the catalog packages are allowed.")
    if exe not in _EXE_OK:
        raise ValueError(f"Program not allowed: {exe}")
    if exe.startswith("winget"):
        if (argv[1:3] == ["install", "-e"] and argv[3] == "--id" and argv[4] in cat.WINGET_IDS.values()
                and argv[5:] in (["--accept-package-agreements", "--accept-source-agreements"],
                                 ["--accept-package-agreements", "--accept-source-agreements", "--silent"])):
            return
        raise ValueError("Only catalog winget installs are allowed.")
    if exe == "brew":
        if argv in (["brew", "install", "--cask", "docker"], ["brew", "install", "podman"]):
            return
        raise ValueError("Only catalog Homebrew installs are allowed.")
    if exe.startswith("wsl"):
        if argv[1:] == ["--install", "--no-distribution"]:
            return
        raise ValueError("Only 'wsl --install --no-distribution' is allowed.")
    if exe == "docker desktop.exe":
        if len(argv) == 1 and a.kind == "launch":
            return
        raise ValueError("Docker Desktop may only be started without arguments.")
    # docker / podman
    if argv[1] == "pull" and len(argv) == 3 and cat.IMAGE_RE.match(argv[2]) and not argv[2].startswith("-"):
        return
    if argv[1] == "load" and len(argv) == 4 and argv[2] == "-i" and argv[3].lower().endswith(_IMG_ARCHIVES) \
            and Path(argv[3]).is_file():
        return
    if exe.startswith("podman") and argv[1:] in (["machine", "init"], ["machine", "start"]):
        return
    if exe.startswith("docker") and argv[1:] == ["desktop", "start"]:
        return
    raise ValueError("Runtime command not allowed.")


EXIT_HELP = {
    4294967291: "Docker Desktop's installer stopped with exit -5. That almost always means one of: Windows virtualization / WSL 2 is not "
                "switched on, Windows is waiting for a restart, or the administrator window was not accepted.",
    2316632070: "winget reports that the installer failed (0x8A150006).",
    1223: "The Windows permission (administrator) window was declined or could not be shown.",
    1602: "The installation was cancelled.",
    1603: "The installer reported a fatal error (1603).",
    3010: "Installed; Windows needs a restart before it can be used.",
}


def explain_exit(rc: Optional[int]) -> str:
    if rc is None:
        return "The program could not be run."
    return EXIT_HELP.get(rc & 0xFFFFFFFF if rc < 0 else rc, f"Exit code {rc}.")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def elevated_command(argv) -> List[str]:
    """Wrap a catalog command so Windows shows its permission (UAC) window; the exit code of the elevated process is returned."""
    exe, *args = [str(a) for a in argv]
    if any(ch in a for a in args for ch in ' \t"\'`$;&|'):
        raise ValueError("Refusing to elevate a command whose arguments contain spaces or shell metacharacters.")
    arglist = "@(" + ",".join(_ps_quote(a) for a in args) + ")" if args else ""
    script = (f"try {{ $p = Start-Process -FilePath {_ps_quote(exe)} " + (f"-ArgumentList {arglist} " if arglist else "") +
              "-Verb RunAs -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } catch { Write-Output $_.Exception.Message; exit 1223 }")
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


def _child_env():
    """Environment for child processes with the institution's proxy / CA bundle / index settings (None = inherit as is)."""
    try:
        from prerequisites.network import child_env
        return child_env()
    except Exception:  # noqa: BLE001
        return None


def _daemon_up(env, runtime_exe: str) -> bool:
    rc, _o, _e = env.run([runtime_exe, "info", "--format", "{{.ServerVersion}}"], timeout=15)
    return rc == 0


def run_actions(actions: Sequence[Action], env=None, progress: Optional[ProgressCb] = None,
                cancel: Optional[threading.Event] = None, timeout_s: int = ACTION_TIMEOUT_S,
                daemon_wait_s: int = DAEMON_WAIT_S) -> List[ActionResult]:
    """Run approved actions in order; stop at the first failure. Never raises."""
    env = env or SystemEnv()
    say = progress or (lambda _m: None)
    results: List[ActionResult] = []
    for a in actions:
        if cancel is not None and cancel.is_set():
            results.append(ActionResult(a.id, False, message="Cancelled."))
            break
        try:
            validate_action(a, getattr(env, "python", None))
        except ValueError as exc:
            say(f"REFUSED: {a.label}: {exc}")
            results.append(ActionResult(a.id, False, message=f"Refused: {exc}"))
            break
        say(f"\n== {a.label}" + ("  (Windows may ask for permission)" if a.admin else ""))
        say("$ " + a.display)
        t0 = time.monotonic()
        res = _launch(a, say, cancel, timeout_s) if a.kind == "launch" else _stream(a, say, cancel, timeout_s)
        res.seconds = round(time.monotonic() - t0, 1)
        if res.ok and a.sets_path:
            for folder in apply_known_paths(env):
                say(f"Added to this app's search path: {folder}")
        if res.ok and a.wait_for_daemon:
            runtime = _runtime_for(a, env)
            if runtime:
                say("Waiting for the container runtime to start (up to %d s)..." % daemon_wait_s)
                end = time.monotonic() + daemon_wait_s
                up = False
                while time.monotonic() < end and not (cancel is not None and cancel.is_set()):
                    if _daemon_up(env, runtime):
                        up = True
                        break
                    time.sleep(3)
                if not up:
                    res.ok, res.message = False, "The runtime did not answer in time. Finish its first-start steps, then click Re-check."
        results.append(res)
        say(("OK" if res.ok else "FAILED") + (f" - {res.message}" if res.message else ""))
        if not res.ok:
            break
    return results


def _runtime_for(a: Action, env) -> Optional[str]:
    from setup_doctor.checks import find_runtime_exe
    return find_runtime_exe(env)[1]


def _launch(a: Action, say: ProgressCb, cancel, timeout_s: int) -> ActionResult:
    try:
        kw = {"creationflags": 0x00000008} if os.name == "nt" else {}      # DETACHED_PROCESS
        subprocess.Popen(list(a.argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        return ActionResult(a.id, True, 0, message="Started.")
    except OSError as exc:
        return ActionResult(a.id, False, None, message=f"Could not start: {exc}")


def _stream(a: Action, say: ProgressCb, cancel, timeout_s: int) -> ActionResult:
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    tail: List[str] = []
    argv = list(a.argv)
    if a.elevate and os.name == "nt":
        try:
            argv = elevated_command(argv)
        except ValueError as exc:
            return ActionResult(a.id, False, None, message=str(exc))
        say("A Windows permission window will open: accept it. The installer then runs with administrator rights (its own output is not shown here).")
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=_child_env(), **kw)
    except OSError as exc:
        return ActionResult(a.id, False, None, message=f"Could not start the program: {exc}")
    deadline = time.monotonic() + timeout_s

    def killer() -> None:
        while p.poll() is None:
            if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                try:
                    p.kill()
                except OSError:
                    pass
                return
            time.sleep(0.3)
    threading.Thread(target=killer, daemon=True).start()
    last = ""
    assert p.stdout is not None
    for line in p.stdout:                       # winget/docker redraw progress with \r; show distinct, non-empty lines
        line = re.sub(r"[\x00-\x08\x0b-\x1f]", "", line).rstrip()
        if line and line != last:
            say(line)
            tail.append(line)
            del tail[:-15]
            last = line
    rc = p.wait()
    if cancel is not None and cancel.is_set():
        return ActionResult(a.id, False, rc, message="Cancelled.", tail=tail)
    if time.monotonic() > deadline:
        return ActionResult(a.id, False, rc, message="Timed out.", tail=tail)
    if rc not in a.ok_codes:
        return ActionResult(a.id, False, rc, message=f"Exit code {rc & 0xFFFFFFFF if rc < 0 else rc}. " + explain_exit(rc), tail=tail)
    return ActionResult(a.id, True, rc, message=explain_exit(rc) if rc == 3010 else "", tail=tail)
