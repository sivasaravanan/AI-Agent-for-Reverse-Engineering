"""Setup doctor: detection, install catalog safety, execution, CLI and dialog (no Docker or internet needed)."""
import sys
import threading
import time
from pathlib import Path

import pytest

import setup_doctor.installer as inst
from setup_doctor import Action, ActionResult, apply_known_paths, run_actions, run_checks, summarize, validate_action
from setup_doctor import catalog as cat
from setup_doctor.checks import find_runtime_exe, known_paths

PF = r"C:\Program Files"


class FakeEnv:
    platform = "win32"
    frozen = False
    in_venv = False
    python = r"C:\py\python.exe"

    def glob(self, pattern):
        import fnmatch
        return sorted(p for p in self._exists if fnmatch.fnmatch(p.replace("\\", "/").lower(), pattern.replace("\\", "/").lower()))

    def __init__(self, which=None, exists=(), runs=None):
        self._which = dict(which or {})
        self._exists = set(exists)
        self._runs = runs or {}
        self.path_added = []
        self.calls = []

    def which(self, name):
        return self._which.get(name)

    def exists(self, path):
        return path in self._exists

    def getenv(self, name, default=""):
        return PF if name == "ProgramFiles" else default

    def add_to_path(self, folder):
        self.path_added.append(folder)
        self._which.setdefault("docker", str(Path(folder) / "docker.exe"))

    def run(self, argv, timeout=20):
        self.calls.append(list(argv))
        key = " ".join(argv[1:])
        for pat, val in self._runs.items():
            if pat in key:
                return val() if callable(val) else val
        return 1, "", "not found"


def ids(items, status=None):
    return [i.id for i in items if status is None or i.status == status]


# ----------------------------------------------------------------------------- detection
def test_missing_runtime_offers_winget_docker_and_podman_alternative():
    items = run_checks(FakeEnv(which={"winget": "winget"}), include_optional=False)
    rt = next(i for i in items if i.id == "runtime")
    assert rt.status == "missing" and rt.required
    assert "Docker.DockerDesktop" in rt.actions[0].display and rt.actions[0].admin
    alt = next(i for i in items if i.id == "runtime-alt")
    assert "RedHat.Podman" in alt.actions[0].display
    assert summarize(items)["missing_required"] == ["runtime"]


def test_no_winget_means_manual_download_page_not_a_guess():
    rt = next(i for i in run_checks(FakeEnv(), include_optional=False) if i.id == "runtime")
    assert rt.actions == [] and rt.url.startswith("https://") and "winget" in rt.detail


def test_installed_but_not_on_path_is_found_and_added_to_path():
    dexe = known_paths(FakeEnv())["docker"][0]
    env = FakeEnv(exists={dexe}, runs={"info": (0, "27.0.1", "")})
    assert find_runtime_exe(env) == ("docker", dexe, False)
    assert apply_known_paths(env) == [str(Path(dexe).parent)] and env.path_added
    assert apply_known_paths(env) == []                       # already visible now: nothing more to add


def test_daemon_down_offers_to_start_docker_desktop():
    kp = known_paths(FakeEnv())
    env = FakeEnv(which={"docker": "docker"}, exists={kp["docker_desktop"][0]}, runs={"info": (1, "", "cannot connect")})
    d = next(i for i in run_checks(env, include_optional=False) if i.id == "daemon")
    assert d.status == "missing" and d.actions[0].kind == "launch" and d.actions[0].wait_for_daemon


def test_images_required_python_optional_others_and_present_ones_ok():
    env = FakeEnv(which={"docker": "docker"}, runs={"info": (0, "27", ""), "image inspect node:20-slim": (0, "", "")})
    items = {i.id: i for i in run_checks(env, include_optional=False)}
    assert items["image:python"].status == "missing" and items["image:python"].required
    assert items["image:python"].actions[0].argv[1:] == ("pull", "python:3.12-slim")
    assert items["image:node"].status == "ok"
    assert items["image:dotnet"].status == "optional" and not items["image:dotnet"].required


def test_podman_pull_uses_a_fully_qualified_name():
    assert cat.qualify_image("podman", "python:3.12-slim") == "docker.io/library/python:3.12-slim"
    assert cat.qualify_image("podman", "mcr.microsoft.com/dotnet/sdk:8.0") == "mcr.microsoft.com/dotnet/sdk:8.0"
    assert cat.qualify_image("docker", "python:3.12-slim") == "python:3.12-slim"


def test_frozen_exe_does_not_offer_pip_installs():
    env = FakeEnv(which={"docker": "docker"})
    env.frozen = True
    items = {i.id: i for i in run_checks(env)}
    assert items["tool:ruff"].status == "info" and items["tool:ruff"].actions == []


# ----------------------------------------------------------------------------- safety of what may run
def test_every_catalog_action_passes_validation():
    env = FakeEnv(which={"winget": "winget", "docker": "docker", "podman": "podman"},
                  runs={"info": (0, "1", "")})
    for item in run_checks(env):
        for a in item.actions:
            validate_action(a, env.python)


@pytest.mark.parametrize("argv", [
    ("cmd", "/c", "del", "*"), ("powershell", "-c", "iwr x"), ("winget", "install", "-e", "--id", "Evil.Tool",
                                                              "--accept-package-agreements", "--accept-source-agreements"),
    ("winget", "uninstall", "Docker.DockerDesktop"), (r"C:\py\python.exe", "-m", "pip", "install", "--user", "requests"),
    (r"C:\py\python.exe", "-c", "print(1)"), ("docker", "run", "--privileged", "x"), ("docker", "pull", "--all-tags"),
    ("docker", "pull", "x;rm -rf /"), ("docker", "load", "-i", "missing.tar"), ("brew", "install", "evil"),
    ("podman", "machine", "rm"), (),
])
def test_validation_rejects_anything_outside_the_catalog(argv):
    with pytest.raises(ValueError):
        validate_action(Action(id="x", label="x", argv=tuple(argv)), r"C:\py\python.exe")


def test_load_action_requires_an_existing_archive(tmp_path):
    t = tmp_path / "imgs.tar"
    t.write_bytes(b"x")
    validate_action(cat.load_image_action("docker", str(t)))
    with pytest.raises(ValueError):
        validate_action(cat.load_image_action("docker", str(tmp_path / "evil.txt")))


# ----------------------------------------------------------------------------- venv, java and tools found after install
def test_pip_in_a_virtualenv_has_no_user_flag_and_still_validates():
    venv = FakeEnv(which={"docker": "docker"})
    venv.in_venv = True
    a = next(i for i in run_checks(venv) if i.id == "tool:ruff").actions[0]
    assert "--user" not in a.argv and a.argv[-1] == "ruff" and a.argv[1:4] == ("-m", "pip", "install")
    validate_action(a, venv.python)
    plain = next(i for i in run_checks(FakeEnv(which={"docker": "docker"})) if i.id == "tool:ruff").actions[0]
    assert "--user" in plain.argv
    validate_action(plain, FakeEnv.python)


def test_pip_validation_stays_strict():
    py = FakeEnv.python
    for argv in [(py, "-m", "pip", "install", "--user", "evil"), (py, "-m", "pip", "install", "ruff", "evil"),
                 (py, "-m", "pip", "install", "--index-url", "http://x", "ruff"), (py, "-m", "pip", "install")]:
        with pytest.raises(ValueError):
            validate_action(Action(id="x", label="x", argv=argv), py)


def test_jdk_and_scripts_installed_in_this_session_are_recognised_without_restart():
    jdk = r"C:\Program Files\Eclipse Adoptium\jdk-17.0.12.7-hotspot\bin\javac.exe"
    ruff = "/py/ruff.exe"
    env = FakeEnv(which={"winget": "winget", "docker": "docker"}, exists={jdk, ruff})
    env.python = "/py/python.exe"                    # forward slashes so pathlib behaves the same on any OS running the tests
    items = {i.id: i for i in run_checks(env)}
    assert items["tool:jdk"].status == "ok" and items["tool:ruff"].status == "ok"
    assert items["tool:pyright"].status == "optional"
    added = apply_known_paths(env)
    assert str(Path(jdk).parent) in added and str(Path(ruff).parent) in added


# ----------------------------------------------------------------------------- execution
def _py_action(code, **kw):
    return Action(id="t", label="test step", argv=(sys.executable, "-c", code), **kw)


@pytest.fixture
def allow_python(monkeypatch):
    monkeypatch.setattr(inst, "validate_action", lambda a, py=None: None)


def test_unvalidated_action_is_refused_and_nothing_runs():
    out = []
    res = run_actions([Action(id="bad", label="bad", argv=("cmd", "/c", "echo hi"))], FakeEnv(), progress=out.append)
    assert not res[0].ok and "Refused" in res[0].message and any("REFUSED" in m for m in out)


def test_streams_output_and_stops_at_first_failure(allow_python):
    out = []
    acts = [_py_action("print('hello')"), _py_action("import sys; print('boom'); sys.exit(3)"), _py_action("print('never')")]
    res = run_actions(acts, FakeEnv(), progress=out.append)
    assert [r.ok for r in res] == [True, False] and res[1].returncode == 3
    assert "hello" in out and "boom" in out and "never" not in out


def test_cancel_and_timeout_kill_the_process(allow_python):
    ev = threading.Event()
    threading.Timer(0.6, ev.set).start()
    t0 = time.time()
    res = run_actions([_py_action("import time; time.sleep(30)")], FakeEnv(), cancel=ev)
    assert not res[0].ok and time.time() - t0 < 10
    res = run_actions([_py_action("import time; time.sleep(30)")], FakeEnv(), timeout_s=1)
    assert not res[0].ok and "Timed out" in res[0].message


def test_after_install_the_new_tool_is_put_on_path(allow_python):
    dexe = known_paths(FakeEnv())["docker"][0]
    env = FakeEnv(exists={dexe})
    res = run_actions([_py_action("pass", sets_path=True)], env)
    assert res[0].ok and env.path_added == [str(Path(dexe).parent)]


def test_waits_for_runtime_after_start_then_succeeds(allow_python, monkeypatch):
    monkeypatch.setattr(inst.time, "sleep", lambda s: None)
    state = {"n": 0}

    def info():
        state["n"] += 1
        return (0, "27", "") if state["n"] >= 3 else (1, "", "starting")
    env = FakeEnv(which={"docker": "docker"}, runs={"info": info})
    res = run_actions([_py_action("pass", wait_for_daemon=True)], env, daemon_wait_s=60)
    assert res[0].ok and state["n"] == 3
    env2 = FakeEnv(which={"docker": "docker"}, runs={"info": (1, "", "down")})
    res2 = run_actions([_py_action("pass", wait_for_daemon=True)], env2, daemon_wait_s=0)
    assert not res2[0].ok and "Re-check" in res2[0].message


# ----------------------------------------------------------------------------- CLI
def test_cli_check_and_install_prompts(monkeypatch, capsys):
    import app
    import setup_doctor as sd
    from setup_doctor.catalog import Item
    act = cat.winget_action("docker", "Install Docker Desktop", 600)
    monkeypatch.setattr(sd, "run_checks", lambda *a, **k: [Item("runtime", "Runtime", "missing", "none", True, [act])])
    assert app.run_setup(False, False) == 1
    ran = []
    monkeypatch.setattr(sd, "run_actions", lambda acts, **k: ran.append(acts) or [sd.ActionResult("install:docker", True)])
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert app.run_setup(True, False) == 1 and ran == []                  # declined: nothing runs
    assert "winget install" in capsys.readouterr().out                      # the exact command was shown first
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert app.run_setup(True, False) == 0 and len(ran) == 1
    ran.clear()
    assert app.run_setup(True, True) == 0 and len(ran) == 1


# ----------------------------------------------------------------------------- UI
tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def _pump(root, cond, timeout=10):
    end = time.time() + timeout
    while not cond():
        root.update()
        time.sleep(0.02)
        assert time.time() < end, "timed out"
    root.update()


def test_dialog_lists_items_shows_exact_commands_and_confirms_before_running(monkeypatch, tmp_path):
    import ui.setup_dialog as sdlg
    root = _root()
    try:
        env = FakeEnv(which={"winget": "winget"})
        dlg = sdlg.SetupDialog(root, env=env, state_file=tmp_path / "s.json")
        _pump(root, lambda: dlg.items)
        plan = dlg.plan.get("1.0", "end")
        assert "winget install -e --id Docker.DockerDesktop" in plan                # required item is pre-ticked and shown
        ran = []
        monkeypatch.setattr(sdlg, "run_actions", lambda acts, env=None, **k: ran.append(list(acts)) or [])
        monkeypatch.setattr(sdlg.messagebox, "askyesno", lambda *a, **k: False)
        dlg._install()
        root.update()
        assert ran == []                                                             # declined: nothing runs
        monkeypatch.setattr(sdlg.messagebox, "askyesno", lambda *a, **k: True)
        dlg._install()
        _pump(root, lambda: ran)
        assert ran[0][0].id == "install:docker"
        dlg.dismiss_var.set(True)
        dlg._close()
        assert sdlg.load_state(tmp_path / "s.json")["dismissed"] is True
    finally:
        root.destroy()


def test_one_failed_item_does_not_stop_the_other_selected_items(monkeypatch, tmp_path):
    import ui.setup_dialog as sdlg
    root = _root()
    try:
        env = FakeEnv(which={"winget": "winget", "docker": "docker"})
        env.in_venv = True
        dlg = sdlg.SetupDialog(root, env=env, state_file=tmp_path / "s.json")
        _pump(root, lambda: dlg.items)
        for k in ("tool:ruff", "tool:jdk"):
            dlg._vars[k].set(True)
        chains = []

        def fake_run(acts, env=None, **k):
            chains.append([a.id for a in acts])
            return [ActionResult(acts[0].id, acts[0].id != "pip:ruff", 1, message="Exit code 1.")]
        monkeypatch.setattr(sdlg, "run_actions", fake_run)
        monkeypatch.setattr(sdlg.messagebox, "askyesno", lambda *a, **k: True)
        dlg._install()
        _pump(root, lambda: len(chains) == 2)
        assert ["pip:ruff"] in chains and ["install:jdk"] in chains            # Java still ran after ruff failed
        _pump(root, lambda: "Finished with problems" in dlg.log.get("1.0", "end"))
    finally:
        root.destroy()


def test_startup_check_only_prompts_when_required_item_missing_and_not_dismissed(monkeypatch, tmp_path):
    import ui.setup_dialog as sdlg
    root = _root()
    try:
        opened = []
        monkeypatch.setattr(sdlg, "open_setup", lambda parent, **k: opened.append(1))
        sdlg.schedule_startup_check(root, delay_ms=10, env=FakeEnv(which={"winget": "winget"}), state_file=tmp_path / "a.json")
        _pump(root, lambda: opened)
        opened.clear()
        sdlg.save_state({"dismissed": True}, tmp_path / "b.json")
        sdlg.schedule_startup_check(root, delay_ms=10, env=FakeEnv(), state_file=tmp_path / "b.json")
        end = time.time() + 1.2
        while time.time() < end:
            root.update()
            time.sleep(0.02)
        assert opened == []
        ok_env = FakeEnv(which={"docker": "docker"}, runs={"info": (0, "1", ""), "image inspect": (0, "", "")})
        sdlg.schedule_startup_check(root, delay_ms=10, env=ok_env, state_file=tmp_path / "c.json")
        end = time.time() + 1.2
        while time.time() < end:
            root.update()
            time.sleep(0.02)
        assert opened == []                                                          # everything present: no popup
    finally:
        root.destroy()


def test_build_tab_has_setup_button(tmp_path):
    from ui.execution_dialog import ExecutionTab
    root = _root()
    try:
        tab = ExecutionTab(root)

        def texts(w):
            out = []
            for c in w.winfo_children():
                try:
                    out.append(c.cget("text"))
                except tk.TclError:
                    pass
                out += texts(c)
            return out
        assert "Set up Docker..." in texts(tab) and "Re-check" in texts(tab) and "Discover commands" in texts(tab)
    finally:
        root.destroy()
