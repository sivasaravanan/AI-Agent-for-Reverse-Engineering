"""Screen wiring: size/rate/view options, external (pop-out) window, snapshots, Live Run thumbnail fed by App Preview, honest size message."""
import struct
import sys
import time
import tkinter as tk
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import screen_tools as st  # noqa: E402


def png(w=800, h=600, rgb=(30, 120, 200)) -> bytes:
    """A real, tiny solid-colour PNG (no Pillow needed)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


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
    st.SNAPSHOTS.set_default(tmp_path / "snaps")
    st.SNAPSHOTS.set_custom("")
    yield tmp_path / "snaps"
    st.SNAPSHOTS.default, st.SNAPSHOTS.custom = old


def pump(root, cond=lambda: False, secs=5.0):
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


# ------------------------------------------------------------------------------------------------ pure helpers
def test_size_rate_and_scale_helpers():
    assert st.parse_size("1024x768") == (1024, 768) and st.parse_size(" 1280 X 720 ") == (1280, 720) and st.parse_size("1920×1080") == (1920, 1080)
    assert st.parse_size("100x100") is None and st.parse_size("9999x9999") is None and st.parse_size("big") is None and st.parse_size("") is None
    assert st.size_text(800, 600) == "800x600"
    assert st.parse_fps("5 fps") == 5.0 and st.parse_fps("0.5 fps") == 0.5 and st.parse_fps("99") == 10.0 and st.parse_fps("x", 2.0) == 2.0
    assert st.fps_text(2.0) == "2 fps" and st.view_label("100") == "100%" and st.view_label("nonsense") == "Fit"
    assert st.choose_scale(800, 600, 1000, 700, "fit") == (1, 1)
    assert st.choose_scale(800, 600, 400, 300, "fit") == (1, 2)
    assert st.choose_scale(800, 600, 1000, 700, "50") == (1, 2) and st.choose_scale(800, 600, 100, 100, "100") == (1, 1)
    assert st.choose_scale(800, 600, 100, 100, "200") == (2, 1) and st.choose_scale(3000, 2000, 100, 100, "200") == (1, 1)     # too large to zoom


def test_snapshot_store_names_never_overwrite(snapshots):
    a, b = st.SNAPSHOTS.save(png(10, 10), "run1"), st.SNAPSHOTS.save(png(10, 10), "run1")
    assert a != b and a.parent == snapshots and a.read_bytes()[:4] == b"\x89PNG" and a.name.startswith("preview_run1_")
    custom = snapshots.parent / "mine"
    st.SNAPSHOTS.set_custom(str(custom))
    assert st.SNAPSHOTS.save(png(10, 10)).parent == custom
    with pytest.raises(OSError):
        st.SNAPSHOTS.save(b"")


# ------------------------------------------------------------------------------------------------ frame view
def test_frame_view_scales_centres_and_maps_the_mouse(root):
    fv = st.FrameView(root)
    fv.pack(fill="both", expand=True)
    pump(root)
    fv.show(png(800, 600))
    pump(root)
    w, h, sub, zoom = fv.meta
    assert (w, h) == (800, 600) and (sub, zoom) == (1, 1) and fv.offset[0] > 0                 # fits at 100% and is centred
    ox, oy = fv.offset
    ev = lambda x, y: SimpleNamespace(x=x, y=y)           # noqa: E731
    assert fv.to_xy(ev(ox + 400, oy + 300)) == (400, 300) and fv.to_xy(ev(ox - 5, oy + 10)) is None and fv.to_xy(ev(ox + 800, oy)) is None
    fv.set_mode("50")
    pump(root)
    assert fv.meta[2:] == (2, 1) and fv.to_xy(ev(fv.offset[0] + 100, fv.offset[1] + 100)) == (200, 200)       # half size: mouse still maps exactly
    fv.set_mode("200")
    pump(root)
    assert fv.meta[2:] == (1, 2)
    fv.clear()
    assert fv.to_xy(ev(5, 5)) is None


# ------------------------------------------------------------------------------------------------ App Preview
def make_view(root, **kw):
    from ui.app_preview_view import AppPreviewView
    v = AppPreviewView(root, **kw)
    v.pack(fill="both", expand=True)
    pump(root, secs=0.3)
    return v


def test_screen_options_are_validated_applied_and_remembered(root):
    saved = {}
    v = make_view(root, on_pref=lambda k, x: saved.__setitem__(k, x), prefs={"screen": "1024x768", "fps": 5.0, "view": "100"})
    assert v.options.screen == "1024x768" and v.options.fps == 5.0 and v.view_var.get() == "100%" and v.frame_view.mode == "100"   # restored
    v.res_var.set("1280x720")
    v._size_changed()
    assert v.options.screen == "1280x720" and saved["screen"] == "1280x720" and "next time" in v.screen_note.get()
    v.res_var.set("banana")
    v._size_changed()
    assert v.options.screen == "1280x720" and v.res_var.get() == "1280x720" and "must look like" in v.screen_note.get()   # typo never reaches a run
    v.res_var.set("1366x768")                                                   # typed, not confirmed: starting still uses it
    assert v._preview_options().screen == "1366x768"
    v.rate_var.set("10 fps")
    v._rate_changed()
    assert v.options.fps == 10.0 and saved["fps"] == 10.0
    v.view_var.set("50%")
    v._view_changed()
    assert v.frame_view.mode == "50" and saved["view"] == "50"


def test_frames_are_drawn_once_shared_with_popout_and_saved_as_snapshots(root, snapshots):
    v = make_view(root)
    assert v.pop_btn.winfo_exists() and v.snap_btn.winfo_exists()
    v.pop_out()                                                                 # nothing running, no web page: says so, opens nothing
    assert v._popout is None and "Nothing to show" in v.status_var.get()
    p1, p2 = png(800, 600, (1, 2, 3)), png(800, 600, (9, 9, 9))
    v._frame({"png": p1, "w": 800, "h": 600})
    v._frame({"png": p2, "w": 800, "h": 600})                                  # two frames in one go -> one draw of the newest
    pump(root, lambda: not v._frame_job)
    assert v._frame_png == p2 and v.frame_view.png == p2 and v.info["Screen"].get().startswith("800x600")
    v.pop_out()
    pump(root)
    pw = v._popout
    assert pw is not None and pw.view.png == p2
    v.pop_out()                                                                 # a second click re-uses the window
    assert v._popout is pw
    p3 = png(800, 600, (50, 60, 70))
    v._frame({"png": p3, "w": 800, "h": 600})
    pump(root, lambda: pw.view.png == p3)
    assert pw.view.png == p3
    path = v.quick_snapshot()
    assert path and path.parent == snapshots and path.read_bytes() == p3 and "Snapshot saved" in v.status_var.get()
    pw.close()
    assert v._popout is None
    v._frame_png = b""
    assert v.quick_snapshot(quiet_if_none=True) is None


def test_auto_snapshot_when_the_run_ends(root, snapshots):
    v = make_view(root)
    v._frame({"png": png(100, 100), "w": 100, "h": 100})
    v.auto_snap_var.set(True)
    v._ended({"status": "completed", "exit_code": 0, "duration_s": 1.0})
    assert len(list(snapshots.glob("*.png"))) == 1


def test_status_says_running_once_the_container_runs(root):
    v = make_view(root)
    v._run_title = "[gui] app.py"
    v.session = SimpleNamespace(running=True, plan=SimpleNamespace(display=True))
    v.status_var.set("Starting run in container be-pv-1 (network disabled)...")
    v._pv_state({"state": "running"})
    assert v.status_var.get() == "Running: [gui] app.py"
    v.session = None


def test_session_input_follows_the_screen_size_of_its_own_run():
    from app_preview.models import PreviewOptions
    from app_preview.session import PreviewSession
    opts = PreviewOptions()
    s = PreviewSession(SimpleNamespace(env={"ASE_SCREEN": "1280x720x24"}, display=True, ports=[]), None, opts)
    assert s.screen_size == (1280, 720)
    assert s.input_event("click", x=1200, y=700) and not s.input_event("click", x=1300, y=700)
    opts.screen = "640x480"                                                     # the panel changing later must not move this run's limits
    assert s.screen_size == (1280, 720)
    assert PreviewSession(SimpleNamespace(env={}, display=True, ports=[]), None, opts).screen_size == (640, 480)


# ------------------------------------------------------------------------------------------------ Live Run
def test_live_run_thumbnail_shows_app_preview_frames_and_opens_externally(root, snapshots):
    from app_preview.events import PV
    from plugins.build_execution.events import BUS
    from ui.live_run_view import LiveRunView
    lv = LiveRunView(root)
    lv.pack(fill="both", expand=True)
    rid = "lr" + str(int(time.time() * 1000))[-6:]
    BUS.emit("start", rid, title="[run] dupescope.py", image="ase-preview-gui:latest", network=False, timeout_s=60)
    pump(root, lambda: rid in lv.runs)
    assert lv.card.mode() == "console" and not lv.card.png                       # nothing yet: this is the blank picture the user saw
    frame = png(800, 600, (5, 80, 5))
    PV.emit("pv_frame", rid, png=frame, w=800, h=600)                            # App Preview publishes frames on PV only
    pump(root, lambda: lv.runs[rid].png)
    assert lv.runs[rid].png == frame and lv.card.png == frame and lv.card.mode() == "gui"
    lv._card_click("gui")
    pump(root)
    assert lv._popout is not None and lv._popout.view.png == frame
    lv._snap_quick()
    assert len(list(snapshots.glob("*.png"))) == 1 and "Snapshot saved" in lv.status_var.get()
    PV.emit("pv_web", rid, url="http://127.0.0.1:8000/", up=True, status=200, headers={"Content-Type": "text/html"},
            body="<html><title>Hello</title><body><p>Hi there</p></body></html>")
    pump(root, lambda: lv.runs[rid].web)
    assert lv.runs[rid].web[0] and lv.runs[rid].web[1] == "Hello" and "Hi there" in lv.runs[rid].web[2]
    BUS.emit("end", rid, status="completed", exit_code=0, notes=[], duration_s=1.0)
    PV.emit("pv_frame", rid, png=png(800, 600, (1, 1, 1)), w=800, h=600)         # a late frame must not flip a finished run back to 'running'
    pump(root, secs=0.5)
    assert lv.card.run_state == "ended"
    lv._popout.close()
    assert lv._popout is None


# ------------------------------------------------------------------------------------------------ size message
def test_quick_copy_message_names_the_real_reason(tmp_path, monkeypatch):
    from plugins.build_execution import quickprep
    from plugins.build_execution.models import Limits
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("print(1)\n")
    (proj / "blob.bin").write_bytes(b"\0" * (3 * 1024 * 1024))
    cfg = dict(quickprep._storage.load(), quick_narrow_mb=1, max_file_mb=1, trim_large=False)
    monkeypatch.setattr(quickprep._storage, "load", lambda: cfg)
    notes = quickprep.prepare(str(proj), tmp_path / "d1", Limits(max_workspace_mb=500), entry="a.py")
    assert notes and notes[0].startswith("Project is larger than the quick-copy size of 1 MB (3 MB; the limit is 500 MB)")      # NOT "larger than 500 MB"
    notes = quickprep.prepare(str(proj), tmp_path / "d2", Limits(max_workspace_mb=2), entry="a.py")
    assert notes and notes[0].startswith("Project is larger than 2 MB (3 MB)")                                                 # over the limit: unchanged wording
