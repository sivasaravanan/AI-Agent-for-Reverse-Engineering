import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from prerequisites import Approval, build_plan, decide
from prerequisites import startup as st
from prerequisites.detect import MISSING, Status
from tests.prerequisites.conftest import FakeRun, make_cfg, make_env

tkmod = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    yield r
    for _ in range(30):
        try:
            r.update()
        except tk.TclError:
            break
        time.sleep(0.02)
    r.destroy()


def pump(root, cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def tab_for(root, tmp_path, **cfgvals):
    from prerequisites.ui import PrerequisitesTab
    env = make_env(tools={"winget": r"C:\winget.exe", "wsl": r"C:\wsl.exe"}, run=FakeRun({"wsl.exe --status": (1, "not installed")}), root=tmp_path)
    nb = ttk.Notebook(root)
    t = PrerequisitesTab(nb, env=env, cfg=make_cfg(tmp_path, **cfgvals), root_dir=tmp_path)
    nb.add(t, text="Prerequisites")
    return t


def test_tab_lists_everything_and_summarises(root, tmp_path):
    t = tab_for(root, tmp_path)
    assert pump(root, lambda: len(t.tree.get_children()) >= 10)
    assert "podman" in t.tree.get_children() and "No container runtime is ready" in t.summary.get()
    assert t.tree.item("podman")["values"][2] == "Missing"


def test_install_flow_shows_plan_and_needs_licence_tick(root, tmp_path):
    from prerequisites.ui import ApprovalDialog
    t = tab_for(root, tmp_path, prefer_user_scope=False)
    assert pump(root, lambda: t.statuses)
    plan = build_plan("docker", t.env, t.cfg)
    dlg = ApprovalDialog(root, [plan], [], modal=False)
    root.update()
    assert dlg.result is None
    dlg.lic_vars["docker"].set(False)
    from tkinter import messagebox
    shown = []
    orig = messagebox.showinfo
    messagebox.showinfo = lambda *a, **k: shown.append(a)
    try:
        dlg._ok()
    finally:
        messagebox.showinfo = orig
    assert shown and dlg.result is None                                 # refused until the licence box is ticked
    dlg.lic_vars["docker"].set(True)
    dlg._ok()
    assert dlg.result and dlg.result[0].plan_hash == plan.plan_hash and dlg.result[0].license_accepted


def test_settings_dialog_locks_policy_keys_and_saves(root, tmp_path):
    from prerequisites import load_config
    from prerequisites.ui import SettingsDialog
    shipped = tmp_path / "ship.json"
    shipped.write_text(json.dumps({"offline_dir": "D:/approved"}))
    user = tmp_path / "u.json"
    cfg = load_config(user, shipped, tmp_path / "m")
    dlg = SettingsDialog(root, cfg, user, modal=False)
    root.update()
    dlg.vars["proxy"].set("http://p:3128")
    dlg.vars["offline_dir"].set("C:/hack")
    import tkinter.messagebox as mb
    mb.showwarning = lambda *a, **k: None
    dlg._save()
    assert json.loads(user.read_text()) ["proxy"] == "http://p:3128" and "offline_dir" not in json.loads(user.read_text())


def test_startup_prompt_snooze_and_never(root, tmp_path):
    from prerequisites.ui import StartupPrompt
    t = tab_for(root, tmp_path)
    assert pump(root, lambda: t.statuses)
    d = decide(t.statuses, t.cfg)
    assert d.action == "prompt"
    p = StartupPrompt(root, t, d, modal=False)
    root.update()
    p._later()
    s = st.load_state(st.state_path(tmp_path))
    assert s["snooze_until"] > time.time()
    p2 = StartupPrompt(root, t, d, modal=False)
    p2._never()
    assert st.load_state(st.state_path(tmp_path))["dont_ask"] is True


def test_progress_window_finish_is_thread_safe(root):
    from prerequisites.ui import ProgressWindow
    ev = threading.Event()
    w = ProgressWindow(root, "x", ev)
    th = threading.Thread(target=lambda: (w.post("line one"), w.post_finish("all done")))
    th.start()
    th.join()
    assert pump(root, lambda: w.status.get() == "all done")
    assert "line one" in w.text.get("1.0", "end")
    w._cancel() if False else None


def test_setup_dialog_gets_institution_button_and_manager_opens(root, tmp_path):
    from setup_doctor.checks import SystemEnv
    from ui.setup_dialog import SetupDialog
    import ui.institution_menu as im
    d = SetupDialog(root, env=SystemEnv(), state_file=tmp_path / "s.json")
    root.update()
    texts = [w.cget("text") for w in _all(d) if isinstance(w, ttk.Button)]
    assert "Institution options..." in texts
    pump(root, lambda: not d._busy, 20)
    d.destroy()
    win = im.open_manager(root, tmp_path)
    root.update()
    assert any(isinstance(w, ttk.Treeview) for w in _all(win))
    win.destroy()


def _all(w):
    out = [w]
    for c in w.winfo_children():
        out += _all(c)
    return out


def test_setup_startup_check_honours_policy(tmp_path, monkeypatch):
    import ui.setup_dialog as sd
    import prerequisites
    calls = []

    class FakeRoot:
        def after(self, *a, **k):
            calls.append(a)
    cfg_off = make_cfg(tmp_path, mode="off")
    monkeypatch.setattr(prerequisites, "load_config", lambda *a, **k: cfg_off)
    sd.schedule_startup_check(FakeRoot(), state_file=tmp_path / "s.json")
    assert calls == []                                                   # policy switched the start-up prompt off
    cfg_on = make_cfg(tmp_path)
    monkeypatch.setattr(prerequisites, "load_config", lambda *a, **k: cfg_on)
    sd.schedule_startup_check(FakeRoot(), state_file=tmp_path / "s2.json")
    assert calls                                                         # default behaviour unchanged


def test_setup_doctor_children_get_institution_environment(tmp_path, monkeypatch):
    import prerequisites.config as pc
    from setup_doctor import installer
    assert installer._child_env() is None or isinstance(installer._child_env(), dict)
    pem = tmp_path / "ca.pem"
    pem.write_text("x")
    cfg = make_cfg(tmp_path, proxy="http://proxy:8080", ca_bundle=str(pem))
    monkeypatch.setattr(pc, "load_config", lambda *a, **k: cfg)
    env = installer._child_env()
    assert env["HTTPS_PROXY"] == "http://proxy:8080" and env["PIP_CERT"] == str(pem)
    from setup_doctor.catalog import Action
    with pytest.raises(ValueError):                                      # the fixed-command safety rule is unchanged
        installer.validate_action(Action("x", "x", ("winget", "install", "-e", "--id", "Evil.Package", "--accept-package-agreements", "--accept-source-agreements")))
