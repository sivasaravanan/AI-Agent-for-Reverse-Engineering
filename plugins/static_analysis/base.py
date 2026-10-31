"""Shared helpers: tool discovery, bounded subprocess execution, adapter base class."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import Finding, Options, ToolRun

MAX_CAPTURE = 200_000
_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
CREATE_NO_WINDOW = 0x08000000


@dataclass
class Context:
    root: Path
    files: dict                      # language -> [Path]
    options: Options
    scratch: Path
    cancel: threading.Event
    log: Callable[[str], None] = lambda msg: None


@dataclass
class RunResult:
    outcome: str                     # done | timeout | cancelled | oserror
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0


def sanitized_env() -> dict:
    """Parent environment minus anything that looks like a credential."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"})
    return env


def find_tool(name: str, options: Options) -> Optional[str]:
    override = options.tool_paths.get(name)
    if override and Path(override).exists():
        return str(override)
    return shutil.which(name)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=10)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_command(cmd: list, cwd, timeout: float, cancel: threading.Event) -> RunResult:
    """Run without a shell, no stdin, no console window, hard timeout, cancellable."""
    t0 = time.monotonic()
    kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=sanitized_env(), **kwargs)
    except OSError as exc:
        return RunResult("oserror", stderr=str(exc), duration_s=time.monotonic() - t0)
    outcome = "done"
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                outcome = "cancelled"
            elif time.monotonic() - t0 > timeout:
                outcome = "timeout"
            if outcome != "done":
                _kill(proc)
                try:
                    out, err = proc.communicate(timeout=10)
                except Exception:
                    out, err = b"", b""
                break
    dec = lambda b: (b or b"").decode("utf-8", "replace")[:MAX_CAPTURE]
    return RunResult(outcome, None if outcome != "done" else proc.returncode,
                     dec(out), dec(err), time.monotonic() - t0)


def tool_version(exe: str, cwd, cancel: threading.Event, args=("--version",)) -> str:
    r = run_command([exe, *args], cwd, 20, cancel)
    text = (r.stdout or r.stderr).strip()
    return text.splitlines()[0].strip() if text else ""


def rel_path(root: Path, p) -> str:
    q = Path(str(p))
    if not q.is_absolute():
        q = root / q
    try:
        return q.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return str(p).replace("\\", "/")


def make_run(analyzer: str, res: RunResult, cmd: list, version: str = "", status: str = "",
             note: str = "", files_checked: int = 0) -> ToolRun:
    if not status:
        status = {"timeout": "timeout", "cancelled": "cancelled", "oserror": "error"}.get(res.outcome, "ok")
    return ToolRun(analyzer=analyzer, status=status, command=[str(c) for c in cmd], version=version,
                   exit_code=res.exit_code, stdout=res.stdout, stderr=res.stderr,
                   duration_s=round(res.duration_s, 3), note=note, files_checked=files_checked)


def unavailable(analyzer: str, tool: str, hint: str) -> ToolRun:
    return ToolRun(analyzer=analyzer, status="unavailable",
                   note=f"'{tool}' was not found on PATH. Nothing was run and nothing was installed. {hint}")


class Adapter:
    """Subclass, set `name` / `languages`, implement run(); then call register_adapter(cls)."""
    name = "base"
    languages: tuple = ()

    def run(self, ctx: Context) -> tuple:
        """Return (list[Finding], list[ToolRun])."""
        raise NotImplementedError

    @staticmethod
    def cap(findings: list, ctx: Context, runs: list) -> list:
        limit = ctx.options.max_findings_per_tool
        if len(findings) > limit:
            if runs:
                runs[-1].note = (runs[-1].note + f" Findings truncated to {limit}.").strip()
            return findings[:limit]
        return findings
