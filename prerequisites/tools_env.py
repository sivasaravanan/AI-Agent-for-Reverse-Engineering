"""Make tools installed by this app (or by an installer after the process started) visible to this process.

Private, no-administrator tool locations (all under the writable data folder):
    tools/venv   - Python tools (ruff, sqlfluff) installed into a private virtual environment
    tools/npm    - Node tools (eslint, typescript, pyright) installed with ``npm --prefix``
Known vendor install folders are added too, so a freshly installed Docker/Podman is found without restarting the app.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from core.runtime_paths import data_root


def tools_dir(root: Optional[Path] = None) -> Path:
    return (root or data_root()) / "tools"


def venv_dir(root: Optional[Path] = None) -> Path:
    return tools_dir(root) / "venv"


def venv_python(root: Optional[Path] = None) -> Path:
    v = venv_dir(root)
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def npm_prefix(root: Optional[Path] = None) -> Path:
    return tools_dir(root) / "npm"


def private_bin_dirs(root: Optional[Path] = None) -> List[Path]:
    v, n = venv_dir(root), npm_prefix(root)
    return [v / ("Scripts" if os.name == "nt" else "bin"), n / ("" if os.name == "nt" else "bin"), n / "node_modules" / ".bin"]


def _env_path(var: str, *parts: str) -> Optional[Path]:
    base = os.environ.get(var)
    return Path(base, *parts) if base else None


def vendor_dirs(platform: str = "") -> List[Path]:
    platform = platform or ("windows" if os.name == "nt" else sys.platform)
    cands: List[Optional[Path]] = []
    if platform.startswith("win"):
        cands += [_env_path("ProgramFiles", "Docker", "Docker", "resources", "bin"),
                  _env_path("LOCALAPPDATA", "Programs", "DockerDesktop", "resources", "bin"),
                  _env_path("ProgramFiles", "RedHat", "Podman"),
                  _env_path("LOCALAPPDATA", "Programs", "RedHat", "Podman"),
                  _env_path("LOCALAPPDATA", "Programs", "Ollama"),
                  _env_path("ProgramFiles", "nodejs"), _env_path("ProgramFiles", "dotnet"),
                  _env_path("ProgramFiles", "Go", "bin"), _env_path("ProgramFiles", "Git", "cmd")]
    elif platform == "darwin":
        cands += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/Applications/Docker.app/Contents/Resources/bin")]
    return [c for c in cands if c is not None]


def registry_path() -> List[str]:
    """Machine + user PATH straight from the registry (what a new process would get). Windows only; [] elsewhere."""
    if os.name != "nt":
        return []
    out: List[str] = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    val, _t = winreg.QueryValueEx(key, "Path")
                    out += [os.path.expandvars(p) for p in str(val).split(os.pathsep) if p]
            except OSError:
                continue
    except ImportError:
        pass
    return out


def activate(root: Optional[Path] = None, platform: str = "") -> List[str]:
    """Prepend existing private/vendor bin folders (and, on Windows, registry PATH entries) to this process's PATH.
    Returns the folders that were added. Idempotent."""
    current = os.environ.get("PATH", "").split(os.pathsep)
    seen = {os.path.normcase(p) for p in current}
    added: List[str] = []
    for d in [*private_bin_dirs(root), *vendor_dirs(platform), *(Path(p) for p in registry_path())]:
        try:
            if d.is_dir() and os.path.normcase(str(d)) not in seen:
                added.append(str(d))
                seen.add(os.path.normcase(str(d)))
        except OSError:
            continue
    if added:
        os.environ["PATH"] = os.pathsep.join(added + current)
    return added


def find_tool(exe: str, root: Optional[Path] = None) -> Optional[str]:
    """PATH first, then the private/vendor folders (without mutating PATH)."""
    hit = shutil.which(exe)
    if hit:
        return hit
    extra = os.pathsep.join(str(d) for d in [*private_bin_dirs(root), *vendor_dirs()] if d.is_dir())
    return shutil.which(exe, path=extra) if extra else None
