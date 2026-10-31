"""Read-only prerequisite checks. Every system access goes through ``Env`` so the logic is testable without Docker."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from setup_doctor import catalog as cat
from setup_doctor.catalog import Action, Item

CREATE_NO_WINDOW = 0x08000000


class SystemEnv:
    """Real machine access. Tests replace this with a fake."""
    platform = sys.platform
    frozen = bool(getattr(sys, "frozen", False))
    python = sys.executable
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    def glob(self, pattern: str) -> List[str]:
        import glob as _g
        return sorted(_g.glob(pattern))

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def getenv(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    def add_to_path(self, folder: str) -> None:
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")

    def run(self, argv: List[str], timeout: float = 20) -> Tuple[Optional[int], str, str]:
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            p = subprocess.run([str(a) for a in argv], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
            return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "", str(exc)


def _is_win(env) -> bool:
    return env.platform.startswith("win")


def known_paths(env) -> Dict[str, List[str]]:
    """Usual install locations (a program installed in this session is not on the running app's PATH yet)."""
    if _is_win(env):
        pf = env.getenv("ProgramFiles", r"C:\Program Files")
        out = {"docker": [str(Path(pf) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe")],
               "podman": [str(Path(pf) / "RedHat" / "Podman" / "podman.exe")],
               "docker_desktop": [str(Path(pf) / "Docker" / "Docker" / "Docker Desktop.exe")]}
        local = env.getenv("LOCALAPPDATA", "")
        if local:                                                  # a per-user install of Docker Desktop
            base = Path(local) / "Programs" / "Docker" / "Docker"
            out["docker"].append(str(base / "resources" / "bin" / "docker.exe"))
            out["docker_desktop"].append(str(base / "Docker Desktop.exe"))
        return out
    if env.platform == "darwin":
        return {"docker": ["/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                           "/Applications/Docker.app/Contents/Resources/bin/docker"],
                "podman": ["/usr/local/bin/podman", "/opt/homebrew/bin/podman"], "docker_desktop": []}
    return {"docker": ["/usr/bin/docker", "/usr/local/bin/docker"], "podman": ["/usr/bin/podman", "/usr/local/bin/podman"],
            "docker_desktop": []}


def tool_paths(env) -> Dict[str, List[str]]:
    """Usual locations of optional tools (patterns allowed). A tool installed in this session is not on PATH yet."""
    out: Dict[str, List[str]] = {}
    py_dir = str(Path(env.python).parent)
    for pkg in ("ruff", "pyright"):                       # pip installs here (a venv's Scripts folder is often not on PATH)
        out[pkg] = [str(Path(py_dir) / f"{pkg}.exe"), str(Path(py_dir) / pkg)]
    if _is_win(env):
        pf = env.getenv("ProgramFiles", r"C:\Program Files")
        out["node"] = [str(Path(pf) / "nodejs" / "node.exe")]
        out["dotnet"] = [str(Path(pf) / "dotnet" / "dotnet.exe")]
        out["javac"] = [str(Path(pf) / "Eclipse Adoptium" / "jdk-*" / "bin" / "javac.exe"),
                        str(Path(pf) / "Java" / "*" / "bin" / "javac.exe")]
    return out


def _expand(env, patterns: List[str]) -> List[str]:
    out: List[str] = []
    for pat in patterns:
        if "*" in pat:
            g = getattr(env, "glob", None)
            out.extend(g(pat) if g else [])
        else:
            out.append(pat)
    return out


def find_tool(env, name: str) -> Optional[str]:
    """On PATH, else in a usual install location."""
    exe = env.which(name)
    if exe:
        return exe
    for p in _expand(env, tool_paths(env).get(name, [])):
        if env.exists(p):
            return p
    return None


def find_runtime_exe(env) -> Tuple[Optional[str], Optional[str], bool]:
    """(backend, executable, on_path). Docker is preferred, like the execution plugin."""
    for name in ("docker", "podman"):
        exe = env.which(name)
        if exe:
            return name, exe, True
    kp = known_paths(env)
    for name in ("docker", "podman"):
        for p in kp[name]:
            if env.exists(p):
                return name, p, False
    return None, None, False


def apply_known_paths(env=None) -> List[str]:
    """Make tools that are installed but not on PATH visible to this process. Returns the folders added."""
    env = env or SystemEnv()
    added: List[str] = []
    candidates = {n: list(v) for n, v in known_paths(env).items() if n in ("docker", "podman")}
    candidates.update(tool_paths(env))
    for name, pats in candidates.items():
        if env.which(name):
            continue
        for p in _expand(env, pats):
            if env.exists(p):
                folder = str(Path(p).parent)
                if folder not in added:
                    env.add_to_path(folder)
                    added.append(folder)
                break
    return added


def run_checks(env=None, include_optional: bool = True) -> List[Item]:
    env = env or SystemEnv()
    items: List[Item] = []
    win, mac = _is_win(env), env.platform == "darwin"
    winget = env.which("winget") if win else None
    brew = env.which("brew") if mac else None

    def install(key: str, label: str, mb: int, note: str = "", admin: bool = True) -> List[Action]:
        if win and winget:
            return [cat.winget_action(key, label, mb, note, admin)]
        if mac and brew:
            a = cat.macos_brew_action(key)
            return [a] if a else []
        return []

    # --- container runtime ------------------------------------------------------------------
    backend, exe, on_path = find_runtime_exe(env)
    if exe:
        detail = f"{backend} found at {exe}" + ("" if on_path else " (not on PATH; this app will use it anyway)")
        items.append(Item("runtime", "Container runtime (Docker or Podman)", "ok", detail, required=True))
    else:
        manual = "" if install("docker", "", 0) else cat.DOWNLOAD_URLS["docker"]
        why = "" if (win and winget) or (mac and brew) else (
            " Automatic install needs winget (Windows 10/11 'App Installer')" if win else
            " Install it from the vendor page or with your package manager.")
        items.append(Item("runtime", "Container runtime (Docker or Podman)", "missing",
                          "Not installed. Needed only to run builds/tests; analysis works without it." + why, required=True,
                          actions=install("docker", "Install Docker Desktop", 600,
                                          "Big download. Needs WSL 2/virtualization; a restart may be required. Free for "
                                          "personal, education and small business use; larger organisations need a paid licence."),
                          url=manual))
        alt = install("podman", "Install Podman (alternative to Docker)", 100, "Free and open source. Needs a Podman machine.")
        if alt:
            items.append(Item("runtime-alt", "Alternative: Podman", "optional",
                              "Choose this instead of Docker if licensing is a concern.", actions=alt,
                              url=cat.DOWNLOAD_URLS["podman"]))

    # --- daemon ------------------------------------------------------------------------------
    daemon_ok = False
    if exe:
        rc, out, err = env.run([exe, "info", "--format", "{{.ServerVersion}}"])
        daemon_ok = rc == 0
        if daemon_ok:
            items.append(Item("daemon", f"{backend} is running", "ok", f"version {out.strip()}", required=True))
        else:
            start: List[Action] = []
            if backend == "docker":
                for p in known_paths(env)["docker_desktop"]:
                    if env.exists(p):
                        start = [cat.start_docker_desktop(p)]
                        break
            elif backend == "podman":
                start = [cat.podman_start_action(exe)]
            items.append(Item("daemon", f"{backend} is installed but not running", "missing",
                              (err or out).strip()[:200] or "No answer from the runtime.", required=True, actions=start))
            if backend == "podman":
                items[-1].actions = [cat.podman_start_action(exe)]
                items.append(Item("podman-machine", "Podman machine (first-time setup)", "optional",
                                  "Run this once if 'podman machine start' says no machine exists.",
                                  actions=cat.podman_machine_actions(exe)))

    # --- images ------------------------------------------------------------------------------
    if exe and daemon_ok:
        for eco, image in cat.default_images().items():
            rc, _o, _e = env.run([exe, "image", "inspect", cat.qualify_image(backend, image)
                                  if backend == "podman" else image])
            if rc == 0:
                items.append(Item(f"image:{eco}", f"Image for {eco} projects", "ok", image))
            else:
                req = eco in cat.REQUIRED_IMAGES
                items.append(Item(f"image:{eco}", f"Image for {eco} projects", "missing" if req else "optional",
                                  f"{image} is not downloaded (about {cat.IMAGE_APPROX_MB.get(eco, 500)} MB)"
                                  + ("" if req else "; only needed if you run builds for this technology."),
                                  required=req, actions=[cat.pull_action(exe, backend, eco, image)]))

    # --- optional analysis tools -------------------------------------------------------------
    if include_optional:
        for pkg in cat.PIP_PACKAGES:
            found = find_tool(env, pkg)
            if found:
                items.append(Item(f"tool:{pkg}", pkg, "ok", f"installed ({found})" if not env.which(pkg) else "installed"))
            elif env.frozen:
                items.append(Item(f"tool:{pkg}", pkg, "info", "Optional. Not installed; install it separately for extra checks."))
            else:
                items.append(Item(f"tool:{pkg}", pkg, "optional", "Optional extra static checks for Python code.",
                                  actions=[cat.pip_action(env.python, pkg, user=not getattr(env, "in_venv", False))]))
        for key, tool, label, mb in (("node", "node", "Node.js (ESLint/TypeScript checks)", 80),
                                     ("dotnet", "dotnet", ".NET SDK (C# analysis)", 250),
                                     ("jdk", "javac", "Java JDK (Java analysis)", 200)):
            if find_tool(env, tool):
                items.append(Item(f"tool:{key}", label, "ok", "installed"))
            else:
                items.append(Item(f"tool:{key}", label, "optional", "Optional; only for analysing that language.",
                                  actions=install(key, "Install " + label, mb)))
    return items


def summarize(items: List[Item]) -> Dict[str, object]:
    missing_required = [i for i in items if i.required and i.status != "ok"]
    return {"ok": not missing_required, "missing_required": [i.id for i in missing_required],
            "optional_missing": [i.id for i in items if i.status == "optional"]}


def start_action(env, backend: Optional[str] = None, exe: Optional[str] = None) -> Optional[Action]:
    """The action that starts the installed container runtime (Docker Desktop / Podman machine), or None when there is nothing to start."""
    if backend is None or exe is None:
        backend, exe, _p = find_runtime_exe(env)
    if not exe:
        return None
    if backend == "podman":
        return cat.podman_start_action(exe)
    if _is_win(env) or env.platform == "darwin":
        for p in known_paths(env).get("docker_desktop", []):
            if env.exists(p):
                return cat.start_docker_desktop(p)
        return cat.docker_cli_start_action(exe)                  # Docker Desktop 4.37+: `docker desktop start`
    return None
