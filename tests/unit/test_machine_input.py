"""The machine window really drives the program: guest input events, the in-container helper's validation, the session's fast channel."""
import importlib.util
import io
import json
import os
import threading
import tkinter as tk
import types
from pathlib import Path

import pytest

from app_preview import PreviewOptions, session as S
from ui.guest_input import GuestInput

HELPER = Path(__file__).resolve().parents[2] / "app_preview" / "runtime" / "ase_screen.py"


def _helper():
    spec = importlib.util.spec_from_file_location("ase_screen_under_test", HELPER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ the helper that runs inside the container
def test_helper_builds_only_validated_xdotool_arguments():
    h = _helper()
    assert h.command({"c": "move", "x": 5, "y": 6}) == ["mousemove", "5", "6"]
    assert h.command({"c": "down", "b": 1, "x": 5, "y": 6}) == ["mousemove", "5", "6", "mousedown", "1"]
    assert h.command({"c": "up", "b": 3}) == ["mouseup", "3"]
    assert h.command({"c": "scroll", "d": "up", "n": 2}) == ["click", "--repeat", "2", "4"]
    assert h.command({"c": "keydown", "k": "Shift_L"}) == ["keydown", "--", "Shift_L"]
    assert h.command({"c": "key", "k": "ctrl+c"}) == ["key", "--", "ctrl+c"]
    assert h.command({"c": "key", "k": "a; rm -rf /"}) is None
    assert h.command({"c": "type", "t": "x\x00y"}) is None
    assert h.command({"c": "nonsense"}) is None
    with pytest.raises(ValueError):
        h.command({"c": "move", "x": -1, "y": 0})
    with pytest.raises(ValueError):
        h.command({"c": "down", "b": 9})


# ------------------------------------------------------------------ the session sends over the channel at once, else queues
def _session(chan_ok=True):
    s = object.__new__(S.PreviewSession)
    s.plan = types.SimpleNamespace(display=True, env={"ASE_SCREEN": "800x600x24"})
    s.options = PreviewOptions()
    s._inputs, s._inputs_lock = [], threading.Lock()
    s._chan_lock = threading.Lock()
    sink = io.BytesIO()
    s._chan = types.SimpleNamespace(stdin=sink, poll=lambda: None)
    s._chan_ok = chan_ok
    return s, sink


def test_input_goes_over_the_channel_when_it_is_up():
    s, sink = _session(True)
    assert s.input_event("down", x=10, y=20, button=1) and s.input_event("up", button=1) and s.input_event("keydown", key="a")
    assert s.input_event("scroll", direction="up", n=2) and s.input_event("move", x=3, y=4)
    lines = [json.loads(x) for x in sink.getvalue().decode().splitlines()]
    assert lines[0] == {"c": "down", "b": 1, "x": 10, "y": 20} and lines[1] == {"c": "up", "b": 1} and lines[2] == {"c": "keydown", "k": "a"}
    assert lines[3] == {"c": "scroll", "d": "up", "n": 2} and lines[4] == {"c": "move", "x": 3, "y": 4}
    assert s._inputs == []


def test_input_is_validated_and_queued_when_there_is_no_channel():
    s, sink = _session(False)
    assert not s.input_event("click", x=900, y=10) and not s.input_event("keydown", key="a b") and not s.input_event("bogus")
    assert not s.input_event("type", text="\x00")
    assert s.input_event("click", x=10, y=10) and s.input_event("keydown", key="Return")
    assert [k for k, _a in s._inputs] == ["click", "keydown"]
    assert s.input_event("move", x=1, y=1) and len(s._inputs) == 2            # a pointer move is never queued for the slow path
    assert sink.getvalue() == b""


def test_no_display_means_no_input():
    s, _ = _session(True)
    s.plan.display = False
    assert not s.input_event("click", x=1, y=1)


# ------------------------------------------------------------------ the window side
class _Rec:
    def __init__(self):
        self.calls = []

    def input_event(self, kind, **kw):
        self.calls.append((kind, kw))
        return True


@pytest.fixture()
def canvas():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    c = tk.Canvas(r, width=200, height=200)
    c.pack()
    r.update()
    yield r, c
    r.destroy()


def _ev(**kw):
    return types.SimpleNamespace(**{"x": 1, "y": 1, "state": 0, "delta": 0, "keysym": "a", **kw})


def test_guest_input_sends_press_move_release_keys_and_wheel(canvas):
    r, c = canvas
    rec, on = _Rec(), {"v": True}
    g = GuestInput(c, lambda e: (e.x * 2, e.y * 2), lambda: rec, lambda: on["v"])
    assert g.press(_ev(x=5, y=6), 1) == "break"
    g.motion(_ev(x=7, y=8))
    g.motion(_ev(x=9, y=10))                                                  # throttled: only the last position is sent
    r.update()
    import time
    time.sleep(0.06)
    r.update()
    assert g.release(_ev(x=9, y=10), 1) == "break"
    kinds = [k for k, _kw in rec.calls]
    assert kinds[0] == "down" and rec.calls[0][1] == {"x": 10, "y": 12, "button": 1}
    assert kinds.count("move") == 2 and rec.calls[1][1] == {"x": 18, "y": 20}      # the throttled move, then the move before the release
    assert kinds[-1] == "up"
    g.wheel(_ev(delta=120))
    assert rec.calls[-1] == ("scroll", {"direction": "up", "n": 1})
    g.key_down(_ev(keysym="Shift_L"))
    g.key_down(_ev(keysym="A"))
    g.key_up(_ev(keysym="A"))
    assert [c_ for c_ in rec.calls[-3:]] == [("keydown", {"key": "Shift_L"}), ("keydown", {"key": "A"}), ("keyup", {"key": "A"})]
    g.release_all()                                                           # Shift_L still held: released when the window loses focus
    assert rec.calls[-1] == ("keyup", {"key": "Shift_L"})


def test_guest_input_is_silent_when_not_captured_and_keeps_window_shortcuts(canvas):
    r, c = canvas
    rec, on = _Rec(), {"v": False}
    g = GuestInput(c, lambda e: (1, 1), lambda: rec, lambda: on["v"], host_combo=lambda e: e.keysym == "t")
    assert g.press(_ev(), 1) is None and g.key_down(_ev()) is None and rec.calls == []
    on["v"] = True
    assert g.key_down(_ev(keysym="F11")) is None and g.key_down(_ev(keysym="t")) is None and rec.calls == []
    assert g.key_down(_ev(keysym="bad name")) is None
    assert g.key_down(_ev(keysym="a")) == "break" and rec.calls == [("keydown", {"key": "a"})]


# ------------------------------------------------------------------ real container (opt-in): click + typing reach the program
@pytest.mark.skipif(os.environ.get("ASE_REAL_CONTAINER_TESTS") != "1", reason="set ASE_REAL_CONTAINER_TESTS=1 with Docker and the GUI preview image")
def test_real_container_click_and_typing_reach_the_program(tmp_path):
    import re
    import time
    from app_preview import approve, custom_entry, gui_image_ready, make_plan, start
    from app_preview.runtimes import status
    from plugins.build_execution.events import BUS
    opts = PreviewOptions()
    if not (status(opts)["available"] and gui_image_ready(opts)["ready"]):
        pytest.skip("no usable runtime or GUI preview image")
    (tmp_path / "app.py").write_text(
        "import tkinter as tk\nr=tk.Tk(); r.geometry('320x140+60+60')\ne=tk.Entry(r); e.place(x=20,y=20)\n"
        "b=tk.Button(r,text='Send',command=lambda: print('GOT:'+e.get(),flush=True)); b.place(x=20,y=70)\nr.update()\n"
        "print('POS %d %d %d %d'%(e.winfo_rootx()+10,e.winfo_rooty()+5,b.winfo_rootx()+10,b.winfo_rooty()+5),flush=True)\nr.mainloop()\n")
    plan = make_plan(str(tmp_path), custom_entry(["python", "app.py"], kind="gui", title="form"), "run", opts, link_output=False)
    out = []
    BUS.subscribe(lambda e: out.append(e["text"]) if e.get("type") == "out" else None)
    s = start(plan, approve(plan), opts)
    try:
        t = time.time()
        while not (s._chan_ok and "POS" in "".join(out)) and time.time() - t < 40:
            time.sleep(0.2)
        assert s._chan_ok, "the fast screen channel did not start"
        ex, ey, bx, by = map(int, re.search(r"POS (\d+) (\d+) (\d+) (\d+)", "".join(out)).groups())
        s.input_event("down", x=ex, y=ey, button=1)
        s.input_event("up", button=1)
        time.sleep(0.5)
        for k in ("o", "k"):
            s.input_event("keydown", key=k)
            s.input_event("keyup", key=k)
        time.sleep(0.3)
        s.input_event("down", x=bx, y=by, button=1)
        s.input_event("up", button=1)
        t = time.time()
        while "GOT:" not in "".join(out) and time.time() - t < 10:
            time.sleep(0.1)
        assert "GOT:ok" in "".join(out)
    finally:
        s.stop()
        s.wait(30)
