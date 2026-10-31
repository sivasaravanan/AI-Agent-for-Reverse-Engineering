"""Auto-start of Docker/Podman and the embedded live run preview."""
import json
import stat
import sys
import threading
import time

import pytest

from plugins.build_execution import Approval, Limits, Options, build_plan, custom_candidate, run_plan
from plugins.build_execution import runtime_start
from plugins.build_execution.events import BUS
from plugins.build_execution.runtime_start import ensure_runtime, start_actions
from tests.unit.test_build_execution_plugin import FAKE, make_zip


@pytest.fixture
def fake(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "docker"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (d / "behavior.json").write_text(json.dumps({"info_fail": True}))
    # a "start" action that makes the fake daemon come up (stands in for systemctl / Docker Desktop)
    starter = d / "start.py"
    starter.write_text(f"import json\njson.dump({{}}, open({str(d / 'behavior.json')!r}, 'w'))\n")

    class F:
        opts = Options(tool_paths={"docker": str(exe)}, backend="docker")
        action = {"label": "fake start", "argv": [sys.executable, str(starter)], "detach": False, "timeout": 20}
        bad_action = {"label": "broken start", "argv": [sys.executable, "-c", "import sys; sys.exit(1)"], "detach": False, "timeout": 20}

        @staticmethod
        def calls():
            p = d / "calls.jsonl"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return F


# ---------------------------------------------------------------- auto start
def test_start_actions_per_platform_are_fixed_commands():
    assert start_actions("docker", "docker", "Darwin")[0]["argv"][:2] == ["open", "-g"]
    win = start_actions("docker", "docker.exe", "Windows")
    assert win[0]["argv"] == ["docker.exe", "desktop", "start"]
    lin = start_actions("docker", "docker", "Linux")
    assert lin[0]["argv"] == ["systemctl", "--user", "start", "docker"]
    assert start_actions("podman", "podman", "Windows")[0]["argv"] == ["podman", "machine", "start"]
    for system in ("Windows", "Darwin", "Linux"):
        for a in start_actions("docker", "docker", system) + start_actions("podman", "podman", system):
            assert isinstance(a["argv"], list) and all(isinstance(x, str) for x in a["argv"])      # no shell strings


def test_ensure_runtime_starts_stopped_daemon_and_reports_progress(fake):
    msgs = []
    st = ensure_runtime(fake.opts, progress=msgs.append, actions=[fake.action], wait_s=20, poll_s=0.05)
    assert st["available"] and st["started"] and st["attempts"] == ["fake start: launched"]
    assert any("starting it" in m for m in msgs) and any("ready" in m for m in msgs)


def test_ensure_runtime_does_nothing_when_already_running(fake):
    ensure_runtime(fake.opts, actions=[fake.action], wait_s=5, poll_s=0.05)        # now running
    msgs = []
    st = ensure_runtime(fake.opts, progress=msgs.append, actions=[fake.bad_action])
    assert st["available"] and not st["started"] and msgs == []


def test_ensure_runtime_failure_gives_manual_hint_and_never_claims_success(fake):
    st = ensure_runtime(fake.opts, actions=[fake.bad_action], wait_s=1, poll_s=0.05, system="Linux")
    assert not st["available"] and "Automatic start did not work" in st["reason"] and "systemctl" in st["hint"]


def test_ensure_runtime_not_installed_is_not_installed(monkeypatch):
    monkeypatch.setattr("plugins.build_execution.sandbox.shutil.which", lambda n: None)
    st = ensure_runtime(Options(), actions=[{"label": "x", "argv": ["false"], "detach": False}])
    assert not st["available"] and st["attempts"] == [] and "No docker/podman" in st["reason"]


def test_ensure_runtime_cancel_stops_waiting(fake):
    ev = threading.Event()
    stuck = {"label": "launch only", "argv": [sys.executable, "-c", "pass"], "detach": False, "timeout": 5}
    t0 = time.monotonic()
    threading.Timer(0.3, ev.set).start()
    st = ensure_runtime(fake.opts, actions=[stuck], cancel=ev, wait_s=60, poll_s=0.1)
    assert not st["available"] and time.monotonic() - t0 < 10


def test_podman_machine_missing_is_reported_not_created(monkeypatch, fake):
    monkeypatch.setattr(runtime_start, "_podman_machine_missing", lambda exe: True)
    monkeypatch.setattr(runtime_start, "isolation_status", lambda o=None: {"available": False, "backend": "podman", "executable": "podman", "version": "", "reason": "down"})
    st = ensure_runtime(Options(), system="Windows", actions=[fake.action])
    assert "podman machine init" in st["hint"] and st["attempts"] == ["podman machine list: none"]


def _plan(tmp_path, fake):
    proj = make_zip(tmp_path / "p.zip", {"app/package.json": '{"scripts":{"test":"node t.js"}}', "app/t.js": "console.log(1)"})
    return build_plan(proj, custom_candidate(["node", "t.js"], "node"), fake.opts)


def test_run_without_auto_start_is_still_skipped(tmp_path, fake):
    res = run_plan(_plan(tmp_path, fake), Approval.for_plan(_plan(tmp_path, fake)), fake.opts)
    assert res.status == "skipped" and not any(c["args"][0] == "run" for c in fake.calls())


def test_run_with_auto_start_starts_runtime_then_runs(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(runtime_start, "start_actions", lambda b, e, s=None: [fake.action])
    opts = Options(tool_paths=fake.opts.tool_paths, backend="docker", auto_start=True)
    plan = _plan(tmp_path, fake)
    res = run_plan(plan, Approval.for_plan(plan), opts)
    assert res.status == "completed" and any("started automatically" in n for n in res.notes)


# ---------------------------------------------------------------- live events
def test_run_publishes_start_phase_output_and_end(tmp_path, fake):
    ok = Options(tool_paths=fake.opts.tool_paths, backend="docker")
    (tmp_path / "fakebin" / "behavior.json").write_text("{}")
    got = []
    unsub = BUS.subscribe(got.append)
    try:
        plan = _plan(tmp_path, fake)
        res = run_plan(plan, Approval.for_plan(plan), ok)
    finally:
        unsub()
    kinds = [e["type"] for e in got]
    assert kinds[0] == "start" and kinds[-1] == "end" and "phase" in kinds
    assert "hello from container" in "".join(e["text"] for e in got if e["type"] == "out")
    assert len({e["run"] for e in got}) == 1 and got[-1]["status"] == res.status == "completed"
    assert BUS.request_cancel(got[0]["run"]) is False                      # finished runs are no longer cancellable


def test_stop_button_path_cancels_a_running_container(tmp_path, fake):
    (tmp_path / "fakebin" / "behavior.json").write_text(json.dumps({"run": "sleep"}))
    ok = Options(tool_paths=fake.opts.tool_paths, backend="docker")
    got = []
    unsub = BUS.subscribe(got.append)
    out = {}
    plan = _plan(tmp_path, fake)
    t = threading.Thread(target=lambda: out.setdefault("r", run_plan(plan, Approval.for_plan(plan), ok)))
    t.start()
    try:
        for _ in range(100):
            if any(e["type"] == "phase" and "Running in container" in e["text"] for e in got):
                break
            time.sleep(0.05)
        assert BUS.request_cancel(got[0]["run"]) is True
        t.join(20)
    finally:
        unsub()
    assert out["r"].status == "cancelled"


def test_watch_container_publishes_stats_and_process_list(monkeypatch):
    from plugins.build_execution import sandbox
    seen = []

    def cli(cmd, timeout=20):
        if "stats" in cmd:
            return 0, "12.5%|34.2MiB / 1GiB|3\n", ""
        return 0, "PID   USER  COMMAND\n1  root  node t.js\n7  root  sleep 1\n", ""
    monkeypatch.setattr(sandbox, "_cli", cli)
    unsub = BUS.subscribe(seen.append)
    stop = threading.Event()
    th = threading.Thread(target=sandbox._watch_container, args=("docker", "c1", "r1", stop, 0.05))
    th.start()
    time.sleep(0.4)
    stop.set(); th.join(3); unsub()
    st = [e for e in seen if e["type"] == "stats"][0]
    assert st["cpu"] == 12.5 and abs(st["mem_mib"] - 34.2) < 0.01 and st["pids"] == "3"
    top = [e for e in seen if e["type"] == "top"][0]
    assert top["header"] == ["PID", "USER", "COMMAND"] and top["rows"][0] == ["1", "root", "node t.js"]


# ---------------------------------------------------------------- embedded view
def test_live_view_shows_a_run_as_it_happens_and_switches_to_itself(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from ui.main_window import MainWindow
    try:
        win = MainWindow(root, tmp_path / "r", base_dir=tmp_path)
        lv = win.live_view

        def pump(n=12):
            for _ in range(n):
                root.update(); time.sleep(0.03)
        win._select_tab("Overview"); pump()
        BUS.emit("start", "run-A", title="python -m pytest -q", argv=["python"], image="python:3.12-slim", network=False, timeout_s=300)
        BUS.emit("phase", "run-A", text="Running in container be-123")
        BUS.emit("out", "run-A", text="collected 3 items\n")
        BUS.emit("err", "run-A", text="warning: slow test\n")
        BUS.emit("stats", "run-A", cpu=40.0, mem_mib=120.0, mem_text="120MiB / 1GiB", pids="4")
        BUS.emit("stats", "run-A", cpu=60.0, mem_mib=130.0, mem_text="130MiB / 1GiB", pids="4")
        BUS.emit("top", "run-A", header=["PID", "COMMAND"], rows=[["1", "python -m pytest"]])
        pump(20)
        assert win.nb.tab(win.nb.select(), "text") == "Live Run"                  # switched to the preview automatically
        text = lv.text.get("1.0", "end")
        assert "collected 3 items" in text and "warning: slow test" in text and "Running in container" in text
        assert "err" in lv.text.tag_names("2.0") or "err" in lv.text.tag_names("3.0")
        assert lv.chip.cget("text") == "● Running" and str(lv.stop_btn["state"]) == "normal"
        assert "CPU 60.0%" in lv.stats_var.get() and lv.runs["run-A"].cpu == [40.0, 60.0]
        assert [lv.procs.item(i, "values") for i in lv.procs.get_children()] == [("1", "python -m pytest")]
        BUS.emit("end", "run-A", status="failed", exit_code=3, notes=[], duration_s=1.2)
        pump(15)
        assert lv.chip.cget("text") == "✖ Failed" and str(lv.stop_btn["state"]) == "disabled"
        # a second run; the first stays reviewable from the run picker
        BUS.emit("start", "run-B", title="node t.js", argv=["node"], image="node:20", network=False, timeout_s=60)
        BUS.emit("out", "run-B", text="second output\n")
        pump(15)
        assert "second output" in lv.text.get("1.0", "end") and "collected 3 items" not in lv.text.get("1.0", "end")
        lv.pick.current(0); lv._on_pick(); pump()
        assert "collected 3 items" in lv.text.get("1.0", "end")
        lv.find_var.set("slow"); lv._highlight()
        assert lv.text.tag_ranges("hit")
        # switching can be turned off
        lv.follow_var.set(False); win._select_tab("Overview")
        BUS.emit("start", "run-C", title="x", argv=["x"], image="i", network=False, timeout_s=1)
        pump(15)
        assert win.nb.tab(win.nb.select(), "text") == "Overview"
    finally:
        root.destroy()


def test_build_tab_has_auto_start_option_and_remembers_it(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from ui.main_window import MainWindow
    try:
        win = MainWindow(root, tmp_path / "r", base_dir=tmp_path)
        assert win.exec_tab.auto_start.get() is True                                # on by default
        win.exec_tab.auto_start.set(False)
        from ui import theme
        assert theme.load_prefs(tmp_path)["auto_start_runtime"] is False
    finally:
        root.destroy()
