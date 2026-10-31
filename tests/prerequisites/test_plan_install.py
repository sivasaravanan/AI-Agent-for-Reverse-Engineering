import base64
import json
import threading
from pathlib import Path

import pytest

from prerequisites import (Approval, AuditLog, InstallPlan, PlanRefusal, build_plan, detect, expand_dependencies, get, pull_sandbox_images,
                           run_plan, start_runtime)
from prerequisites.catalog import BY_ID, CATALOG, DIRECT
from prerequisites.install import elevated_windows_command
from prerequisites.network import DownloadResult
from tests.prerequisites.conftest import FakeRun, make_cfg, make_env

WIN_TOOLS = {"winget": r"C:\winget.exe", "wsl": r"C:\wsl.exe"}


def test_catalog_invariants():
    assert len({p.id for p in CATALOG}) == len(CATALOG)
    kinds = {"winget", "direct", "venv-pip", "npm-local", "brew", "apt", "dnf", "pacman", "zypper", "feature"}
    for p in CATALOG:
        for plat, ms in p.methods.items():
            assert plat in ("windows", "linux", "darwin")
            for m in ms:
                assert m.kind in kinds and (m.kind != "direct" or m.ref in DIRECT)
        for n in p.needs:
            assert n in BY_ID
    assert BY_ID["docker"].license_ack and not BY_ID["podman"].license_ack
    for key, spec in DIRECT.items():
        assert all(u.startswith("https://") for u in spec.urls.values()) and spec.publishers


# ------------------------------------------------------------------ planning
def test_windows_winget_defaults_to_per_user_when_supported(tmp_path):
    env = make_env(tools=WIN_TOOLS)
    p = build_plan("podman", env, make_cfg(tmp_path))
    assert isinstance(p, InstallPlan) and p.method == "winget" and p.scope == "user" and not p.needs_admin
    cmd = p.steps[0].command
    assert cmd[:3] == [r"C:\winget.exe", "install", "-e"] and "RedHat.Podman" in cmd and cmd[-2:] == ["--scope", "user"]
    assert any("machine" in " ".join(s.command) or "init" in s.command for s in p.steps)      # optional podman machine steps present
    m = build_plan("podman", env, make_cfg(tmp_path), scope="machine")
    assert m.scope == "machine" and m.needs_admin


def test_docker_requires_licence_and_admin_flag(tmp_path):
    p = build_plan("docker", make_env(tools=WIN_TOOLS), make_cfg(tmp_path, prefer_user_scope=False))
    assert p.license_ack_required and p.needs_admin and "subscription" in p.license_note


def test_no_winget_falls_back_to_verified_direct_download(tmp_path):
    env = make_env(tools={})
    p = build_plan("podman", env, make_cfg(tmp_path))
    assert p.method == "direct download" and p.steps[0].kind == "download" and p.steps[0].url.startswith("https://github.com/containers/podman")
    assert [s.kind for s in p.steps[:3]] == ["download", "verify", "run"] and p.steps[2].command[1:4] == ["/install", "/quiet", "/norestart"]
    assert p.needs_admin and "api.github.com" in p.hosts
    arm = build_plan("podman", make_env(tools={}, arch="arm64"), make_cfg(tmp_path))
    assert arm.steps[0].url.endswith("windows-arm64.exe")


def test_docker_direct_user_scope_flag_and_licence_args(tmp_path):
    p = build_plan("docker", make_env(tools={}), make_cfg(tmp_path))
    run = p.steps[2].command
    assert "--user" in run and "--accept-license" in run and not p.needs_admin           # per-user install, licence flag only because it is gated


def test_mirror_overrides_download_host(tmp_path):
    p = build_plan("docker", make_env(tools={}), make_cfg(tmp_path, mirror_base_url="https://mirror.corp.example/installers/"))
    assert p.steps[0].url == "https://mirror.corp.example/installers/Docker%20Desktop%20Installer.exe" and "mirror.corp.example" in p.hosts


def test_policy_blocks_and_disables(tmp_path):
    env = make_env(tools=WIN_TOOLS)
    assert isinstance(build_plan("docker", env, make_cfg(tmp_path, blocked=["docker"])), PlanRefusal)
    r = build_plan("podman", env, make_cfg(tmp_path, allow_installs=False))
    assert isinstance(r, PlanRefusal) and "policy" in r.reason.lower()
    r = build_plan("podman", make_env(tools={}), make_cfg(tmp_path, allow_downloads=False))
    assert isinstance(r, PlanRefusal) and "downloads are disabled" in " ".join([r.reason, r.advice]).lower()
    w = build_plan("podman", env, make_cfg(tmp_path, allow_winget=False))
    assert isinstance(w, InstallPlan) and w.method == "direct download"


def test_offline_folder_preferred_and_checksummed(tmp_path):
    off = tmp_path / "offline"
    off.mkdir()
    inst = off / "podman-installer-windows-amd64.exe"
    inst.write_bytes(b"MZ")
    import hashlib
    (off / "SHA256SUMS").write_text(f"{hashlib.sha256(b'MZ').hexdigest()}  {inst.name}\n")
    p = build_plan("podman", make_env(tools=WIN_TOOLS), make_cfg(tmp_path, offline_dir=str(off)))
    assert p.method == "offline folder" and not p.needs_network and p.offline_file == str(inst) and p.steps[0].kind == "verify"
    from prerequisites.plan import offline_checksum
    assert offline_checksum(inst) == hashlib.sha256(b"MZ").hexdigest()


def test_linux_and_mac_methods(tmp_path):
    cfg = make_cfg(tmp_path)
    lin_root = build_plan("podman", make_env("linux", admin=True, tools={"apt-get": "/usr/bin/apt-get"}), cfg)
    assert lin_root.steps[0].command == ["/usr/bin/apt-get", "install", "-y", "podman"] and not lin_root.needs_admin
    gui = build_plan("podman", make_env("linux", tools={"apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec"}), cfg)
    assert gui.steps[0].command[0] == "/usr/bin/pkexec" and gui.needs_admin
    cli = build_plan("podman", make_env("linux", interactive=True, tools={"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}), cfg)
    assert cli.steps[0].command[0] == "/usr/bin/sudo"
    none = build_plan("podman", make_env("linux", tools={"apt-get": "/usr/bin/apt-get"}), cfg)
    assert isinstance(none, PlanRefusal) and "sudo" in none.advice
    mac = build_plan("docker", make_env("darwin", tools={"brew": "/opt/homebrew/bin/brew"}), cfg)
    assert mac.steps[0].command[-3:] == ["install", "--cask", "docker"]
    assert isinstance(build_plan("podman", make_env("linux", tools={}), cfg), PlanRefusal)


def test_private_tool_installs_need_no_admin_and_respect_mirrors(tmp_path):
    cfg = make_cfg(tmp_path, pip_index_url="https://pypi.corp/simple", proxy="http://p:1", ca_bundle="C:/ca.pem", npm_registry="https://npm.corp/")
    env = make_env("windows", tools={"npm": "/usr/bin/npm"}, root=tmp_path)
    py = build_plan("ruff", env, cfg)
    pip = py.steps[-1].command
    assert not py.needs_admin and "--only-binary=:all:" in pip and pip[pip.index("--index-url") + 1] == "https://pypi.corp/simple"
    assert "--proxy" in pip and "--cert" in pip and py.hosts == ["pypi.corp"] and str(tmp_path) in " ".join(pip)
    npm = build_plan("eslint", env, cfg).steps[0].command
    assert "--ignore-scripts" in npm and "eslint@9" in npm and "--registry" in npm and "--cafile" in npm
    assert isinstance(build_plan("ruff", make_env(python=None, root=tmp_path), cfg), PlanRefusal)
    assert isinstance(build_plan("eslint", make_env(tools={}, root=tmp_path), cfg), PlanRefusal)


def test_plan_hash_changes_with_content_and_describe_is_complete(tmp_path):
    env = make_env(tools=WIN_TOOLS)
    a, b = build_plan("podman", env, make_cfg(tmp_path)), build_plan("podman", env, make_cfg(tmp_path))
    assert a.plan_hash == b.plan_hash != build_plan("podman", env, make_cfg(tmp_path), scope="machine").plan_hash
    text = build_plan("docker", env, make_cfg(tmp_path)).describe()
    for needle in ("Administrator rights", "Network", "Steps:", "winget", "Licence"):
        assert needle in text


def test_dependencies_expand_in_order(tmp_path):
    env = make_env(tools={"wsl": r"C:\wsl.exe"}, run=FakeRun({"wsl.exe --status": (1, "not installed")}))
    assert expand_dependencies(["podman"], env) == ["wsl2", "podman"]
    env2 = make_env(tools={}, root=tmp_path)
    assert expand_dependencies(["eslint"], env2) == ["node", "eslint"]
    assert expand_dependencies(["podman"], make_env("linux")) == ["podman"]


# ------------------------------------------------------------------ running
def run_env(rules, tools=None, **kw):
    run = FakeRun(rules)
    return make_env(tools=WIN_TOOLS if tools is None else tools, run=run, **kw), run


def test_refused_without_exact_approval_or_licence(tmp_path):
    env, run = run_env({})
    cfg = make_cfg(tmp_path)
    plan = build_plan("docker", env, cfg)
    audit = AuditLog(tmp_path / "a.jsonl")
    assert run_plan(plan, None, env, cfg, audit=audit).status == "refused"
    assert run_plan(plan, Approval("deadbeef"), env, cfg, audit=audit).status == "refused"
    r = run_plan(plan, Approval.for_plan(plan, license_accepted=False), env, cfg, audit=audit)
    assert r.status == "refused" and "licence" in r.detail.lower() and run.commands() == []
    assert run_plan(build_plan("podman", env, make_cfg(tmp_path, blocked=["docker"])), None, env, cfg).status == "refused"


def test_winget_success_detects_result(tmp_path):
    env, run = run_env({"winget.exe install": (0, "Successfully installed"), "podman.exe --version": (0, "podman version 5.8.3"),
                        "podman.exe info": (0, "5.8.3")}, tools={**WIN_TOOLS})
    cfg = make_cfg(tmp_path)
    plan = build_plan("podman", env, cfg)
    plan.steps = plan.steps[:1]
    env._tools["podman"] = r"C:\podman.exe"
    # pretend podman only appears after the install step
    calls = {"n": 0}
    real_which = env.which
    env.which = lambda exe: (None if exe == "podman" and not any("winget" in c for c in run.commands()) else real_which(exe))
    lines = []
    r = run_plan(plan, Approval.for_plan(plan), env, cfg, progress=lines.append, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "installed" and r.state_after == "ready" and any("Successfully" in x for x in lines)


def test_user_scope_failure_offers_machine_scope_and_reboot_and_uac(tmp_path):
    cfg = make_cfg(tmp_path)
    env, run = run_env({"winget.exe install": (1, "installer failed")})
    plan = build_plan("podman", env, cfg)
    plan.steps = plan.steps[:1]
    r = run_plan(plan, Approval.for_plan(plan), env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "failed" and r.retry_machine_scope and "machine-wide" in r.advice

    env, run = run_env({"winget.exe install": (3010, "")})
    plan = build_plan("podman", env, cfg)
    plan.steps = plan.steps[:1]
    assert run_plan(plan, Approval.for_plan(plan), env, cfg, audit=AuditLog(tmp_path / "a.jsonl")).status == "needs-reboot"

    env, run = run_env({"-EncodedCommand": (1223, "canceled by the user")})
    plan = build_plan("wsl2", env, cfg)
    r = run_plan(plan, Approval.for_plan(plan), env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "uac-declined" and "IT" in r.advice


def test_direct_download_flow_verifies_before_running(tmp_path, monkeypatch):
    env, run = run_env({"installer.exe": (0, ""), "Installer.exe": (0, "")}, tools={})
    env.root = tmp_path
    cfg = make_cfg(tmp_path)
    plan = build_plan("docker", env, cfg)
    dest = Path(plan.steps[0].dest)

    def fake_download(url, d, cfg_, **kw):
        Path(d).parent.mkdir(parents=True, exist_ok=True)
        Path(d).write_bytes(b"MZfake")
        return DownloadResult(Path(d), "ab" * 32, 6, False, url)
    monkeypatch.setattr("prerequisites.install.network.download", fake_download)
    ap = Approval.for_plan(plan, license_accepted=True)

    # 1) unverifiable (no PowerShell signature available here) -> refused, installer never runs
    r = run_plan(plan, ap, env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "failed" and "verification failed" in r.detail.lower() and "NOT run" in r.advice
    assert not [c for c in run.commands() if "Desktop Installer" in c]
    # 2) verified -> installer runs silently with the approved arguments
    from prerequisites.verify import VerifyResult
    monkeypatch.setattr("prerequisites.install.verify_installer", lambda *a, **k: VerifyResult(True, "authenticode", "Verified.", "ab" * 32, "Docker Inc"))
    run.calls.clear()
    r = run_plan(plan, ap, env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    ran = [c for c in run.commands() if "Desktop Installer" in c]
    assert ran and "--quiet" in ran[0] and "--accept-license" in ran[0] and "--user" in ran[0]
    assert r.status in ("failed", "installed", "needs-start")       # docker binary not present in the fake -> 'failed: not found afterwards'
    assert r.status == "failed" and "not found afterwards" in r.detail


def test_download_failure_is_reported_with_advice(tmp_path, monkeypatch):
    from prerequisites.network import NetworkError
    env, _run = run_env({}, tools={})
    cfg = make_cfg(tmp_path)
    plan = build_plan("podman", env, cfg)
    monkeypatch.setattr("prerequisites.install.network.download",
                        lambda *a, **k: (_ for _ in ()).throw(NetworkError("tls-inspection", "CERTIFICATE_VERIFY_FAILED", "install your root CA")))
    monkeypatch.setattr("prerequisites.install._resolve_github_checksum", lambda *a, **k: "")
    r = run_plan(plan, Approval.for_plan(plan), env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "failed" and "tls-inspection" in r.detail and "root CA" in r.advice


def test_cancel_before_start_and_audit_masks_secrets(tmp_path):
    env, run = run_env({})
    cfg = make_cfg(tmp_path)
    plan = build_plan("podman", env, cfg)
    ev = threading.Event()
    ev.set()
    audit = AuditLog(tmp_path / "a.jsonl")
    assert run_plan(plan, Approval.for_plan(plan), env, cfg, cancel=ev, audit=audit).status == "cancelled"
    audit.log("download", url="https://user:S3cr3t@host/x", note="password=hunter2")
    blob = (tmp_path / "a.jsonl").read_text()
    assert "S3cr3t" not in blob and "hunter2" not in blob and "install-start" in blob and "install-result" in blob
    assert all(json.loads(line)["ts"] for line in blob.splitlines())


def test_already_present_is_not_reinstalled(tmp_path):
    env, run = run_env({"podman.exe --version": (0, "podman 5"), "podman.exe info": (0, "5")}, tools={**WIN_TOOLS, "podman": r"C:\podman.exe"})
    cfg = make_cfg(tmp_path)
    plan = build_plan("podman", env, cfg)
    r = run_plan(plan, Approval.for_plan(plan), env, cfg, audit=AuditLog(tmp_path / "a.jsonl"))
    assert r.status == "already-present" and not run.commands()


def test_elevated_command_is_encoded_and_strict():
    cmd = elevated_windows_command([r"C:\Program Files\Docker\Docker Desktop Installer.exe", "install", "--quiet"])
    assert cmd[0] == "powershell.exe" and "-EncodedCommand" in cmd
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert "-Verb RunAs" in script and "Docker Desktop Installer.exe" in script and "exit 1223" in script
    with pytest.raises(ValueError):
        elevated_windows_command(["x.exe", "a b"])
    with pytest.raises(ValueError):
        elevated_windows_command(["x.exe", "a;calc"])


def test_pull_images_uses_mirror_and_retags(tmp_path):
    env, run = run_env({"image inspect": (1, ""), "pull": (0, ""), "tag": (0, "")}, tools={"podman": "/usr/bin/podman"})
    cfg = make_cfg(tmp_path, registry_mirror="artifactory.corp/docker/")
    res = pull_sandbox_images(env, cfg, "podman", images=["python:3.12-slim"], audit=AuditLog(tmp_path / "a.jsonl"))
    assert res[0].status == "installed"
    cmds = run.commands("long") + run.commands("quick")
    assert any("pull artifactory.corp/docker/python:3.12-slim" in c for c in cmds)
    assert any("tag artifactory.corp/docker/python:3.12-slim python:3.12-slim" in c for c in cmds)
    env2, run2 = run_env({"image inspect": (0, "")}, tools={"podman": "/usr/bin/podman"})
    assert pull_sandbox_images(env2, cfg, "podman", images=["python:3.12-slim"], audit=AuditLog(tmp_path / "a.jsonl"))[0].status == "already-present"
    assert pull_sandbox_images(make_env(tools={}), cfg, "podman", audit=AuditLog(tmp_path / "a.jsonl"))[0].status == "failed"


def test_detect_states_for_container_runtime():
    env, _ = run_env({"info": (1, "Cannot connect"), "--version": (0, "docker 27")}, tools={"docker": "/usr/bin/docker"})
    st = detect(get("docker"), env)
    assert st.state == "installed" and "not usable" in st.detail
    env, _ = run_env({"info": (0, "ok"), "--version": (0, "docker 27")}, tools={"docker": "/usr/bin/docker"})
    assert detect(get("docker"), env).state == "ready"
    assert detect(get("docker"), make_env(tools={})).state == "missing"
    assert detect(get("wsl2"), make_env("linux")).state == "unsupported"


def test_start_runtime_podman_creates_then_starts_machine():
    env, run = run_env({"machine list": (0, ""), "machine init": (0, ""), "machine start": (0, ""), "info": (0, "ok"), "--version": (0, "p")},
                       tools={"podman": "/usr/bin/podman"})
    # runtime is "ready" because info succeeds -> nothing to do
    assert start_runtime("podman", env).state == "ready" and not run.commands()
    env2, run2 = run_env({"machine list": (0, ""), "machine init": (0, ""), "machine start": (0, ""), "--version": (0, "p"), "info": (1, "no machine")},
                         tools={"podman": "/usr/bin/podman"})
    start_runtime("podman", env2, wait_seconds=0)
    assert [c.split("podman ", 1)[1] for c in run2.commands()] == ["machine init", "machine start"]
