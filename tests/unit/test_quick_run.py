"""'⚡ Run now': entry choice, package detection, cached package image, single approval, automatic remedies."""
import os
import stat
import types

import pytest

from app_preview import approve, build_gui_image
from app_preview import quick
from app_preview.models import Entry, PreviewOptions
from plugins.build_execution.models import Limits


def _project(tmp_path):
    (tmp_path / "app.py").write_text("import os, sys\nimport cv2\nimport numpy as np\nimport yaml\nimport helper\n"
                                     "import tkinter\nimport win32api\nfrom pkg import thing\n")
    (tmp_path / "helper.py").write_text("import requests\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "thing.py").write_text("")
    (tmp_path / "unrelated.py").write_text("import pandas\n")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4  # pinned\npandas\n")
    return tmp_path


def test_third_party_uses_the_code_closure_and_requirements_pins(tmp_path):
    got = quick.third_party(_project(tmp_path), "./app.py")
    assert set(got) == {"opencv-python-headless", "numpy==1.26.4", "pyyaml", "requests"}     # not pandas, os, tkinter, helper, win32api
    assert quick.third_party(tmp_path, "run.js") == []


def test_pick_entry_prefers_window_then_web_then_console_or_the_named_file():
    mk = lambda i, k, f="": Entry(id=i, title=i, kind=k, ecosystem="python", argv=["python", f or i], file=f)      # noqa: E731
    es = [mk("c", "console", "c.py"), mk("w", "web", "w.py"), mk("g", "gui", "g.py")]
    assert quick.pick_entry(es).id == "g"
    assert quick.pick_entry(es[:2]).id == "w"
    assert quick.pick_entry(es, "./c.py").id == "c"
    assert quick.pick_entry([]) is None


def test_missing_module_names_the_package():
    assert quick.missing_module("Traceback\nModuleNotFoundError: No module named 'cv2'") == "cv2"
    assert quick.missing_module("No module named 'yaml.nodes'") == "yaml"
    assert quick.missing_module("No module named 'win32api'") == "" and quick.missing_module("fine") == ""
    assert quick.pip_for_module("PIL") == "pillow"


def test_env_plan_is_deterministic_hash_bound_and_rejects_odd_names(tmp_path, monkeypatch):
    monkeypatch.setattr(quick, "_env_dir", lambda tag: tmp_path / tag.split(":")[0])
    o = PreviewOptions()
    a = quick.make_env_plan(o, "python:3.12-slim", ["numpy", "pyyaml", "x; rm -rf /"], "/bin/docker", "docker")
    b = quick.make_env_plan(o, "python:3.12-slim", ["pyyaml", "numpy"], "/bin/docker", "docker")
    assert a.tag == b.tag and a.plan_hash == b.plan_hash and a.tag.startswith("ase-env-")
    text = (tmp_path / a.tag.split(":")[0] / "Dockerfile").read_text()
    assert "FROM python:3.12-slim" in text and "'numpy' 'pyyaml'" in text and "rm -rf" not in text
    assert "numpy" in a.risks[0]
    with pytest.raises(ValueError):
        quick.make_env_plan(o, "python:3.12-slim", [], "/bin/docker", "docker")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stub")
def test_env_image_builds_only_with_its_own_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(quick, "_env_dir", lambda tag: tmp_path / tag.split(":")[0])
    log = tmp_path / "log"
    exe = tmp_path / "docker"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho built\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    plan = quick.make_env_plan(PreviewOptions(), "python:3.12-slim", ["numpy"], str(exe), "docker")
    other = quick.make_env_plan(PreviewOptions(), "python:3.12-slim", ["pyyaml"], str(exe), "docker")
    assert not build_gui_image(plan, approve(other))["ok"] and not log.exists()
    res = build_gui_image(plan, approve(plan))
    assert res["ok"] and "build --tag " + plan.tag in log.read_text()


def _entry(kind="console", file="app.py"):
    return Entry(id="e", title="app", kind=kind, ecosystem="python", argv=["python", file], file=file)


def test_analyse_lists_every_step_and_the_cached_image(tmp_path, monkeypatch):
    monkeypatch.setattr(quick, "_env_dir", lambda tag: tmp_path / "env" / tag.split(":")[0])
    src = _project(tmp_path / "p") if (tmp_path / "p").mkdir() is None else None
    o = PreviewOptions()
    stopped = lambda opts: {"available": False, "backend": "docker", "executable": "/bin/docker", "reason": "off"}   # noqa: E731
    q = quick.analyse(src, [_entry()], o, status_fn=stopped, image_fn=lambda e, i: False)
    assert q.needs_runtime_start and q.needs_env_image and q.packages and q.image.startswith("ase-env-")
    assert any("Start docker" in s for s in q.steps) and any("Install" in s for s in q.steps) and any("Run python app.py" in s for s in q.steps)
    ready = lambda opts: {"available": True, "backend": "docker", "executable": "/bin/docker", "reason": ""}         # noqa: E731
    q2 = quick.analyse(src, [_entry()], o, status_fn=ready, image_fn=lambda e, i: True)
    assert not q2.needs_runtime_start and not q2.needs_env_image and len(q2.steps) == 2          # everything cached: just run and show
    none = quick.analyse(src, [], o, status_fn=ready)
    assert none.problems


def test_analyse_without_a_runtime_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr("app_preview.runtimes.find_runtime", lambda options, prefer=None: (None, None))
    q = quick.analyse(tmp_path, [_entry()], PreviewOptions(), status_fn=lambda o: {"available": False, "backend": None, "executable": None, "reason": ""})
    assert q.problems and "Docker" in q.problems[0]


# ------------------------------------------------------------------ UI
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("No display")
    yield r
    r.destroy()


def _pump(root, view, cond, secs=5):
    import time
    t = time.time()
    while time.time() - t < secs and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


def test_run_now_one_approval_then_prepares_and_starts(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v, quick_run as qr
    monkeypatch.setattr(quick, "_env_dir", lambda tag: tmp_path / "env" / tag.split(":")[0])
    proj = tmp_path / "proj"
    proj.mkdir()
    _project(proj)
    o = PreviewOptions()
    plan = quick.make_env_plan(o, "python:3.12-slim", ["numpy"], "/bin/docker", "docker")
    prep = quick.QuickPrep(entry=_entry(), packages=["numpy"], steps=["Install numpy", "Run python app.py"], needs_env_image=True,
                           env_plan=plan, image=plan.tag)
    monkeypatch.setattr(qr, "analyse", lambda *a, **k: prep)
    built, asked = [], []
    monkeypatch.setattr(qr, "build_gui_image", lambda p, a, progress=None: (built.append((p.tag, a.plan_hash == p.plan_hash)), {"ok": True, "notes": ["ok"]})[1])
    monkeypatch.setattr(qr.messagebox, "askokcancel", lambda *a, **k: asked.append(a[1]) or True)
    view = v.AppPreviewView(root)
    view.pack()
    view.src_var.set(str(proj))
    seen = {}
    monkeypatch.setattr(view, "start", lambda: seen.update(img=view._image_override, ok=view._quick_ok))
    view.run_now()
    _pump(root, view, lambda: "img" in seen)
    assert len(asked) == 1 and "1. Install numpy" in asked[0] and "docker build" in asked[0]
    assert built == [(plan.tag, True)] and seen == {"img": plan.tag, "ok": True}
    assert view.mode_var.get() == "run" and view.entry_var.get().endswith("app")


def test_start_uses_the_prepared_image_and_skips_the_second_dialog(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v
    view = v.AppPreviewView(root)
    view.pack()
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "app.py").write_text("print(1)\n")
    view.src_var.set(str(proj))
    view.source = str(proj)
    e = _entry()
    view.entry_by_title["[console] app"] = e
    view.entry_var.set("[console] app")
    got = {}

    def fake_plan(src, entry, mode, opts, image="", **k):
        got["image"] = image
        return types.SimpleNamespace(display=False, image=image, mode=mode)
    monkeypatch.setattr(v, "make_plan", fake_plan)
    monkeypatch.setattr(v, "runtime_status", lambda o: {"available": True, "backend": "docker", "reason": "", "executable": "/bin/docker"})
    began = []
    monkeypatch.setattr(view, "_begin", lambda plan, opts: began.append(plan))
    view.approver = lambda plan, iso: pytest.fail("a second approval dialog must not appear")
    view._image_override, view._quick_ok = "ase-env-abc:latest", True
    view.start()
    _pump(root, view, lambda: began)
    assert got["image"] == "ase-env-abc:latest" and began and view._image_override == "" and not view._quick_ok


def test_missing_package_is_installed_and_the_run_repeats(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v, quick_run as qr
    monkeypatch.setattr(quick, "_env_dir", lambda tag: tmp_path / "env" / tag.split(":")[0])
    monkeypatch.setattr("app_preview.runtimes.find_runtime", lambda options, prefer=None: ("docker", "/bin/docker"))
    view = v.AppPreviewView(root)
    view.pack()
    view._quick_prep = quick.QuickPrep(entry=_entry(), packages=["numpy"], image="python:3.12-slim")
    built = []
    monkeypatch.setattr(qr, "build_gui_image", lambda p, a, progress=None: (built.append(p.dockerfile_sha256), {"ok": True, "notes": []})[1])
    prompts = []
    monkeypatch.setattr(qr.messagebox, "askyesno", lambda *a, **k: prompts.append(a[1]) or True)
    gone = []
    monkeypatch.setattr(view, "_quick_go", lambda ev, waited=0: gone.append(ev["image"]))
    res = types.SimpleNamespace(status="failed", exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'PIL'")
    view._handle({"type": "ui_result", "res": res})
    _pump(root, view, lambda: gone)
    assert "pillow" in prompts[0] and view._quick_prep.packages == ["numpy", "pillow"] and gone == [view._quick_prep.env_plan.tag] and built
    view._quick_tries = 2
    view._handle({"type": "ui_result", "res": res})                     # no endless loops
    assert len(prompts) == 1


def test_out_of_memory_repeats_once_with_more_memory(root, monkeypatch):
    from ui import app_preview_view as v
    view = v.AppPreviewView(root)
    view.pack()
    view._quick_prep = quick.QuickPrep(entry=_entry(), image="python:3.12-slim")
    view.options.limits = Limits(timeout_s=60, memory_mb=512)
    started = []
    monkeypatch.setattr(view, "start", lambda: started.append(view._image_override))
    view._handle({"type": "ui_result", "res": types.SimpleNamespace(status="failed", exit_code=137, stdout="", stderr="")})
    _pump(root, view, lambda: started)
    assert view.options.limits.memory_mb == 1024 and started == ["python:3.12-slim"]


def test_file_picker_lists_only_the_script_and_its_imports(root, tmp_path):
    from ui import app_preview_view as v
    proj = tmp_path / "AI Chat"
    proj.mkdir()
    (proj / "dupescope.py").write_text("import helper\nprint(1)\n")
    (proj / "helper.py").write_text("X = 1\n")
    (proj / "other.py").write_text("print('x')\n")
    (proj / "big").mkdir()
    (proj / "big" / "huge.py").write_text("y = 2\n")
    view = v.AppPreviewView(root)
    view.pack()
    view.open_file(str(proj / "dupescope.py"))
    _pump(root, view, lambda: view.entry_var.get() and view._focus is not None)
    listed = {i[2:] for i in view.tree.tk.splitlist(view.tree.tk.call(view.tree, "children", "")) if i.startswith("f:")}
    assert view._focus == {"dupescope.py", "helper.py"} and listed == {"dupescope.py", "helper.py"}
    assert "dupescope.py" in view.entry_var.get() and view.src_var.get() == str(proj)
    view.show_whole_folder()
    root.update()
    assert view._focus is None
    # a second pick in the same folder rescans and refocuses
    view.open_file(str(proj / "other.py"))
    _pump(root, view, lambda: view._focus == {"other.py"})
    assert view._focus == {"other.py"} and "other.py" in view.entry_var.get()


def test_named_file_runs_even_when_the_scan_did_not_list_it(tmp_path):
    (tmp_path / "dupescope.py").write_text("print(1)\n")
    ready = lambda o: {"available": True, "backend": "docker", "executable": "/bin/docker", "reason": ""}      # noqa: E731
    gui = Entry(id="g", title="g", kind="gui", ecosystem="python", argv=["python", "g.py"], file="g.py")
    q = quick.analyse(tmp_path, [gui], PreviewOptions(), script="dupescope.py", status_fn=ready, image_fn=lambda e, i: True)
    assert q.entry.argv == ["python", "dupescope.py"]


def test_handover_starts_at_once_without_waiting_for_the_project_scan(root, tmp_path, monkeypatch):
    """Build & Test hands a window program over: it must start immediately, not wait for (or be reset by) a slow listing."""
    from ui import app_preview_view as v
    proj = tmp_path / "AI Chat"
    proj.mkdir()
    (proj / "dupescope.py").write_text("import tkinter\nprint(1)\n")
    (proj / "run.py").write_text("print(2)\n")
    real = v.discover_entries

    def slow(path):
        time.sleep(1.2)
        return real(path)
    import time
    monkeypatch.setattr(v, "discover_entries", slow)
    monkeypatch.setattr(v, "runtime_status", lambda o: {"available": True, "backend": "docker", "reason": "", "executable": "/bin/true", "version": "x"})
    monkeypatch.setattr(v, "image_present", lambda e, i: True)
    shown = []
    view = v.AppPreviewView(root, approver=lambda plan, iso: shown.append(plan) or False)
    view.pack()
    t0 = time.time()
    view.run_script(str(proj), "./dupescope.py")
    _pump(root, view, lambda: shown, secs=3)
    assert shown and time.time() - t0 < 1.0, "the approval must appear before the 1.2 s listing finishes"
    assert any("dupescope.py" in a for a in shown[0].argv)
    _pump(root, view, lambda: view.entries, secs=4)                      # the listing finishes later ...
    assert "dupescope.py" in view.entry_var.get()                       # ... and does not replace the chosen program


def test_project_listing_never_walks_into_a_virtual_environment(root, tmp_path):
    from ui import app_preview_view as v
    proj = tmp_path / "p"
    (proj / "env" / "Lib").mkdir(parents=True)
    (proj / "env" / "pyvenv.cfg").write_text("home = x\n")
    for i in range(200):
        (proj / "env" / "Lib" / f"m{i}.py").write_text("x=1\n")
    (proj / "app.py").write_text("print(1)\n")
    view = v.AppPreviewView(root)
    view.pack()
    view.set_source(str(proj))
    _pump(root, view, lambda: view.names)
    assert view.names == ["app.py"]


def test_start_with_an_empty_entry_box_picks_the_best_program_or_waits_for_the_list(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "run.py").write_text("print(2)\n")
    (proj / "win.py").write_text("import tkinter\nroot = tkinter.Tk()\nroot.mainloop()\n")
    monkeypatch.setattr(v, "runtime_status", lambda o: {"available": True, "backend": "docker", "reason": "", "executable": "/bin/true", "version": "x"})
    monkeypatch.setattr(v, "image_present", lambda e, i: True)
    shown = []
    boxes = []
    monkeypatch.setattr(v.messagebox, "showinfo", lambda *a, **k: boxes.append(a[1]))
    view = v.AppPreviewView(root, approver=lambda plan, iso: shown.append(plan) or False)
    view.pack()
    view.set_source(str(proj))
    view.start()                                                        # pressed while the list is still being read
    assert view._start_when_listed and not boxes
    _pump(root, view, lambda: shown)
    assert shown and not boxes                                           # started by itself once the list was ready: no "Nothing to start" box
    assert any(a.endswith(".py") for a in shown[0].argv)
