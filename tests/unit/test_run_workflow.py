"""One run workflow: requirements picked up, command choice starts the run, leftovers cleared before it, shared resource panel."""
import sys
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_preview.models import Entry  # noqa: E402
from plugins.build_execution import cleanup  # noqa: E402
from plugins.build_execution.events import BUS  # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("No display")
    yield r
    r.destroy()


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv("AISEA_HOME", str(tmp_path / "home"))
    (tmp_path / "systmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
    return tmp_path / "systmp"


def pump(root, cond=lambda: False, secs=5.0):
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


def view(root):
    from ui.app_preview_view import AppPreviewView
    v = AppPreviewView(root)
    v.pack(fill="both", expand=True)
    pump(root, secs=0.2)
    return v


def entry(install):
    return Entry(id="e", title="app.py", kind="gui", ecosystem="python", argv=["python", "app.py"], file="app.py",
                 install=["pip", "install", "-r", "requirements.txt"] if install else None)


def test_requirements_and_command_choice(root):
    v = view(root)
    v.entry_by_title = {"[gui] with": entry(True), "[gui] without": entry(False)}
    v.entry_var.set("[gui] with")
    started = []
    v.start = lambda: started.append(1)
    v._entry_picked()
    pump(root, lambda: started)
    assert v.install_var.get() and started                       # requirements.txt -> installed in the container; choosing starts it
    v.entry_var.set("[gui] without")
    v.auto_go_var.set(False)
    v._entry_picked()
    pump(root, secs=0.3)
    assert not v.install_var.get() and len(started) == 1         # auto-start can be switched off


def test_leftovers_are_cleared_before_the_run_begins(root, tmp_home):
    left = tmp_home / "be_ws_old"
    left.mkdir()
    (left / "f").write_text("x")
    v = view(root)
    began = []
    v._begin = lambda plan, opts: began.append(plan)
    v._fresh_then("PLAN", None, {"available": False})
    pump(root, lambda: began)
    assert began == ["PLAN"] and not left.exists()
    assert not cleanup.has_leftovers()
    v._fresh_then("P2", None, {"available": False})              # nothing left: no waiting
    assert began == ["PLAN", "P2"]
    left.mkdir()
    v.auto_clean_var.set(False)
    v._fresh_then("P3", None, {"available": False})
    assert began[-1] == "P3" and left.exists()                   # setting off: nothing deleted


def test_fresh_start_never_touches_a_live_run(tmp_home):
    import threading
    left = tmp_home / "be_pv_live"
    left.mkdir()
    BUS.register_cancel("live1", threading.Event())
    try:
        assert "active" in cleanup.fresh_start()[0] and left.exists()
    finally:
        BUS.unregister_cancel("live1")
    cleanup.fresh_start()
    assert not left.exists()


def test_resource_panel_is_shared_and_fed_by_run_events(root):
    from ui.live_run_view import LiveRunView
    from ui.run_panels import ResourcePanel
    v = view(root)
    v.run_id = "r1"
    assert isinstance(v.res_panel, ResourcePanel) and isinstance(LiveRunView(root).res, ResourcePanel)
    v._handle({"type": "stats", "run": "r1", "cpu": 12.0, "mem_mib": 50.0, "mem_text": "50MiB", "pids": 3, "t": time.time()})
    v._handle({"type": "top", "run": "r1", "header": ["PID", "CMD"], "rows": [["1", "python app.py"]], "t": time.time()})
    v._handle({"type": "phase", "run": "r1", "text": "Starting", "t": time.time()})
    assert v._cpu == [12.0] and [v.res_panel.procs.item(i, "values") for i in v.res_panel.procs.get_children()] == [("1", "python app.py")]
    assert v.res_panel.timeline.size() == 1


# ---------------------------------------------------------------- merged features (Build & Test / Live Run -> one view)
def test_build_commands_are_start_options_too(tmp_path):
    from app_preview.discovery import discover_entries
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "package.json").write_text('{"scripts": {"build": "tsc", "test": "jest"}}')
    entries, _ = discover_entries(str(proj))
    kinds = {e.id: e.kind for e in entries}
    assert any(k == "test" for k in kinds.values()) and any(k == "console" and "build" in i for i, k in kinds.items()), kinds


def test_run_history_find_wrap_and_clear_project(root, tmp_home):
    v = view(root)
    forgot = []
    v.on_forget = lambda: forgot.append(1)
    v._run_title, v._last_status = "[run] a.py", "completed"
    v._console("first run output\n")
    v._archive_run()                                              # next run starts: the old output is kept in the picker
    v._set_text(v.console, "")
    v._console("second run output\n")
    assert len(v.hist_box["values"]) == 2
    v.hist_box.current(1)
    v._history_picked()
    assert "first run output" in v.console.get("1.0", "end") and v._viewing
    v.find_var.set("RUN")
    v._highlight_console()
    assert v.console.tag_ranges("hit")
    v.hist_box.current(0)
    v._history_picked()
    assert "second run output" in v.console.get("1.0", "end") and not v._viewing
    v.src_var.set("/some/project")
    left = tmp_home / "be_ws_x"
    left.mkdir()
    v.q.put({"type": "ui_cleared", "report": ["removed 1 project copy"]})
    pump(root, lambda: forgot)
    assert forgot and v.src_var.get() == "" and "Cleared" in v.status_var.get()


def test_main_window_routes_runs_and_lists_one_run_view(tmp_path):
    from ui import theme
    actions = dict(theme.SIDEBAR_GROUPS)["Actions"]
    assert "App Preview" in actions and "Build & Test" not in actions and "Live Run" not in actions     # still reachable under More


def test_old_tabs_are_hidden_but_come_back_when_needed(root, tmp_path):
    from ui import main_window as mw
    win = mw.MainWindow(root, tmp_path / "reports")
    for _ in range(5):
        root.update()
    states = {win.nb.tab(i, "text"): win.nb.tab(i, "state") for i in range(win.nb.index("end"))}
    assert states["Build & Test"] == "hidden" and states["Live Run"] == "hidden" and states["App Preview"] == "normal"
    assert "Live Run" not in win.sidebar.rows and "App Preview" in win.sidebar.rows
    BUS.emit("start", "other-run", title="pytest", argv=["x"], image="i", network=False, timeout_s=5)     # a run started elsewhere
    pump(root, lambda: win.nb.tab(win.nb.select(), "text") == "Live Run")
    assert win.nb.tab(win.nb.select(), "text") == "Live Run" and "Live Run" in win.sidebar.rows
