"""Screen capture of the preview: command fallbacks, back-off and the plain-language diagnosis when no snapshot arrives."""
import threading
import time
import types
from pathlib import Path

from app_preview import session as S

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (800).to_bytes(4, "big") + (600).to_bytes(4, "big") + b"x" * 40


def _sess(monkeypatch, fake, **timing):
    s = object.__new__(S.PreviewSession)
    s.exe, s.name, s.options = "docker", "be-pv-x", types.SimpleNamespace(fps=2.0)
    s._inputs, s._inputs_lock = [], threading.Lock()
    events = []
    s._pv = lambda t, **d: events.append((t, d))
    for k, v in timing.items():
        setattr(s, k, v)
    monkeypatch.setattr(S, "_cli_bytes", fake)
    return s, events


def test_capture_falls_back_to_the_other_command_name_and_remembers_it(monkeypatch):
    calls = []

    def fake(cmd, timeout=10, data=None):
        calls.append(cmd[5])
        if cmd[5] == "import":
            return 127, b"", b"OCI runtime exec failed: exec: \"import\": executable file not found in $PATH"
        return (0, PNG, b"") if cmd[5] == "import-im7.q16" else (127, b"", b"not found")
    s, _ = _sess(monkeypatch, fake)
    st = {"cmd": None}
    assert s._capture(st)[0] == 0 and st["cmd"] == 1
    calls.clear()
    assert s._capture(st)[0] == 0 and len(calls) == 1             # the working command is used directly next time


def test_a_real_tool_failure_does_not_try_every_name(monkeypatch):
    n = []
    s, _ = _sess(monkeypatch, lambda cmd, timeout=10, data=None: (n.append(1), (1, b"", b"Can't open display"))[1])
    s._capture({"cmd": None})
    assert len(n) == 1


def test_frames_reports_why_nothing_shows_and_backs_off(monkeypatch):
    calls = []

    def fake(cmd, timeout=10, data=None):
        calls.append(cmd)
        if "ase_diag.py" in cmd[-1]:
            return 0, b"X sockets: none\nvisible windows: 0\n", b""
        return None, b"", b"timed out after 6 seconds"
    s, events = _sess(monkeypatch, fake, DIAG_AFTER_S=0.3, DIAG_EVERY_S=60.0, CAPTURE_TIMEOUT_S=1)
    stop = threading.Event()
    t = threading.Thread(target=s._frames, args=(stop,), daemon=True)
    t.start()
    time.sleep(2.0)
    stop.set()
    t.join(5)
    texts = [d["info"]["text"] for tp, d in events if tp == "pv_note"]
    assert any("No screen snapshot yet (timed out" in x for x in texts) and any("Screen check:" in x and "visible windows: 0" in x for x in texts)
    assert sum(1 for x in texts if "Screen check" in x) == 1       # once, not every loop
    assert len([c for c in calls if "ase_diag.py" not in c[-1]]) <= 12      # backed off: not hammering the runtime


def test_frames_emits_the_first_frame_fast(monkeypatch):
    s, events = _sess(monkeypatch, lambda cmd, timeout=10, data=None: (0, PNG, b""))
    stop = threading.Event()
    t = threading.Thread(target=s._frames, args=(stop,), daemon=True)
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(5)
    frames = [d for tp, d in events if tp == "pv_frame"]
    assert len(frames) == 1 and frames[0]["w"] == 800 and frames[0]["h"] == 600      # identical frames are not re-sent


def test_image_recipe_checks_screen_capture_and_diag_script_ships():
    root = Path(S.__file__).resolve().parent
    df = (root / "images" / "Dockerfile.gui").read_text()
    assert "import -silent -window root png:/tmp/ase-check.png" in df and "xdotool" in df
    assert (root / "runtime" / "ase_diag.py").is_file()
