"""Where the application finds its read-only resources and where it may write (works for source, install and standalone EXE).

* ``resource_root()`` - folder with ``config/``, ``database/schema.sql``, ``docs/`` (the bundle folder inside a PyInstaller EXE).
* ``data_root()``     - writable folder for ``data/``, ``logs/``, ``reports/``, downloads and private tools.

Behaviour is unchanged where it already worked: from source both are the project folder, and a standalone EXE keeps writing next to
its bundle when that folder is writable. Only when it is NOT writable (EXE installed under Program Files, locked-down institutional
PCs) data moves to ``%LOCALAPPDATA%\\AISoftwareEngineeringAgent`` so the application no longer fails there. A file named
``portable.flag`` next to the EXE forces the EXE folder; ``AISEA_HOME`` overrides everything.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Dict

APP_DIRNAME = "AISoftwareEngineeringAgent"
_WRITABLE_CACHE: Dict[str, bool] = {}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def exe_dir() -> Path:
    return Path(sys.executable).resolve().parent if is_frozen() else resource_root()


def _writable(folder: Path) -> bool:
    """Real write test (os.access is unreliable for Windows ACLs such as Program Files). Cached per folder for the process."""
    key = str(folder)
    if key not in _WRITABLE_CACHE:
        probe = folder / f".write_test_{uuid.uuid4().hex[:8]}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            _WRITABLE_CACHE[key] = True
        except OSError:
            _WRITABLE_CACHE[key] = False
    return _WRITABLE_CACHE[key]


def _user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIRNAME


def data_root() -> Path:
    override = os.environ.get("AISEA_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if not is_frozen():
        return resource_root()
    if (exe_dir() / "portable.flag").exists():
        return exe_dir()
    res = resource_root()
    return res if _writable(res) else _user_data_dir()


def machine_policy_path() -> Path:
    """Admin/GPO-deployed policy (overrides user settings and cannot be changed from the app)."""
    if os.name == "nt":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP_DIRNAME / "policy.json"
    return Path("/etc/aisea/policy.json")


def ensure_dirs(root: Path) -> None:
    for sub in ("data", "logs", "reports"):
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def seed_config(src: Path | None = None, dst: Path | None = None) -> list:
    """Standalone EXE only: copy the bundled ``config/`` files that are missing from the writable data folder, so settings,
    profiles and policy can be edited there (existing files are never overwritten). Returns the files copied."""
    import shutil
    src = (src or resource_root()) / "config"
    dst = (dst or data_root()) / "config"
    copied: list = []
    if not src.is_dir() or src.resolve() == dst.resolve():
        return copied
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            target = dst / f.name
            if f.is_file() and not target.exists():
                shutil.copy2(f, target)
                copied.append(f.name)
    except OSError:
        pass
    return copied
