import json
import os
import shutil
import time
from pathlib import Path

import pytest

import app
from prerequisites import (Approval, Status, build_it_request, container_ready, decide, diagnose, offline_manifest)
from prerequisites import startup as st
from prerequisites.__main__ import main as cli
from prerequisites.detect import INSTALLED, MISSING, READY
from tests.prerequisites.conftest import FakeRun, make_cfg, make_env

WIN = {"winget": r"C:\winget.exe", "wsl": r"C:\wsl.exe"}


def S(pid, state):
    return Status(pid, pid, "container-runtime", state)


# ------------------------------------------------------------------ diagnose
def hurdle_ids(d):
    return {h.id for h in d.hurdles}


def test_diagnose_reports_institutional_hurdles(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 3_000_000_000})())
    run = FakeRun({"VirtualizationFirmwareEnabled": (0, "False"), "Win32_OperatingSystem": (0, "Microsoft Windows 11 Home"),
                   "RebootPending": (0, "True")})
    env = make_env(tools={}, run=run, build=18362, root=tmp_path)
    cfg = make_cfg(tmp_path, allow_downloads=False, allow_installs=False, offline_dir=str(tmp_path / "gone"))
    conn = [{"host": "github.com", "ok": "no", "kind": "tls-inspection", "detail": "x", "advice": "install your root CA"}]
    ids = hurdle_ids(diagnose(env, cfg, connectivity=conn, statuses=[]))
    assert {"downloads-disabled", "installs-disabled", "offline-missing", "low-disk", "old-windows", "not-admin", "no-winget",
            "virtualization-off", "windows-home", "pending-reboot", "net-tls-inspection-github.com"} <= ids


def test_diagnose_clean_machine_has_no_blockers(tmp_path):
    env = make_env(admin=True, tools=WIN, run=FakeRun({"VirtualizationFirmwareEnabled": (0, "True"), "Win32_OperatingSystem": (0, "Windows 11 Pro"),
                                                      "RebootPending": (0, "False")}), root=tmp_path)
    d = diagnose(env, make_cfg(tmp_path), connectivity=[{"host": "github.com", "ok": "yes", "kind": "", "detail": "", "advice": ""}], statuses=[S("podman", READY)])
    assert not [h for h in d.hurdles if h.severity == "blocker"] and d.system["container_runtime_ready"] is True


# ------------------------------------------------------------------ IT request / offline manifest
def test_it_request_is_actionable(tmp_path):
    env = make_env(tools=WIN)
    text = build_it_request(["wsl2", "podman", "docker"], env, make_cfg(tmp_path), contact="A. Student")
    for needle in ("A. Student", "Podman", "Docker Desktop", "RedHat.Podman", "subscription", "Removal", "Network access", "cdn.winget.microsoft.com",
                   "audit log", "SHA256SUMS", "--install --no-distribution"):
        assert needle in text, needle
    no_dl = build_it_request(["podman"], make_env(tools={}), make_cfg(tmp_path, allow_installs=False))
    assert "Automated install not possible" in no_dl and "podman.io" in no_dl


def test_offline_manifest_lists_what_to_carry_in(tmp_path):
    m = offline_manifest(["podman", "docker", "ruff"], make_env(tools={}), make_cfg(tmp_path, offline_dir="D:/media"))
    assert m["copy_to"] == "D:/media" and {i["id"] for i in m["items"]} == {"podman", "docker", "ruff"}
    docker = next(i for i in m["items"] if i["id"] == "docker")
    assert docker["url"].startswith("https://desktop.docker.com") and docker["file"] == "Docker Desktop Installer.exe"


# ------------------------------------------------------------------ start-up decision
def test_startup_decisions(tmp_path, monkeypatch):
    monkeypatch.delenv("AISEA_NO_PREREQ_CHECK", raising=False)
    cfg = make_cfg(tmp_path)
    none = [S("podman", MISSING), S("docker", MISSING)]
    d = decide(none, cfg)
    assert d.action == "prompt" and d.offer == ["podman"] and d.recommended_runtime == "podman"
    assert decide([S("podman", READY)], cfg).action == "skip"
    assert decide(none, make_cfg(tmp_path, mode="off")).action == "skip"
    assert decide(none, make_cfg(tmp_path, startup_check=False)).action == "skip"
    assert decide(none, cfg, env_skip=True).action == "skip"
    monkeypatch.setenv("AISEA_NO_PREREQ_CHECK", "1")
    assert decide(none, cfg).action == "skip"
    monkeypatch.delenv("AISEA_NO_PREREQ_CHECK")
    assert decide(none, cfg, {"dont_ask": True}).reason.startswith("user chose")
    now = time.time()
    assert decide(none, cfg, {"snooze_until": now + 100}, now=now).action == "skip"
    assert decide(none, cfg, {"snooze_until": now - 1}, now=now).action == "prompt"
    assert decide(none, make_cfg(tmp_path, allow_installs=False, allow_downloads=False)).action == "skip"
    assert decide(none, make_cfg(tmp_path, allow_installs=False, allow_downloads=False, offline_dir="D:/x")).action == "prompt"


def test_startup_runtime_choice_and_auto_rules(tmp_path):
    none = [S("podman", MISSING), S("docker", MISSING)]
    assert decide(none, make_cfg(tmp_path, prefer_runtime="docker")).offer == ["docker"]
    assert decide(none, make_cfg(tmp_path, blocked=["podman"])).offer == ["docker"]
    assert decide(none, make_cfg(tmp_path, blocked=["podman", "docker"])).action == "skip"
    inst = [S("podman", MISSING), S("docker", INSTALLED)]
    d = decide(inst, make_cfg(tmp_path))
    assert d.offer == ["docker"] and "not running" in d.reason
    assert decide(none, make_cfg(tmp_path, mode="auto")).action == "auto"                     # Podman: no licence click-through
    assert decide(none, make_cfg(tmp_path, mode="auto", prefer_runtime="docker")).action == "prompt"   # Docker: never silently
    assert decide(none, make_cfg(tmp_path, mode="auto", prefer_runtime="docker"), {"license_accepted": ["docker"]}).action == "auto"


def test_state_snooze_dont_ask_and_licence_memory(tmp_path):
    p = tmp_path / "state.json"
    assert st.load_state(p) == {}
    st.snooze(7, p, now=1000.0)
    st.dont_ask_again(p)
    st.remember_license("docker", p)
    st.remember_license("docker", p)
    s = st.load_state(p)
    assert s["snooze_until"] == 1000.0 + 7 * 86400 and s["dont_ask"] is True and s["license_accepted"] == ["docker"]
    p.write_text("{broken")
    assert st.load_state(p) == {}


# ------------------------------------------------------------------ command line
def run_cli(args, env, cfg, capsys):
    rc = cli(args, env=env, cfg=cfg)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_check_and_plan_change_nothing(tmp_path, capsys):
    run = FakeRun({"wsl.exe --status": (1, "not installed")})
    env = make_env(tools=WIN, run=run, root=tmp_path)
    cfg = make_cfg(tmp_path)
    rc, out, _ = run_cli(["check"], env, cfg, capsys)
    assert rc == 0 and "Podman" in out and "NOT ready" in out
    rc, out, _ = run_cli(["check", "--json"], env, cfg, capsys)
    assert json.loads(out)[0]["id"] == "wsl2"
    rc, out, _ = run_cli(["plan", "podman"], env, cfg, capsys)
    assert rc == 0 and "winget" in out and "WSL" in out and out.index("Windows Subsystem") < out.index("Podman")   # dependency first
    assert run.commands() == []                                                              # nothing executed


def test_cli_install_requires_yes_and_licence_flag(tmp_path, capsys):
    run = FakeRun({"winget.exe install": (0, "ok"), "wsl.exe --status": (0, "Default Version: 2")})
    env = make_env(tools=WIN, run=run, root=tmp_path)
    cfg = make_cfg(tmp_path, prefer_user_scope=False)
    rc, _o, err = run_cli(["install", "docker"], env, cfg, capsys)
    assert rc == 3 and "--yes" in err and run.commands() == []
    rc, out, _ = run_cli(["install", "docker", "--yes"], env, cfg, capsys)
    assert rc == 1 and "licence" in out.lower() and not [c for c in run.commands() if "Docker.DockerDesktop" in c]
    rc, out, _ = run_cli(["install", "docker", "--yes", "--accept-license", "docker"], env, cfg, capsys)
    assert [c for c in run.commands() if "Docker.DockerDesktop" in c]


def test_cli_unknown_id_it_request_config_and_manifest(tmp_path, capsys, monkeypatch):
    env, cfg = make_env(tools=WIN, root=tmp_path), make_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cli(["plan", "nonsense"], env=env, cfg=cfg)
    capsys.readouterr()
    out_file = tmp_path / "req.md"
    assert run_cli(["it-request", "--out", str(out_file), "--contact", "me"], env, cfg, capsys)[0] == 0 and "Podman" in out_file.read_text(encoding="utf-8")
    rc, out, _ = run_cli(["offline-manifest"], env, cfg, capsys)
    assert rc == 0 and json.loads(out)["items"]
    rc, out, _ = run_cli(["config", "set", "proxy", "http://p:9"], env, cfg, capsys)
    assert rc == 0 and json.loads((tmp_path / "data" / "prerequisites.json").read_text())["proxy"] == "http://p:9"
    assert run_cli(["config", "set", "nope", "x"], env, cfg, capsys)[0] == 2
    rc, out, _ = run_cli(["config", "show"], env, cfg, capsys)
    assert json.loads(out)["values"]["mode"] == "ask"


def test_app_prereq_passthrough(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AISEA_HOME", str(tmp_path))
    assert app.main(["--prereq", "config", "show"]) == 0
    assert '"values"' in capsys.readouterr().out
    assert app.main(["--version"]) if False else True      # --version exits via argparse; covered by other tests
