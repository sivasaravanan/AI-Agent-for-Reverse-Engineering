"""Execution environment service (Phase 4): isolation detection and tool versions. Never installs anything."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict


@dataclass
class IsolationInfo:
    available: bool = False
    backend: str = ""
    reason: str = ""
    version: str = ""
    executable: str = ""


def detect_isolation(options=None) -> IsolationInfo:
    """Fail closed: unavailable unless a container runtime answers ``info``."""
    try:
        from plugins.build_execution import isolation_status
        st = isolation_status(options)
    except Exception as exc:  # noqa: BLE001
        return IsolationInfo(reason=f"Isolation check failed: {type(exc).__name__}: {exc}")
    return IsolationInfo(available=bool(st.get("available")), backend=st.get("backend") or "",
                         reason=st.get("reason") or "", version=st.get("version") or "",
                         executable=st.get("executable") or "")


_TOOL_VERSION_ARGS = {"python": [sys.executable, "--version"], "ruff": ["ruff", "--version"],
                      "pyright": ["pyright", "--version"], "node": ["node", "--version"],
                      "eslint": ["eslint", "--version"], "tsc": ["tsc", "--version"],
                      "dotnet": ["dotnet", "--version"], "javac": ["javac", "-version"],
                      "sqlfluff": ["sqlfluff", "--version"], "docker": ["docker", "--version"],
                      "podman": ["podman", "--version"], "ollama": ["ollama", "--version"]}


def tool_versions(timeout: float = 8.0) -> Dict[str, str]:
    """Version string per tool, or 'not installed'. Runs only fixed, read-only version queries."""
    out: Dict[str, str] = {"platform": platform.platform()}
    for name, cmd in _TOOL_VERSION_ARGS.items():
        if name != "python" and shutil.which(cmd[0]) is None:
            out[name] = "not installed"
            continue
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            out[name] = ((p.stdout or p.stderr).strip().splitlines() or ["unknown"])[0][:120]
        except (OSError, subprocess.SubprocessError) as exc:
            out[name] = f"error: {type(exc).__name__}"
    return out
