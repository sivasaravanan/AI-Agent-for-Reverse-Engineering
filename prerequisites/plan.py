"""Turn "install X" into an exact, reviewable plan (commands, URLs, admin need, licence, hosts). Building a plan never touches the network
and never changes the machine; ``prerequisites.install.run_plan`` executes it only with an approval bound to the plan's hash."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from prerequisites import tools_env
from prerequisites.catalog import BY_ID, DIRECT, Direct, Method, Prereq, get
from prerequisites.detect import READY, detect
from prerequisites.env import Env

OFFLINE_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "podman": ("podman-installer-windows-*.exe",), "docker": ("Docker Desktop Installer.exe", "Docker*Installer*.exe"),
    "ollama": ("OllamaSetup.exe",), "node": ("node-v*-x64.msi",), "jdk": ("*jdk*.msi", "OpenJDK*.msi"),
    "dotnet": ("dotnet-sdk-*-win-x64.exe",), "go": ("go*.windows-amd64.msi",), "python": ("python-3.*-amd64.exe",),
}
CHECKSUM_FILES = ("SHA256SUMS", "sha256sums.txt", "SHA256SUMS.txt", "shasums")
PM_COMMANDS = {"apt": ["apt-get", "install", "-y"], "dnf": ["dnf", "install", "-y"], "pacman": ["pacman", "-S", "--noconfirm"],
               "zypper": ["zypper", "--non-interactive", "install"]}
WINGET_OK = (0, 0x8A15002B, -1978335189)


@dataclass
class Step:
    kind: str                      # download | verify | run | note
    title: str
    command: List[str] = field(default_factory=list)
    elevated: bool = False         # run through UAC (Windows) / pkexec-sudo (Linux)
    url: str = ""
    dest: str = ""
    timeout: int = 1800
    optional: bool = False
    ok_codes: Tuple[int, ...] = (0,)
    reboot_codes: Tuple[int, ...] = (3010, 1641)


@dataclass
class InstallPlan:
    prereq_id: str
    name: str
    method: str
    scope: str                     # user | machine | n/a
    steps: List[Step]
    needs_admin: bool = False
    needs_network: bool = True
    hosts: List[str] = field(default_factory=list)
    license_ack_required: bool = False
    license_note: str = ""
    reboot_possible: bool = False
    notes: List[str] = field(default_factory=list)
    checksum_source: str = ""
    publishers: Tuple[str, ...] = ()
    offline_file: str = ""

    @property
    def plan_hash(self) -> str:
        payload = {"id": self.prereq_id, "method": self.method, "scope": self.scope, "lic": self.license_ack_required,
                   "steps": [[s.kind, s.command, s.elevated, s.url, s.dest] for s in self.steps], "offline": self.offline_file}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def describe(self) -> str:
        out = [f"Install {self.name} via {self.method}" + (f" ({self.scope} scope)" if self.scope not in ("", "n/a") else "")]
        out.append("Administrator rights: " + ("YES - Windows will ask you to confirm (UAC) / your password" if self.needs_admin else "not needed"))
        out.append("Network: " + (("needs access to " + ", ".join(self.hosts)) if self.needs_network and self.hosts else
                                  ("needed" if self.needs_network else "not needed")))
        if self.reboot_possible:
            out.append("A restart may be required afterwards.")
        for n in self.notes:
            out.append("Note: " + n)
        out.append("Steps:")
        for i, s in enumerate(self.steps, 1):
            line = f"  {i}. {s.title}"
            if s.url:
                line += f"\n     {s.url}"
            if s.command:
                line += "\n     $ " + " ".join(s.command)
            out.append(line + ("  (optional)" if s.optional else ""))
        if self.license_ack_required:
            out.append("Licence: " + self.license_note)
        return "\n".join(out)


@dataclass
class PlanRefusal:
    prereq_id: str
    reason: str
    advice: str = ""
    alternatives: List[str] = field(default_factory=list)
    manual_url: str = ""


Plan = Union[InstallPlan, PlanRefusal]


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def find_offline(pid: str, offline_dir: str) -> Optional[Path]:
    if not offline_dir:
        return None
    root = Path(offline_dir)
    if not root.is_dir():
        return None
    for pat in OFFLINE_PATTERNS.get(pid, ()):
        for f in sorted(root.glob("*")):
            if f.is_file() and fnmatch.fnmatch(f.name.lower(), pat.lower()):
                return f
    return None


def offline_checksum(file: Path) -> str:
    """Expected SHA-256 for an offline installer from SHA256SUMS-style files or '<file>.sha256' next to it ('' if none)."""
    from prerequisites.verify import parse_shasums
    side = file.with_name(file.name + ".sha256")
    candidates = [side] + [file.parent / n for n in CHECKSUM_FILES]
    for c in candidates:
        try:
            if c.is_file():
                text = c.read_text(encoding="utf-8", errors="replace")
                table = parse_shasums(text)
                if file.name in table:
                    return table[file.name]
                bare = text.strip().split()
                if c is side and bare and len(bare[0]) == 64:
                    return bare[0].lower()
        except OSError:
            continue
    return ""


def _elevate_needed(env: Env, admin_required: bool) -> bool:
    return admin_required and not env.is_admin


def _installer_run_step(path_or_name: str, spec_args: Sequence[str], env: Env, scope: str, title: str) -> Step:
    low = path_or_name.lower()
    if low.endswith(".msi"):
        cmd = ["msiexec.exe", "/i", path_or_name, "/quiet", "/norestart"]
    else:
        cmd = [path_or_name, *spec_args]
    return Step("run", title, cmd, elevated=_elevate_needed(env, scope == "machine"), timeout=3600)


def _direct_plan(pid: str, prereq: Prereq, env: Env, cfg, scope_pref: Optional[str]) -> Plan:
    spec: Direct = DIRECT[pid]
    url = spec.urls.get(env.arch) or spec.urls.get("amd64", "")
    fname = spec.filename.format(arch=env.arch)
    if cfg.mirror_base_url:
        url = cfg.mirror_base_url.rstrip("/") + "/" + urllib.parse.quote(fname)
    scope = scope_pref or ("user" if (cfg.prefer_user_scope and spec.user_scope) else "machine")
    if scope == "user" and not spec.user_scope:
        scope = "machine"
    dest = str(env.downloads_dir / fname)
    args = list(spec.user_args if scope == "user" else spec.machine_args)
    if prereq.license_ack:
        args += list(spec.license_args)
    steps = [Step("download", f"Download {fname}", url=url, dest=dest),
             Step("verify", "Verify checksum / publisher signature", dest=dest),
             _installer_run_step(dest, args, env, scope, f"Run the {prereq.name} installer silently")]
    hosts = [_host(url)] + (["api.github.com", "objects.githubusercontent.com"] if spec.checksum.startswith("github") else [])
    return InstallPlan(pid, prereq.name, "direct download", scope, steps, scope == "machine", True, hosts,
                       prereq.license_ack, prereq.license_note, True, checksum_source=spec.checksum or "Authenticode signature",
                       publishers=spec.publishers)


def _offline_plan(pid: str, prereq: Prereq, env: Env, cfg, f: Path) -> Plan:
    spec = DIRECT.get(pid)
    if f.suffix.lower() != ".msi" and spec is None:
        return PlanRefusal(pid, f"The offline file {f.name} has no known silent-install switches.",
                           "Run it manually, or use winget/package manager.", manual_url=prereq.manual_url)
    scope = "user" if (spec and cfg.prefer_user_scope and spec.user_scope) else "machine"
    args = list((spec.user_args if scope == "user" else spec.machine_args) if spec else ())
    if prereq.license_ack and spec:
        args += list(spec.license_args)
    steps = [Step("verify", f"Verify {f.name} from the offline folder", dest=str(f)),
             _installer_run_step(str(f), args, env, scope, f"Run {f.name} silently")]
    return InstallPlan(pid, prereq.name, "offline folder", scope, steps, scope == "machine", False, [], prereq.license_ack,
                       prereq.license_note, True, offline_file=str(f), publishers=spec.publishers if spec else (),
                       checksum_source=offline_checksum(f) and "SHA256SUMS" or "Authenticode signature")


def _winget_plan(pid: str, prereq: Prereq, m: Method, env: Env, cfg, scope_pref: Optional[str]) -> Plan:
    scope = scope_pref or ("user" if (cfg.prefer_user_scope and m.user_scope) else "machine")
    cmd = [env.winget or "winget", "install", "-e", "--id", m.ref, "--source", "winget", "--silent", "--disable-interactivity",
           "--accept-source-agreements", "--accept-package-agreements", "--scope", scope]
    notes = ["winget downloads from the vendor and verifies the package hash itself; Windows asks for confirmation if administrator rights are needed."]
    return InstallPlan(pid, prereq.name, "winget", scope, [Step("run", f"winget install {m.ref}", cmd, timeout=3600)],
                       scope == "machine", True, ["cdn.winget.microsoft.com", "the vendor's download host"], prereq.license_ack,
                       prereq.license_note, True, notes)


def _pm_plan(pid: str, prereq: Prereq, m: Method, env: Env) -> Plan:
    if m.kind == "brew":
        cask = m.ref.startswith("cask:")
        cmd = [env.which("brew") or "brew", "install"] + (["--cask", m.ref[5:]] if cask else [m.ref])
        return InstallPlan(pid, prereq.name, "Homebrew", "user", [Step("run", f"brew install {m.ref}", cmd, timeout=3600)], False, True,
                           ["formulae.brew.sh", "ghcr.io"], prereq.license_ack, prereq.license_note)
    base = PM_COMMANDS[m.kind] + m.ref.split()
    elevator = None
    if env.is_admin:
        elevator = []
    elif env.interactive and env.which("sudo"):
        elevator = [env.which("sudo")]
    elif env.which("pkexec"):
        elevator = [env.which("pkexec")]
    if elevator is None:
        return PlanRefusal(pid, "Administrator rights are required and neither a terminal (sudo) nor pkexec is available.",
                           f"Run in a terminal:  sudo {' '.join(base)}", manual_url=prereq.manual_url)
    cmd = [*elevator, env.which(base[0]) or base[0], *base[1:]]
    return InstallPlan(pid, prereq.name, m.kind, "machine", [Step("run", f"{m.kind} install {m.ref}", cmd, timeout=3600)],
                       not env.is_admin, True, ["your distribution's package mirrors"], prereq.license_ack, prereq.license_note)


def _venv_plan(pid: str, prereq: Prereq, m: Method, env: Env, cfg) -> Plan:
    if not env.python:
        return PlanRefusal(pid, "No Python interpreter was found to create the private tool environment.",
                           "Install Python 3.11+ first (the 'python' item), then retry.", manual_url="https://www.python.org/downloads/")
    venv, vpy = tools_env.venv_dir(env.root), tools_env.venv_python(env.root)
    steps: List[Step] = []
    if not vpy.exists():
        steps.append(Step("run", "Create the private tool environment", [env.python, "-m", "venv", str(venv)], timeout=300))
    pip = [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--only-binary=:all:", "--upgrade", m.ref]
    hosts = ["pypi.org", "files.pythonhosted.org"]
    if cfg.pip_index_url:
        pip += ["--index-url", cfg.pip_index_url]
        hosts = [_host(cfg.pip_index_url)]
    if cfg.proxy:
        pip += ["--proxy", cfg.proxy]
    if cfg.ca_bundle:
        pip += ["--cert", cfg.ca_bundle]
    steps.append(Step("run", f"pip install {m.ref} (binary packages only) into the private environment", pip, timeout=900))
    return InstallPlan(pid, prereq.name, "private Python environment", "user", steps, False, True, hosts,
                       notes=[f"Installed under {venv}; no administrator rights and no global changes."])


def _npm_plan(pid: str, prereq: Prereq, m: Method, env: Env, cfg) -> Plan:
    npm = env.which("npm")
    if not npm:
        return PlanRefusal(pid, "npm was not found (Node.js is not installed).", "Install Node.js first (the 'node' item).",
                           alternatives=["node"], manual_url="https://nodejs.org/en/download")
    prefix = tools_env.npm_prefix(env.root)
    cmd = [npm, "install", "--prefix", str(prefix), "--ignore-scripts", "--no-audit", "--no-fund", m.ref]
    hosts = ["registry.npmjs.org"]
    if cfg.npm_registry:
        cmd += ["--registry", cfg.npm_registry]
        hosts = [_host(cfg.npm_registry)]
    if cfg.proxy:
        cmd += ["--proxy", cfg.proxy, "--https-proxy", cfg.proxy]
    if cfg.ca_bundle:
        cmd += ["--cafile", cfg.ca_bundle]
    return InstallPlan(pid, prereq.name, "private npm prefix", "user", [Step("run", f"npm install {m.ref} (install scripts disabled)", cmd, timeout=900)],
                       False, True, hosts, notes=[f"Installed under {prefix}; no administrator rights and no global changes."])


def _post_steps(pid: str, plan: InstallPlan, env: Env) -> None:
    if pid == "podman" and env.platform in ("windows", "darwin"):
        exe = "podman"
        plan.steps += [Step("run", "Create the Podman machine (downloads a Linux VM image)", [exe, "machine", "init"], timeout=3600, optional=True),
                       Step("run", "Start the Podman machine", [exe, "machine", "start"], timeout=600, optional=True)]
        plan.hosts += ["quay.io", "(VM image registry)"]
        plan.notes.append("The Podman machine image is a large download; allow quay.io/ghcr.io or skip these optional steps and use your mirror.")
    if pid == "docker":
        plan.notes.append("After installing, start Docker Desktop once and accept its terms; this app detects it automatically.")


def build_plan(pid: str, env: Env, cfg, *, scope: Optional[str] = None, method: Optional[str] = None) -> Plan:
    prereq = get(pid)
    if pid in cfg.blocked:
        return PlanRefusal(pid, "This item is blocked by your institution's policy.", "Contact your IT department.")
    if not cfg.allow_installs:
        return PlanRefusal(pid, "Installing software from this app is disabled by policy.", "Use the IT request text, or install manually.",
                           manual_url=prereq.manual_url)
    methods = prereq.methods.get(env.platform, ())
    if not methods:
        return PlanRefusal(pid, f"No automated install method for {env.platform}.", "Install it manually.", manual_url=prereq.manual_url)
    result: Optional[Plan] = None
    off = find_offline(pid, cfg.offline_dir) if env.platform == "windows" else None
    if off and method in (None, "offline"):
        result = _offline_plan(pid, prereq, env, cfg, off)
    reasons: List[str] = []
    if result is None or isinstance(result, PlanRefusal):
        for m in methods:
            if method and method not in (m.kind, "direct download" if m.kind == "direct" else m.kind):
                continue
            if m.kind == "winget":
                if not (cfg.allow_winget and env.winget):
                    reasons.append("winget is not available or disabled")
                    continue
                result = _winget_plan(pid, prereq, m, env, cfg, scope)
            elif m.kind == "direct":
                if not cfg.allow_downloads:
                    reasons.append("downloads are disabled by policy")
                    continue
                result = _direct_plan(pid, prereq, env, cfg, scope)
            elif m.kind == "feature":
                st = detect(prereq, env)
                steps = [Step("run", "Enable WSL (no Linux distribution)", [env.which("wsl") or "wsl.exe", "--install", "--no-distribution"],
                              elevated=not env.is_admin, timeout=1800, ok_codes=(0,), reboot_codes=(3010, 1641))]
                result = InstallPlan(pid, prereq.name, "Windows feature", "machine", steps, True, True, ["Windows Update"], reboot_possible=True,
                                     notes=["Needs administrator rights and normally a restart. Virtualization must be enabled in the BIOS/UEFI."])
            elif m.kind in ("brew", "apt", "dnf", "pacman", "zypper"):
                tool = {"brew": "brew"}.get(m.kind, PM_COMMANDS.get(m.kind, [m.kind])[0])
                if not env.which(tool):
                    reasons.append(f"{tool} not found")
                    continue
                result = _pm_plan(pid, prereq, m, env)
            elif m.kind == "venv-pip":
                result = _venv_plan(pid, prereq, m, env, cfg)
            elif m.kind == "npm-local":
                result = _npm_plan(pid, prereq, m, env, cfg)
            if isinstance(result, InstallPlan):
                break
    if result is None:
        return PlanRefusal(pid, "No install method is usable here: " + ("; ".join(dict.fromkeys(reasons)) or "none available") + ".",
                           "Use the offline folder, the IT request text, or install it manually.", manual_url=prereq.manual_url)
    if isinstance(result, InstallPlan):
        _post_steps(pid, result, env)
        if result.needs_network and not cfg.allow_downloads and result.method not in ("winget", "Homebrew", "apt", "dnf", "pacman", "zypper"):
            return PlanRefusal(pid, "Downloads are disabled by policy.", "Use the offline folder.", manual_url=prereq.manual_url)
    return result


def expand_dependencies(ids: Sequence[str], env: Env) -> List[str]:
    """Add missing dependencies (e.g. WSL2 before Podman/Docker, Node before ESLint) in install order."""
    order: List[str] = []

    def visit(i: str) -> None:
        if i in order:
            return
        p = get(i)
        for n in p.needs:
            if n == "wsl2" and env.platform != "windows":
                continue
            if detect(get(n), env).state != READY:
                visit(n)
        order.append(i)
    for i in ids:
        visit(i)
    return order
