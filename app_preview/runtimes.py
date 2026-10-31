"""Container runtime detection for the preview: Docker, Podman, nerdctl (containerd), Finch, or any docker-compatible CLI.

All of them accept the same ``run/exec/kill/rm/stats/top/image inspect`` commands, so the preview treats them uniformly.
Nothing is installed or started here; ``plugins.build_execution.runtime_start`` still owns auto-start for docker/podman.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plugins.build_execution.sandbox import _cli

from .models import BACKENDS, PreviewOptions

KNOWN = ("docker", "podman", "nerdctl", "finch")


def find_runtime(options: PreviewOptions, prefer: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """(backend name, executable) or (None, None). ``custom`` uses options.custom_exe as given."""
    backend = prefer or options.backend
    if backend not in BACKENDS:
        return None, None
    if backend == "custom":
        exe = options.custom_exe.strip()
        if exe and (Path(exe).exists() or shutil.which(exe)):
            return "custom", str(Path(exe)) if Path(exe).exists() else (shutil.which(exe) or exe)
        return None, None
    names = KNOWN if backend == "auto" else (backend,)
    for n in names:
        override = options.tool_paths.get(n)
        exe = override if override and Path(override).exists() else shutil.which(n)
        if exe:
            return n, exe
    return None, None


def status(options: Optional[PreviewOptions] = None) -> Dict:
    """{'available', 'backend', 'executable', 'version', 'reason'} - never raises, never starts anything."""
    options = options or PreviewOptions()
    name, exe = find_runtime(options)
    if not exe:
        what = "the custom executable" if options.backend == "custom" else "docker/podman/nerdctl/finch"
        return {"available": False, "backend": None, "executable": None, "version": "",
                "reason": f"No container runtime found ({what} is not on PATH). The preview cannot start anything; "
                          "nothing is ever run on the host."}
    rc, out, err = _cli([exe, "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:                       # fail closed: only a runtime whose `info` answers counts (a client-only `version` does not)
        return {"available": False, "backend": name, "executable": exe, "version": "",
                "reason": f"{name} is installed but not usable (is the daemon/VM running?): {(err or out).strip()[:300]}"}
    return {"available": True, "backend": name, "executable": exe, "version": out.strip().splitlines()[0] if out.strip() else "",
            "reason": ""}


def installed(options: Optional[PreviewOptions] = None) -> List[str]:
    """Names of runtimes whose executable is present (a stopped daemon still counts)."""
    options = options or PreviewOptions()
    return [n for n in KNOWN if find_runtime(options, n)[1]]


def image_present(exe: str, image: str) -> bool:
    rc, _, _ = _cli([exe, "image", "inspect", image])
    return rc == 0
