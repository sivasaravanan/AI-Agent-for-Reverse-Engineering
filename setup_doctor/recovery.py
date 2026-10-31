"""What to do when installing the container runtime fails (read-only probes + fixed catalog actions; nothing runs by itself).

``diagnose(env, rc, tail)`` looks at the machine (WSL 2, virtualization, a pending restart, administrator rights, an existing install) and
returns a plain-language explanation and the follow-up ``Item``s the setup dialog shows: enable WSL 2, retry as administrator (shows the
Windows permission window), start Docker if it is in fact installed, the manual download page, Podman as the free alternative.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from setup_doctor import catalog as cat
from setup_doctor.catalog import Item
from setup_doctor.checks import find_runtime_exe, known_paths, start_action
from setup_doctor.installer import EXIT_HELP

INNER_RE = re.compile(r"exit code:?\s*(\d{3,10})", re.I)
VIRT_URL = "https://support.microsoft.com/windows/enable-virtualization-on-windows-c5578302-6e43-4b4b-a449-8ced115f58e1"
WSL_URL = "https://learn.microsoft.com/windows/wsl/install"


@dataclass
class Diagnosis:
    message: str
    causes: List[str] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)


def _clean(text: str) -> str:
    return (text or "").replace("\x00", "").strip()


def inner_code(tail: List[str]) -> Optional[int]:
    """The installer's own exit code, when winget printed it ('Installer failed with exit code: 4294967291')."""
    for line in reversed(tail or []):
        m = INNER_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def probe(env) -> dict:
    """Read-only questions about the machine. Missing answers are None (unknown), never guesses."""
    out: dict = {"wsl": None, "virt": None, "reboot": None, "admin": None}
    rc, o, e = env.run(["wsl", "--status"], timeout=20)
    txt = _clean(o + e).lower()
    if rc is not None:
        out["wsl"] = rc == 0 and "not installed" not in txt and "not enabled" not in txt and "is not supported" not in txt
    rc, o, _e = env.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "$c=Get-CimInstance Win32_ComputerSystem; $p=Get-CimInstance Win32_Processor | Select -First 1; "
                         "Write-Output ($c.HypervisorPresent.ToString()+','+$p.VirtualizationFirmwareEnabled.ToString())"], timeout=25)
    if rc == 0:
        parts = _clean(o).lower().split(",")
        if len(parts) == 2:
            out["virt"] = parts[0] == "true" or parts[1] == "true"
    rc, _o, _e = env.run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"], timeout=10)
    if rc is not None:
        out["reboot"] = rc == 0
    rc, _o, _e = env.run(["net", "session"], timeout=10)
    if rc is not None:
        out["admin"] = rc == 0
    return out


def diagnose(env, rc: Optional[int], tail: Optional[List[str]] = None) -> Diagnosis:
    tail = list(tail or [])
    inner = inner_code(tail)
    lines: List[str] = []
    items: List[Item] = []
    code = inner if inner is not None else rc
    lines.append(EXIT_HELP.get(code & 0xFFFFFFFF if isinstance(code, int) and code < 0 else code, f"The installer ended with exit code {code}.")
                 if code is not None else "The installer did not finish.")
    # Is it, in fact, installed already? Then the answer is "start it", not "install it".
    backend, exe, _p = find_runtime_exe(env)
    if exe:
        act = start_action(env, backend, exe)
        lines.append(f"{backend} is already installed ({exe}). It only has to be started.")
        if act is not None:
            items.append(Item("fix:start", f"Start {backend.title()}", "missing", "Installed but not running: start it.", actions=[act]))
    else:
        facts = probe(env)
        causes: List[str] = []
        if facts["reboot"]:
            causes.append("Windows is waiting for a restart (from an earlier install or from enabling WSL). Restart Windows, then press Re-check and install again.")
            items.append(Item("fix:reboot", "Restart Windows first", "missing", "A restart is pending; the installer cannot finish before it.", url=WSL_URL))
        if facts["virt"] is False:
            causes.append("Virtualization is switched off in the BIOS / UEFI. Turn on Intel VT-x / AMD-V (SVM), start Windows again and retry. "
                          "This cannot be changed from software.")
            items.append(Item("fix:virt", "Turn on virtualization in the BIOS / UEFI", "missing",
                              "Needed by Docker Desktop and Podman (WSL 2).", url=VIRT_URL))
        if facts["wsl"] is False:
            causes.append("WSL 2 is not installed. Enable it (administrator window), restart Windows, then install Docker again.")
            items.append(Item("fix:wsl", "Enable WSL 2 (needed by Docker Desktop)", "missing",
                              "Opens the Windows permission window; a restart follows.", actions=[cat.wsl_install_action()], url=WSL_URL))
        if facts["admin"] is False:
            causes.append("This app is not running as administrator; the installer's own permission window may have been hidden. "
                          "The administrator retry below shows it explicitly.")
        if not causes and (facts["wsl"], facts["virt"]) == (None, None):
            causes.append("The checks of WSL / virtualization could not be run here; use the administrator retry or the manual download below.")
        lines += causes
        items.append(Item("fix:docker-admin", "Retry installing Docker Desktop as administrator", "missing",
                          "Runs the installer silently through the Windows permission window (accept it).",
                          actions=[cat.docker_admin_action()], url=cat.DOWNLOAD_URLS["docker"]))
        items.append(Item("fix:manual", "Install Docker Desktop yourself", "missing",
                          "Download the installer from docker.com, run it, then press Re-check.", url=cat.DOWNLOAD_URLS["docker"]))
        lines.append("Podman (free, listed above as the alternative) needs WSL 2 as well.")
    return Diagnosis("\n".join(lines), lines, items)
