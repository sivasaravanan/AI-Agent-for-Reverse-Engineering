"""Docker install failure: explained, with the way forward; installed-but-stopped Docker gets a Start button."""
import threading
import time
from pathlib import Path

import pytest

import setup_doctor.installer as inst
from setup_doctor import Action, run_checks, validate_action
from setup_doctor import catalog as cat
from setup_doctor.checks import known_paths, start_action
from setup_doctor.recovery import diagnose, inner_code
from tests.setup_doctor.test_setup_doctor import FakeEnv


def test_exit_codes_are_explained_in_words():
    assert "virtualization" in inst.explain_exit(4294967291) and "WSL" in inst.explain_exit(-5)
    assert "winget" in inst.explain_exit(2316632070)
    assert "restart" in inst.explain_exit(3010)


def test_inner_installer_code_is_read_from_the_winget_log():
    tail = ["Starting package install...", "Installer failed with exit code: 4294967291"]
    assert inner_code(tail) == 4294967291


def test_new_actions_are_catalog_only_and_pass_validation():
    for a in (cat.wsl_install_action(), cat.docker_admin_action(), cat.docker_cli_start_action("docker")):
        validate_action(a, "py")
    with pytest.raises(ValueError):
        validate_action(Action("x", "x", ("wsl", "--unregister", "Ubuntu")), "py")
    with pytest.raises(ValueError):
        validate_action(Action("x", "x", ("winget", "install", "-e", "--id", "Docker.DockerDesktop", "--accept-package-agreements",
                                          "--accept-source-agreements", "--force")), "py")


def test_elevated_wrapper_refuses_unsafe_arguments_and_quotes_the_rest():
    cmd = inst.elevated_command(["winget", "install", "-e", "--id", "Docker.DockerDesktop"])
    assert cmd[0] == "powershell.exe" and "-EncodedCommand" in cmd
    with pytest.raises(ValueError):
        inst.elevated_command(["winget", "install", "a b"])


def _env(**runs):
    return FakeEnv(which={"winget": "winget"}, runs=runs)


def test_failure_without_wsl_offers_wsl_then_admin_retry():
    env = _env(**{"--status": (1, "", "The Windows Subsystem for Linux is not installed."), "HypervisorPresent": (0, "False,True", ""),
                  "RebootPending": (1, "", ""), "session": (1, "", "")})
    d = diagnose(env, 2316632070, ["Installer failed with exit code: 4294967291"])
    ids = [i.id for i in d.items]
    assert "fix:wsl" in ids and "fix:docker-admin" in ids and "fix:manual" in ids
    assert "virtualization" in d.message.lower() or "WSL 2" in d.message
    wsl = next(i for i in d.items if i.id == "fix:wsl")
    assert wsl.fixable and wsl.actions[0].elevate


def test_failure_with_virtualization_off_says_bios_and_pending_restart_is_named():
    env = _env(**{"--status": (0, "Default Version: 2", ""), "HypervisorPresent": (0, "False,False", ""), "RebootPending": (0, "", ""),
                  "session": (0, "", "")})
    d = diagnose(env, 2316632070, [])
    assert "BIOS" in d.message and "restart" in d.message.lower()
    assert {"fix:virt", "fix:reboot"} <= {i.id for i in d.items}


def test_failure_when_docker_is_in_fact_installed_offers_start():
    kp = known_paths(FakeEnv())
    env = FakeEnv(which={"docker": "docker"}, exists={kp["docker_desktop"][0]}, runs={"info": (1, "", "no daemon")})
    d = diagnose(env, 2316632070, [])
    assert "already installed" in d.message and d.items[0].id == "fix:start" and d.items[0].actions[0].kind == "launch"


def test_start_action_per_backend_and_per_user_install():
    env = FakeEnv(which={"docker": "docker"})
    assert start_action(env).argv[1:] == ("desktop", "start")                     # no Docker Desktop exe known: its command line
    kp = known_paths(FakeEnv())
    env2 = FakeEnv(which={"docker": "docker"}, exists={kp["docker_desktop"][0]})
    assert start_action(env2).kind == "launch"
    assert start_action(FakeEnv(which={"podman": "podman"})).argv[1:] == ("machine", "start")
    assert start_action(FakeEnv()) is None
    assert any("Programs" in p for p in known_paths(type("E", (FakeEnv,), {"getenv": lambda s, n, d="": r"C:\Users\u\AppData\Local" if n == "LOCALAPPDATA" else (r"C:\Program Files" if n == "ProgramFiles" else d)})())["docker_desktop"])


def test_ok_codes_accept_restart_needed():
    a = cat.wsl_install_action()
    assert 3010 in a.ok_codes and 0 in a.ok_codes


def _root():
    import tkinter as tk
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")


def _pump(root, cond, timeout=10):
    end = time.time() + timeout
    while not cond():
        root.update()
        time.sleep(0.02)
        assert time.time() < end, "timed out"
    root.update()


def test_dialog_start_button_enabled_only_when_installed_but_stopped_and_runs_the_start_action(monkeypatch, tmp_path):
    import ui.setup_dialog as sdlg
    root = _root()
    try:
        kp = known_paths(FakeEnv())
        env = FakeEnv(which={"docker": "docker"}, exists={kp["docker_desktop"][0]}, runs={"info": (1, "", "cannot connect")})
        dlg = sdlg.SetupDialog(root, env=env, state_file=tmp_path / "s.json")
        _pump(root, lambda: dlg.items)
        assert str(dlg.start_btn["state"]) == "normal" and "Docker" in dlg.start_btn["text"]
        ran = []
        monkeypatch.setattr(sdlg, "run_actions", lambda acts, env=None, **k: ran.append(list(acts)) or [])
        dlg._start_runtime()
        _pump(root, lambda: ran)
        assert ran[0][0].id == "start:docker" and ran[0][0].kind == "launch"
        dlg.destroy()
        env2 = FakeEnv(which={"docker": "docker"}, runs={"info": (0, "27", "")})
        d2 = sdlg.SetupDialog(root, env=env2, state_file=tmp_path / "s2.json")
        _pump(root, lambda: d2.items)
        assert str(d2.start_btn["state"]) == "disabled"                            # running: nothing to start (the button is still shown)
        d2.destroy()
    finally:
        root.destroy()


def test_failed_docker_install_shows_reason_and_repair_rows(monkeypatch, tmp_path):
    import ui.setup_dialog as sdlg
    from setup_doctor.installer import ActionResult
    root = _root()
    try:
        env = FakeEnv(which={"winget": "winget"}, runs={"--status": (1, "", "not installed"), "HypervisorPresent": (0, "True,True", ""),
                                                         "RebootPending": (1, "", ""), "session": (1, "", "")})
        dlg = sdlg.SetupDialog(root, env=env, state_file=tmp_path / "s.json")
        _pump(root, lambda: dlg.items)
        monkeypatch.setattr(sdlg, "run_actions", lambda acts, env=None, **k: [ActionResult("install:docker", False, 2316632070, message="Exit code 2316632070.",
                                                                                           tail=["Installer failed with exit code: 4294967291"])])
        monkeypatch.setattr(sdlg.messagebox, "askyesno", lambda *a, **k: True)
        dlg._install()
        _pump(root, lambda: any(i.id == "fix:wsl" for i in dlg.items))
        log = dlg.log.get("1.0", "end")
        assert "Why it failed" in log and "WSL" in log
        assert dlg._vars["fix:wsl"].get() is True                                    # the first repair step is pre-ticked
        assert "wsl --install --no-distribution" in dlg.plan.get("1.0", "end")
    finally:
        root.destroy()
