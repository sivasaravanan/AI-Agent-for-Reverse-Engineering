"""Environment and tool-version inventory. Only reads versions of tools on PATH (`<tool> --version`,
5 s timeout). Missing tools are reported as 'not installed' - never guessed."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, Iterable, Optional

DEFAULT_TOOLS = ("git", "node", "npm", "dotnet", "java", "javac", "ruff", "pyright", "docker", "pytest")


def _probe(tool: str) -> Dict[str, Any]:
    path = shutil.which(tool)
    if not path:
        return {"installed": False, "version": None}
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        out = (p.stdout or p.stderr).strip().splitlines()
        return {"installed": True, "path": path, "version": out[0][:120] if out else None, "exit_code": p.returncode}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"installed": True, "path": path, "version": None, "error": exc.__class__.__name__}


def collect_environment(tools: Optional[Iterable[str]] = DEFAULT_TOOLS, probe: bool = True) -> Dict[str, Any]:
    env: Dict[str, Any] = {
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "platform": platform.platform(), "machine": platform.machine(),
    }
    try:
        import tkinter
        env["tkinter"] = str(tkinter.TkVersion)
    except Exception:  # noqa: BLE001
        env["tkinter"] = None
    env["tools"] = {t: _probe(t) for t in (tools or ())} if probe else {}
    return env
