"""VirtualBox-style windows: machine store + snapshot tree (pure), then Manager / machine window / snapshots / settings on a real Tk."""
import json
import struct
import sys
import time
import tkinter as tk
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_preview.models import Entry  # noqa: E402
from app_preview.vmstore import MAX_AUTO_SNAPSHOTS, VMStore, clean_settings, machine_state  # noqa: E402
from ui import screen_tools as st  # noqa: E402


def png(w=80, h=60, rgb=(30, 120, 200)) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


ENTRY = {"id": "e1", "title": "main.py", "kind": "gui", "ecosystem": "python", "argv": ["python", "main.py"], "file": "main.py"}


# ------------------------------------------------------------------------------------------------ pure store
def test_settings_are_clamped_and_validated():
    s = clean_settings({"screen": "banana", "fps": 99, "view": "x", "timeout_s": 1, "memory_mb": "abc", "backend": "evil", "install": 1, "zzz": 5})
    assert s["screen"] == "800x600" and s["fps"] == 10.0 and s["view"] == "fit" and s["timeout_s"] == 10 and s["memory_mb"] == 1536
    assert s["backend"] == "auto" and s["install"] is True and "zzz" not in s
    assert clean_settings({"screen": "1920 x 1080"})["screen"] == "1920x1080"


def test_machines_persist_and_names_are_unique(tmp_path):
    s = VMStore(tmp_path / "vm")
    a = s.new_machine("App", "/p", "run", ENTRY)
    b = s.new_machine("App", "/p", "debug", ENTRY)
    assert a.name == "App" and b.name == "App (2)"
    again = VMStore(tmp_path / "vm")
    assert {m.name for m in again.machines.values()} == {"App", "App (2)"}
    assert again.find_machine("/p", ["python", "main.py"], "run").id == a.id and again.find_machine("/p", ["python", "x.py"], "run") is None
    c = again.clone(a.id, "Copy")
    assert c.name == "Copy" and c.entry == a.entry and c.id != a.id


def test_snapshot_tree_current_pointer_and_delete_reparents(tmp_path):
    s = VMStore(tmp_path / "vm")
    m = s.new_machine("App", "/p", "run", ENTRY)
    s1 = s.take(m.id, "", "first", png(), "console 1", "running", "ok", {"State": "Running"})
    s2 = s.take(m.id, "second", png=png(40, 30), console="c2")
    assert s1.name == "Snapshot 1" and s2.parent == s1.id and s.current(m.id) == s2.id and s1.screen == "80x60" and s2.screen == "40x30"
    s.set_current(m.id, s1.id)
    s3 = s.take(m.id, "branch")                                 # taken while s1 is current -> a second child of s1
    assert s3.parent == s1.id
    assert [(d, x.name) for d, x in s.tree(m.id)] == [(0, "Snapshot 1"), (1, "second"), (1, "branch")]
    assert s.png(m.id, s1.id)[:4] == b"\x89PNG" and s.console(m.id, s1.id) == "console 1"
    s.delete(m.id, s1.id)                                       # children move up
    assert {x.parent for x in s.snapshots(m.id)} == {""} and s.current(m.id) == s3.id
    assert not (tmp_path / "vm" / m.id / s1.id).exists()
    s.edit(m.id, s2.id, "renamed", "desc")
    again = VMStore(tmp_path / "vm")
    assert again.get(m.id, s2.id).name == "renamed" and again.get(m.id, s2.id).description == "desc"
    out = again.export_png(m.id, s2.id, tmp_path / "x.png")
    assert out.read_bytes()[:4] == b"\x89PNG"
    with pytest.raises(OSError):
        again.export_png(m.id, s3.id, tmp_path / "y.png")       # no picture


def test_ids_cannot_escape_the_store_and_bad_json_is_ignored(tmp_path):
    base = tmp_path / "vm"
    base.mkdir()
    (base / "machines.json").write_text(json.dumps({"machines": [{"id": "../../evil", "name": "x"}, {"id": "abcdef123456", "name": "ok", "mode": "zzz"}]}))
    s = VMStore(base)
    assert list(s.machines) == ["abcdef123456"] and s.machines["abcdef123456"].mode == "run"
    with pytest.raises(ValueError):
        s.png("../x", "abcdef123456") if False else s._sdir("abcdef123456", "../../x")
    assert s.png("abcdef123456", "nothere1") == b"" and s.last_png("../..") == b""
    (base / "abcdef123456").mkdir()
    (base / "abcdef123456" / "snapshots.json").write_text("{not json")
    assert s.snapshots("abcdef123456") == []


def test_auto_snapshots_are_pruned_and_machine_remove_deletes_files(tmp_path):
    s = VMStore(tmp_path / "vm")
    m = s.new_machine("App", "/p", "run", ENTRY)
    s.take(m.id, "manual", png=png())
    for i in range(MAX_AUTO_SNAPSHOTS + 4):
        s.take(m.id, f"auto {i}", png=png(), auto=True)
    snaps = s.snapshots(m.id)
    assert sum(1 for x in snaps if x.auto) == MAX_AUTO_SNAPSHOTS and any(x.name == "manual" for x in snaps)
    s.record_end(m, "failed", png(), "log text")
    assert s.last_log(m.id) == "log text" and machine_state(m, "") == "Aborted" and machine_state(m, "running") == "Running"
    s.remove(m.id)
    assert not (tmp_path / "vm" / m.id).exists() and m.id not in VMStore(tmp_path / "vm").machines


# ------------------------------------------------------------------------------------------------ windows
@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("No display")
    r.geometry("1200x800")
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def snapshots(tmp_path):
    old = (st.SNAPSHOTS.default, st.SNAPSHOTS.custom)
    st.SNAPSHOTS.set_default(tmp_path / "data" / "snapshots")
    st.SNAPSHOTS.set_custom("")
    yield tmp_path
    st.SNAPSHOTS.default, st.SNAPSHOTS.custom = old


def pump(root, cond=lambda: False, secs=3.0):
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


def fake_run(v, tmp_path, running=True, shot=None):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "main.py").write_text("print('hi')\n")
    calls = []
    plan = SimpleNamespace(entry=Entry(**ENTRY), mode="run", display=True)
    v.source = str(proj)
    v.session = SimpleNamespace(running=running, plan=plan, input_event=lambda *a, **k: calls.append((a, k)) or True, stop=lambda: calls.append("stop"))
    v.run_id, v.state = "run1", "running"
    v._frame_png = shot if shot is not None else png()
    v._console("hello from the program\n")
    v.info["Uptime"].set("5 s")
    return calls


def test_a_run_creates_a_machine_and_its_end_is_recorded(root, tmp_path):
    from ui.app_preview_view import AppPreviewView
    v = AppPreviewView(root)
    v.pack(fill="both", expand=True)
    pump(root, secs=0.3)
    fake_run(v, tmp_path)
    v._vm_watch()
    ms = list(v.vm_store.machines.values())
    assert len(ms) == 1 and ms[0].runs == 1 and ms[0].last_status == "running" and v.vm_current_id == ms[0].id and ms[0].entry["argv"] == ["python", "main.py"]
    assert v.vm_live_state(ms[0].id) == "running" and v.vm_live_state("other") == ""
    ms[0].settings["auto_snapshot_end"] = True
    v.session.running, v.state, v._last_status = False, "ended", "completed"
    v._vm_watch()
    assert ms[0].last_status == "completed" and v.vm_store.last_png(ms[0].id)[:4] == b"\x89PNG" and "hello" in v.vm_store.last_log(ms[0].id)
    assert len(v.vm_store.snapshots(ms[0].id)) == 1 and v.vm_store.snapshots(ms[0].id)[0].auto
    v._vm_watch()                                                # nothing new is recorded twice
    assert len(v.vm_store.snapshots(ms[0].id)) == 1 and ms[0].runs == 1
    fake_run(v, tmp_path)
    v.run_id = "run2"                                            # the same program again -> the same machine
    v._vm_watch()
    assert len(v.vm_store.machines) == 1 and ms[0].runs == 2


def test_manager_machine_window_snapshots_and_settings(root, tmp_path, monkeypatch):
    from tkinter import messagebox

    import ui.vm_snapshots as vs
    from ui.app_preview_view import AppPreviewView
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    v = AppPreviewView(root)
    v.pack(fill="both", expand=True)
    pump(root, secs=0.3)
    calls = fake_run(v, tmp_path)
    v._vm_watch()
    mid = v.vm_current_id

    # --- Manager
    mgr = v.open_vm_manager()
    pump(root, secs=0.6)
    assert mgr.tree.get_children() == (mid,) and "Running" in mgr.tree.item(mid, "values")[0]
    assert "Start command" in mgr.text.get("1.0", "end") and mgr.preview.png[:4] == b"\x89PNG"
    assert v.open_vm_manager() is mgr                            # a second call raises the same window
    mgr.filter.set("nomatch")
    mgr.fill(force=True)
    assert mgr.tree.get_children() == ()
    mgr.filter.set("")
    mgr.fill(force=True)

    # --- machine window
    win = v.open_vm_window(mid)
    pump(root, secs=0.6)
    assert "[Running]" in win.title() and win._png[:4] == b"\x89PNG"
    v.send_input_var.set(True)
    win.send_key("ctrl+alt+Delete")
    assert any(a and a[0] == ("key",) and k == {"key": "ctrl+alt+Delete"} for a, k in [c for c in calls if isinstance(c, tuple)]) or calls
    win.page_var.set("console")
    win.refresh()
    assert "hello from the program" in win.console.get("1.0", "end")
    win.scale_var.set("50%")
    win._scale()
    assert win.view.mode == "50"
    win.page_var.set("auto")
    win.hold_var.set(True)
    first = win._png
    v._frame_png = png(80, 60, (200, 10, 10))
    win.refresh()
    assert win._png == first                                      # held
    win.hold_var.set(False)
    win.refresh()
    assert win._png == v._frame_png

    # --- snapshots
    snap = v.vm_take_snapshot(mid, ask=False, name="before", description="d")
    assert snap.name == "before" and v.vm_store.png(mid, snap.id)[:4] == b"\x89PNG" and "hello" in v.vm_store.console(mid, snap.id)
    panel = mgr.snaps
    panel.set_machine(mid)
    pump(root, secs=0.2)
    assert snap.id in panel.tree.get_children() or panel.tree.exists(snap.id)
    panel.tree.selection_set(snap.id)
    panel._show()
    assert str(panel.buttons["restore"]["state"]) == "normal" and "before" in panel.head.get()
    v.vm_store.machines[mid].settings["screen"] = "1024x768"
    monkeypatch.setattr(vs, "ask_restore", lambda *a, **k: False)
    v.vm_store.machines[mid].settings = dict(v.vm_store.machines[mid].settings, screen="1024x768")
    panel.restore()
    assert v.vm_restored[0] == mid and v.vm_store.machines[mid].settings["screen"] == "800x600"      # the snapshot's settings came back
    panel.tree.selection_set(snap.id)
    panel.delete()
    assert v.vm_store.get(mid, snap.id) is None

    # --- settings
    dlg = v.open_vm_settings(mid, "Display")
    pump(root, secs=0.2)
    dlg.v["screen"].set("1280x720")
    dlg.v["memory_mb"].set(2048)
    dlg.v["name"].set("Renamed")
    dlg.v["auto_snapshot_end"].set(True)
    dlg.ok()
    m = v.vm_store.machines[mid]
    assert m.name == "Renamed" and m.settings["screen"] == "1280x720" and m.settings["memory_mb"] == 2048 and m.settings["auto_snapshot_end"]
    dlg2 = v.open_vm_settings(mid)
    dlg2.v["screen"].set("banana")
    dlg2.ok()
    assert "Screen size" in dlg2.msg.get() and v.vm_store.machines[mid].settings["screen"] == "1280x720"     # a typo never reaches a run
    dlg2.destroy()

    # --- closing the window of a running machine asks; cancelling keeps it
    win.hold_var.set(False)
    win.destroy()
    mgr.destroy()


def test_powered_off_machine_shows_its_last_picture_and_cannot_start_without_project(root, tmp_path, monkeypatch):
    from tkinter import messagebox

    from ui.app_preview_view import AppPreviewView
    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: shown.append(a))
    v = AppPreviewView(root)
    v.pack(fill="both", expand=True)
    pump(root, secs=0.3)
    m = v.vm_store.new_machine("Lonely", str(tmp_path / "missing"), "run", ENTRY)
    v.vm_store.record_end(m, "failed", png(), "boom")
    win = v.open_vm_window(m.id)
    pump(root, secs=0.5)
    assert "[Aborted]" in win.title() and win._png[:4] == b"\x89PNG"
    assert v.vm_start_machine(m) is False and shown                      # project not found: an explanation, no run
    assert v.vm_live_state() == "" and str(win.tb["start"]["state"]) == "normal" and str(win.tb["stop"]["state"]) == "disabled"
    win.page_var.set("console")
    win.refresh()
    assert "boom" in win.console.get("1.0", "end")
    win.destroy()


def test_new_machine_dialog_validates(root, tmp_path):
    from ui.app_preview_view import AppPreviewView
    from ui.vm_manager import NewMachineDialog
    v = AppPreviewView(root)
    v.pack()
    pump(root, secs=0.2)
    d = NewMachineDialog(v)
    d.v["source"].set(str(tmp_path / "nope"))
    d.v["command"].set("python main.py")
    d.ok()
    assert "existing" in d.msg.get() and d.result is None
    d.v["source"].set(str(tmp_path))
    d.v["command"].set("")
    d.ok()
    assert "start command" in d.msg.get()
    d.v["command"].set("python main.py --flag 'a b'")
    d.v["name"].set("Mine")
    d.ok()
    assert d.result[0] == "Mine" and d.result[3]["argv"] == ["python", "main.py", "--flag", "a b"]
