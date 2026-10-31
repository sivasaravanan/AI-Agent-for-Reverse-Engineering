"""Find the hurdles typical of institutional machines BEFORE they cause a confusing failure, and say what to do about each."""
from __future__ import annotations

import os
import shutil
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from prerequisites import network
from prerequisites.env import Env


@dataclass
class Hurdle:
    id: str
    severity: str        # blocker | warning | info
    title: str
    detail: str
    fix: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class Diagnosis:
    system: Dict[str, Any] = field(default_factory=dict)
    hurdles: List[Hurdle] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"system": self.system, "hurdles": [h.to_dict() for h in self.hurdles]}


def _ps(env: Env, expr: str) -> str:
    rc, out, _e = env.quick(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", expr], 25)
    return out.strip() if rc == 0 else ""


def proxy_in_use(cfg) -> str:
    if cfg.proxy:
        return "configured in settings"
    proxies = urllib.request.getproxies()
    return ", ".join(sorted(proxies)) + " (environment/system)" if proxies else ""


def diagnose(env: Env, cfg, *, connectivity: Optional[List[Dict[str, str]]] = None, statuses: Optional[list] = None) -> Diagnosis:
    d = Diagnosis()
    s = d.system
    s.update({"platform": env.platform, "arch": env.arch, "administrator": env.is_admin, "standalone_exe": env.frozen,
              "winget": bool(env.winget), "proxy": bool(proxy_in_use(cfg)), "offline_folder": cfg.offline_dir or "",
              "mirror": bool(cfg.mirror_base_url), "mode": cfg.mode})
    try:
        s["free_gb"] = round(shutil.disk_usage(str(env.root if env.root.exists() else env.root.parent)).free / 1e9, 1)
    except OSError:
        s["free_gb"] = None
    H = d.hurdles
    if not cfg.allow_downloads:
        H.append(Hurdle("downloads-disabled", "blocker", "Downloads are disabled by institutional policy",
                        "The policy forbids downloading installers from inside this app.",
                        "Place approved installers (and a SHA256SUMS file) in the offline folder, or hand the IT request text to your IT desk."))
    if not cfg.allow_installs:
        H.append(Hurdle("installs-disabled", "blocker", "Installing software from this app is disabled by policy", "",
                        "Use the IT request text; the app still works for static analysis."))
    if cfg.offline_dir and not os.path.isdir(cfg.offline_dir):
        H.append(Hurdle("offline-missing", "warning", "The offline folder does not exist", cfg.offline_dir, "Fix the path in the Prerequisites settings."))
    if s.get("free_gb") is not None and s["free_gb"] < 10:
        H.append(Hurdle("low-disk", "warning", f"Only {s['free_gb']} GB free", "Container engines and images need roughly 10 GB.", "Free disk space or choose another data folder (AISEA_HOME)."))
    if env.platform == "windows":
        if 0 < env.windows_build < 19041:
            H.append(Hurdle("old-windows", "blocker", f"Windows build {env.windows_build} is too old for WSL 2",
                            "Podman and Docker Desktop need Windows 10 2004 (build 19041) or newer.", "Update Windows, or use a different machine."))
        if not env.is_admin:
            H.append(Hurdle("not-admin", "info", "You are not an administrator",
                            "Per-user installs (winget --scope user, Docker --user, private tools) work without it; machine-wide installs and enabling WSL need it.",
                            "Keep 'prefer per-user installs' on, or ask IT (IT request button)."))
        if not env.winget:
            H.append(Hurdle("no-winget", "info", "winget is not available", "Many institutions remove or block it.", "The app falls back to verified vendor downloads or the offline folder."))
        virt = _ps(env, "(Get-CimInstance Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled")
        s["virtualization_enabled"] = virt
        if virt.lower() == "false":
            H.append(Hurdle("virtualization-off", "blocker", "Hardware virtualization is disabled in the BIOS/UEFI",
                            "Linux containers cannot run without it.", "Enable Intel VT-x / AMD-V in the firmware setup (IT may need to do this)."))
        caption = _ps(env, "(Get-CimInstance Win32_OperatingSystem).Caption")
        s["windows"] = caption
        if "home" in caption.lower():
            H.append(Hurdle("windows-home", "info", "Windows Home edition", "Hyper-V is not available; the WSL 2 back end still works.", "Use the WSL 2 back end (the default here)."))
        pending = _ps(env, "Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending'")
        if pending.lower() == "true":
            H.append(Hurdle("pending-reboot", "warning", "Windows has a restart pending", "Installers can fail until you restart.", "Restart Windows, then retry."))
    ok_hosts = {r["host"]: r for r in (connectivity or [])}
    for r in ok_hosts.values():
        if r["ok"] != "yes":
            H.append(Hurdle(f"net-{r['kind'] or 'unreachable'}-{r['host']}", "blocker" if cfg.allow_downloads else "info",
                            f"Cannot reach {r['host']}", r.get("detail", ""), r.get("advice", "") or "Check proxy/firewall settings."))
    if statuses is not None:
        from prerequisites.detect import container_ready
        ok, _b = container_ready(statuses)
        s["container_runtime_ready"] = ok
    return d
