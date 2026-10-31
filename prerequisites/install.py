"""Execute an approved InstallPlan: download (proxy/CA aware) -> verify -> silent install -> re-detect. Every action is audited.
Nothing runs without an Approval for the exact plan hash; licence-gated products also need the user's explicit licence acceptance."""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from prerequisites import network, tools_env
from prerequisites.audit import AuditLog, mask
from prerequisites.catalog import BY_ID, DIRECT, SANDBOX_IMAGES, get
from prerequisites.detect import INSTALLED, READY, Status, detect
from prerequisites.env import Env, RunResult
from prerequisites.plan import InstallPlan, Step, WINGET_OK, offline_checksum
from prerequisites.verify import parse_shasums, verify_installer

Progress = Callable[[str], None]


@dataclass
class Approval:
    plan_hash: str
    license_accepted: bool = False
    approved_by: str = "user"

    @classmethod
    def for_plan(cls, plan: InstallPlan, license_accepted: bool = False) -> "Approval":
        return cls(plan.plan_hash, license_accepted)


@dataclass
class InstallResult:
    prereq_id: str
    status: str                 # installed | already-present | needs-start | needs-reboot | failed | cancelled | refused | uac-declined
    detail: str = ""
    advice: str = ""
    log: List[str] = field(default_factory=list)
    state_after: str = ""
    retry_machine_scope: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("installed", "already-present", "needs-start", "needs-reboot")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def elevated_windows_command(cmd: Sequence[str]) -> List[str]:
    """Wrap ``cmd`` so Windows shows the UAC prompt; exit code of the elevated process is returned (1223 = user declined)."""
    exe, *args = [str(c) for c in cmd]
    if any(ch in a for a in args for ch in ' \t"\'`$;&|'):
        raise ValueError("Refusing to elevate a command whose arguments contain spaces or shell metacharacters.")
    arglist = "@(" + ",".join(_ps_quote(a) for a in args) + ")" if args else ""
    script = (f"try {{ $p = Start-Process -FilePath {_ps_quote(exe)} " + (f"-ArgumentList {arglist} " if arglist else "") +
              "-Verb RunAs -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } catch { Write-Output $_.Exception.Message; exit 1223 }")
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


def _resolve_github_checksum(spec_checksum: str, filename: str, cfg, opener, log: Progress) -> str:
    """SHA-256 for the latest GitHub release asset (asset digest from the API, then a 'shasums' asset). '' when unavailable."""
    repo = spec_checksum.split(":", 1)[1]
    try:
        data = json.loads(network.fetch_text(f"https://api.github.com/repos/{repo}/releases/latest", cfg, opener=opener))
        for a in data.get("assets", []):
            if a.get("name") == filename and str(a.get("digest", "")).startswith("sha256:"):
                return a["digest"].split(":", 1)[1].lower()
        for a in data.get("assets", []):
            if a.get("name", "").lower() in ("shasums", "sha256sums", "sha256sums.txt"):
                table = parse_shasums(network.fetch_text(a["browser_download_url"], cfg, opener=opener))
                if filename in table:
                    return table[filename]
    except network.NetworkError as exc:
        log(f"Could not read the release checksum ({exc.kind}); will rely on the publisher signature.")
    except (ValueError, KeyError, TypeError):
        log("Release metadata had an unexpected format; will rely on the publisher signature.")
    return ""


def run_plan(plan: InstallPlan, approval: Optional[Approval], env: Env, cfg, *, progress: Optional[Progress] = None,
             cancel: Optional[threading.Event] = None, audit: Optional[AuditLog] = None, opener=None) -> InstallResult:
    audit = audit or AuditLog()
    lines: List[str] = []

    def log(msg: str) -> None:
        msg = mask(msg)
        lines.append(msg)
        if progress:
            progress(msg)

    pid = plan.prereq_id

    def done(status: str, detail: str = "", advice: str = "", **kw) -> InstallResult:
        audit.log("install-result", prereq=pid, method=plan.method, status=status, detail=detail)
        return InstallResult(pid, status, detail, advice, lines[-200:], **kw)

    if approval is None or approval.plan_hash != plan.plan_hash:
        return done("refused", "No approval for this exact plan.")
    if plan.license_ack_required and not approval.license_accepted:
        return done("refused", f"{plan.name} needs you to accept its licence terms first. {plan.license_note}")
    if not cfg.allow_installs or pid in cfg.blocked:
        return done("refused", "Blocked by policy.")
    if plan.needs_network and plan.method == "direct download" and not cfg.allow_downloads:
        return done("refused", "Downloads are disabled by policy.")
    pre = detect(get(pid), env)
    if pre.state == READY and pid not in ("wsl2",):
        log(f"{plan.name} is already installed ({pre.version or pre.path}).")
        return done("already-present", pre.version or pre.path, state_after=READY)
    audit.log("install-start", prereq=pid, method=plan.method, scope=plan.scope, plan_hash=plan.plan_hash,
              steps=[[s.kind, s.command, s.url] for s in plan.steps], admin=plan.needs_admin)
    reboot = False
    last_sha = ""
    for i, step in enumerate(plan.steps, 1):
        if cancel and cancel.is_set():
            return done("cancelled", "Cancelled before step %d." % i)
        log(f"[{i}/{len(plan.steps)}] {step.title}")
        if step.kind == "download":
            spec = DIRECT.get(pid)
            sha = ""
            try:
                if spec and spec.checksum.startswith("github-latest:"):
                    sha = _resolve_github_checksum(spec.checksum, Path(step.dest).name, cfg, opener, log)
                last = [0.0]

                def prog(done_b: int, total: Optional[int]) -> None:
                    if time.monotonic() - last[0] > 1.0:
                        last[0] = time.monotonic()
                        log(f"    {done_b // 1048576} MB" + (f" of {total // 1048576} MB" if total else ""))
                res = network.download(step.url, Path(step.dest), cfg, expected_sha256=sha, progress=prog, cancel=cancel, opener=opener,
                                       extra_hosts=[network.urllib.parse.urlparse(cfg.mirror_base_url).hostname or ""] if cfg.mirror_base_url else ())
                audit.log("download", prereq=pid, url=step.url, sha256=res.sha256, size=res.size, resumed=res.resumed, sha_verified=bool(sha))
                log(f"    downloaded {res.size // 1048576} MB, SHA-256 {res.sha256[:16]}...")
                last_sha = sha
            except network.NetworkError as exc:
                if exc.kind == "cancelled":
                    return done("cancelled", "Cancelled during download.")
                audit.log("download-failed", prereq=pid, url=step.url, kind=exc.kind, error=str(exc))
                return done("failed", f"Download failed ({exc.kind}): {exc}", exc.advice)
        elif step.kind == "verify":
            f = Path(step.dest)
            expected = offline_checksum(f) if plan.offline_file else last_sha
            vr = verify_installer(f, expected_sha256=expected, publishers=plan.publishers, require=cfg.require_verification,
                                  windows=(env.platform == "windows"))
            audit.log("verify", prereq=pid, file=str(f), ok=vr.ok, method=vr.method, detail=vr.detail, sha256=vr.sha256, publisher=vr.publisher)
            log(f"    verification: {vr.method} - {vr.detail}")
            if not vr.ok:
                return done("failed", "Installer verification failed: " + vr.detail,
                            "The file was NOT run. Re-download it, use winget/the offline folder with a SHA256SUMS file, or ask IT.")
        elif step.kind == "run":
            cmd = list(step.command)
            if step.elevated and env.platform == "windows":
                try:
                    cmd = elevated_windows_command(cmd)
                except ValueError as exc:
                    return done("failed", str(exc))
            audit.log("run", prereq=pid, command=step.command, elevated=step.elevated)
            res: RunResult = env.run_long(cmd, timeout=step.timeout, on_line=log, cancel=cancel, interactive=env.interactive and step.elevated)
            if res.cancelled:
                return done("cancelled", "Cancelled while running: " + step.title)
            if res.timed_out:
                return done("failed", f"Timed out after {step.timeout}s: {step.title}")
            rc = res.rc
            if rc == 1223 and step.elevated:
                return done("uac-declined", "The administrator confirmation was declined or unavailable.",
                            "Ask IT to run the install (use 'IT request'), or choose a per-user method if one exists.")
            if rc in step.reboot_codes:
                reboot = True
            elif rc is None or (rc not in step.ok_codes and rc not in (WINGET_OK if plan.method == "winget" else ())):
                if step.optional:
                    log(f"    optional step failed (exit {rc}); continuing.")
                    continue
                advice = "Check the log above."
                retry = False
                if plan.scope == "user" and plan.method in ("winget", "direct download"):
                    advice = "The per-user install failed; you can retry with the machine-wide install (needs administrator rights)."
                    retry = True
                return done("failed", f"Step failed (exit code {rc}): {step.title}", advice, retry_machine_scope=retry)
    tools_env.activate(env.root, env.platform)
    after = detect(get(pid), env)
    if reboot:
        return done("needs-reboot", "Installed; a restart is required before it can be used.", state_after=after.state)
    if after.state == READY:
        return done("installed", after.version or after.path, state_after=READY)
    if after.state == INSTALLED:
        return done("needs-start", after.detail or "Installed; the runtime still has to be started.",
                    "Start it (Prerequisites tab > Start runtime) and re-check.", state_after=INSTALLED)
    if pid == "wsl2":
        return done("needs-reboot", "WSL was enabled; restart Windows, then re-check.", state_after=after.state)
    return done("failed", "The installer finished but the tool was not found afterwards.",
                "A restart or a new login may be needed so PATH is updated; re-check then.", state_after=after.state)


def start_runtime(pid: str, env: Env, progress: Optional[Progress] = None, wait_seconds: int = 120) -> Status:
    """Start Podman's machine / Docker Desktop and wait until the engine answers. Returns the new status."""
    log = progress or (lambda _m: None)
    p = get(pid)
    st = detect(p, env)
    if st.state == READY or not st.path:
        return st
    if pid == "podman":
        rc, out, err = env.quick([st.path, "machine", "list", "--format", "{{.Name}}"], 30)
        if rc == 0 and not out.strip():
            log("Creating the Podman machine (large download)...")
            env.run_long([st.path, "machine", "init"], 3600, log)
        log("Starting the Podman machine...")
        env.run_long([st.path, "machine", "start"], 600, log)
    elif pid == "docker" and env.platform == "windows":
        import os
        for base in (os.environ.get("ProgramFiles", ""), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            exe = Path(base, "Docker", "Docker", "Docker Desktop.exe") if "Programs" not in base else Path(base, "DockerDesktop", "Docker Desktop.exe")
            if exe.is_file():
                log("Starting Docker Desktop...")
                import subprocess
                subprocess.Popen([str(exe)], creationflags=0x00000008 | 0x00000200, close_fds=True)   # detached
                break
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        st = detect(p, env)
        if st.state == READY:
            return st
        time.sleep(3)
    return detect(p, env)


def pull_sandbox_images(env: Env, cfg, runtime: str, images: Sequence[str] = SANDBOX_IMAGES, progress: Optional[Progress] = None,
                        cancel: Optional[threading.Event] = None, audit: Optional[AuditLog] = None) -> List[InstallResult]:
    """Pull the container images Phase 4 uses (never automatic - the caller must have the user's consent).
    ``registry_mirror`` pulls from an internal registry and re-tags to the standard names Phase 4 expects."""
    audit = audit or AuditLog()
    log = progress or (lambda _m: None)
    exe = env.which(runtime)
    out: List[InstallResult] = []
    if not exe:
        return [InstallResult("images", "failed", f"{runtime} not found.")]
    for img in images:
        if cancel and cancel.is_set():
            out.append(InstallResult(img, "cancelled"))
            break
        have, _o, _e = env.quick([exe, "image", "inspect", img], 30)
        if have == 0:
            log(f"{img}: already present")
            out.append(InstallResult(img, "already-present"))
            continue
        src = f"{cfg.registry_mirror.rstrip('/')}/{img}" if cfg.registry_mirror else img
        log(f"Pulling {src} ...")
        audit.log("image-pull", image=src)
        r = env.run_long([exe, "pull", src], 1800, log, cancel)
        if r.rc == 0 and src != img:
            env.quick([exe, "tag", src, img], 30)
        out.append(InstallResult(img, "installed" if r.rc == 0 else "failed", "" if r.rc == 0 else (r.output.splitlines() or ["pull failed"])[-1][:200],
                                 "" if r.rc == 0 else "Check registry access, or set 'registry_mirror' to your internal registry."))
    return out
