"""Everything the installer needs from the machine, behind one object so tests (and dry-runs) can substitute it."""
from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from core.runtime_paths import data_root, is_frozen
from prerequisites import tools_env

NO_WINDOW = 0x08000000


@dataclass
class RunResult:
    rc: Optional[int]
    output: str = ""
    timed_out: bool = False
    cancelled: bool = False


Quick = Callable[[Sequence[str], float], Tuple[Optional[int], str, str]]
LongRun = Callable[..., RunResult]


def _quick(cmd: Sequence[str], timeout: float = 20.0) -> Tuple[Optional[int], str, str]:
    kw = {"creationflags": NO_WINDOW} if os.name == "nt" else {}
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


def _long_run(cmd: Sequence[str], timeout: float = 1800.0, on_line: Optional[Callable[[str], None]] = None,
              cancel: Optional[threading.Event] = None, env: Optional[Dict[str, str]] = None,
              cwd: Optional[str] = None, interactive: bool = False) -> RunResult:
    kw: dict = {"creationflags": NO_WINDOW} if os.name == "nt" and not interactive else {}
    try:
        proc = subprocess.Popen([str(c) for c in cmd], stdout=None if interactive else subprocess.PIPE,
                                stderr=None if interactive else subprocess.STDOUT,
                                stdin=None if interactive else subprocess.DEVNULL, env=env, cwd=cwd, **kw)
    except OSError as exc:
        return RunResult(None, f"Could not start: {exc}")
    lines: List[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            if on_line and line:
                on_line(line)

    t = threading.Thread(target=pump, daemon=True)
    if proc.stdout is not None:
        t.start()
    deadline = time.monotonic() + timeout
    cancelled = timed_out = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
        elif time.monotonic() > deadline:
            timed_out = True
        if cancelled or timed_out:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(0.2)
    if proc.stdout is not None:
        t.join(5)
    return RunResult(proc.returncode, "\n".join(lines[-400:]), timed_out, cancelled)


def _is_admin() -> bool:
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:  # noqa: BLE001
        return False


def _system_python() -> Optional[str]:
    """Interpreter that can create the private tool venv. From source: the running Python. In an EXE: a Python on PATH."""
    if not is_frozen():
        return sys.executable
    for name in ("python", "python3", "py"):
        hit = shutil.which(name)
        if hit and "WindowsApps" not in hit:      # skip the Microsoft Store stub
            return hit
    return None


@dataclass
class Env:
    platform: str = "linux"                  # windows | linux | darwin
    arch: str = "amd64"                      # amd64 | arm64
    which: Callable[[str], Optional[str]] = field(default=lambda exe: tools_env.find_tool(exe))
    quick: Quick = field(default=_quick)
    run_long: LongRun = field(default=_long_run)
    is_admin: bool = False
    root: Path = field(default_factory=data_root)
    frozen: bool = False
    python: Optional[str] = None
    interactive: bool = False                # CLI with a terminal: sudo may prompt for a password
    windows_build: int = 0

    @property
    def winget(self) -> Optional[str]:
        return self.which("winget") if self.platform == "windows" else None

    @classmethod
    def real(cls, root: Optional[Path] = None, interactive: bool = False) -> "Env":
        plat = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
        mach = _platform.machine().lower()
        build = 0
        if plat == "windows":
            try:
                build = int(_platform.version().split(".")[-1])
            except (ValueError, IndexError):
                build = 0
        r = root or data_root()
        return cls(platform=plat, arch="arm64" if mach in ("arm64", "aarch64") else "amd64",
                   which=lambda exe: tools_env.find_tool(exe, r), is_admin=_is_admin(), root=r, frozen=is_frozen(),
                   python=_system_python(), interactive=interactive, windows_build=build)

    @property
    def downloads_dir(self) -> Path:
        return self.root / "downloads"

    @property
    def tools_dir(self) -> Path:
        return tools_env.tools_dir(self.root)
