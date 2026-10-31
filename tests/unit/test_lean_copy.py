"""Lean copy: a script run copies only what it needs - never the whole folder or archive; the rest on demand."""
import os
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest

from app_preview import quick
from plugins.build_execution import quickprep, storage
from plugins.build_execution.models import Limits
from plugins.build_execution.sandbox import prepare_workspace


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("AISEA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(storage, "_data_dir", lambda: tmp_path / "home" / "data")
    quickprep.clear_includes()
    yield
    quickprep.clear_includes()


def _project(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("import helper\nimport json\ncfg = json.load(open('settings.json'))\nopen('assets/logo.png','rb')\n"
                                 "t = 'templates'\nprint(cfg)\n")
    (root / "helper.py").write_text("import util\n")
    (root / "util.py").write_text("X = 1\n")
    (root / "settings.json").write_text("{}")
    (root / "assets").mkdir()
    (root / "assets" / "logo.png").write_bytes(b"\x89PNG....")
    (root / "templates").mkdir()
    (root / "templates" / "index.html").write_text("<p>hi</p>")
    (root / "other.py").write_text("print('not needed')\n")
    (root / "docs").mkdir()
    (root / "docs" / "big.txt").write_text("x" * 5000)
    (root / "secret").mkdir()
    (root / ".env").write_text("TOKEN=abc")
    (root / "AI Chat").mkdir()
    (root / "AI Chat" / "model.bin").write_bytes(b"0" * 200_000)
    return root


def _copied(dest: Path):
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())


def test_lean_copy_takes_only_the_scripts_needs_even_in_a_small_project(tmp_path):
    proj = _project(tmp_path / "p")
    dest = tmp_path / "ws"
    notes = prepare_workspace(proj, dest, Limits(max_workspace_mb=500), entry="app.py")
    got = _copied(dest)
    assert "app.py" in got and "helper.py" in got and "util.py" in got                 # the import chain
    assert "settings.json" in got and "assets/logo.png" in got and "templates/index.html" in got   # named in the code (file, file, folder)
    assert "other.py" not in got and "docs/big.txt" not in got and "AI Chat/model.bin" not in got and ".env" not in got
    assert notes[0].startswith("Lean copy: only app.py and") and "not copied" in notes[0]
    assert any("Include..." in n for n in notes)


def test_manual_includes_are_added_and_bring_their_imports(tmp_path):
    proj = _project(tmp_path / "p")
    (proj / "plugin.py").write_text("import util\nimport extra_dep\n")
    (proj / "extra_dep.py").write_text("Y = 2\n")
    quickprep.add_includes(proj, ["docs", "plugin.py", "nope/missing.txt"])
    dest = tmp_path / "ws"
    notes = prepare_workspace(proj, dest, Limits(max_workspace_mb=500), entry="app.py")
    got = _copied(dest)
    assert "docs/big.txt" in got and "plugin.py" in got and "extra_dep.py" in got and "other.py" not in got
    assert any("'nope/missing.txt' is not in the project" in n for n in notes)
    assert quickprep.includes_for(proj) == ["docs", "plugin.py", "nope/missing.txt"]
    quickprep.clear_includes(proj)
    assert quickprep.includes_for(proj) == []


def test_lean_copy_also_works_for_a_zip_archive(tmp_path):
    proj = _project(tmp_path / "p")
    z = tmp_path / "proj.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in proj.rglob("*"):
            if f.is_file():
                zf.write(f, "projdir/" + f.relative_to(proj).as_posix())
    dest = tmp_path / "ws"
    notes = prepare_workspace(z, dest, Limits(max_workspace_mb=500), entry="app.py")
    got = _copied(dest)
    assert "app.py" in got and "settings.json" in got and "other.py" not in got and "AI Chat/model.bin" not in got
    assert notes[0].startswith("Lean copy")


def test_switching_lean_copy_off_restores_the_old_behaviour(tmp_path):
    proj = _project(tmp_path / "p")
    storage.save(lean_copy=False)
    dest = tmp_path / "ws"
    prepare_workspace(proj, dest, Limits(max_workspace_mb=500), entry="app.py")
    assert "other.py" in _copied(dest)                                               # small project: full copy as before


def test_big_folder_named_in_the_code_is_not_copied_silently(tmp_path):
    proj = _project(tmp_path / "p")
    (proj / "app.py").write_text("print('data')\nD = 'bigdata'\n")
    (proj / "bigdata").mkdir()
    for i in range(REF := 320):
        (proj / "bigdata" / f"f{i}.txt").write_text("x")
    dest = tmp_path / "ws"
    notes = prepare_workspace(proj, dest, Limits(max_workspace_mb=500), entry="app.py")
    assert not any(g.startswith("bigdata/") for g in _copied(dest)) and any("bigdata/" in n and "Include..." in n for n in notes)


def test_needed_files_reads_the_programs_own_error_text():
    names = ["app.py", "helper.py", "cfg/settings.json", "templates/index.html", "pkg/__init__.py", "pkg/sub.py", "data/a.csv"]
    text = ("FileNotFoundError: [Errno 2] No such file or directory: 'settings.json'\njinja2.exceptions.TemplateNotFound: index.html\n"
            "ModuleNotFoundError: No module named 'pkg.sub'\nFileNotFoundError: [Errno 2] No such file or directory: 'data'\n"
            "FileNotFoundError: [Errno 2] No such file or directory: 'gone.txt'\nModuleNotFoundError: No module named 'numpy'")
    got = quick.needed_files(text, names)
    assert set(got["files"]) == {"cfg/settings.json", "templates/index.html", "pkg/sub.py", "pkg/__init__.py"}
    assert got["dirs"] == ["data"] and got["absent"] == ["gone.txt"]            # numpy is a package to install, not a project file


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


def _pump(root, cond, secs=3):
    t = time.time()
    while time.time() - t < secs and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


def test_failed_run_offers_the_missing_file_then_runs_again(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v, lean_copy as lc
    proj = _project(tmp_path / "p")
    view = v.AppPreviewView(root)
    view.pack()
    view.set_source(str(proj))
    _pump(root, lambda: view.names)
    asked = []
    monkeypatch.setattr(lc.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    started = []
    monkeypatch.setattr(view, "start", lambda: started.append(1))
    res = types.SimpleNamespace(status="failed", exit_code=1, stdout="", stderr="FileNotFoundError: [Errno 2] No such file or directory: 'docs/big.txt'")
    view._handle({"type": "ui_result", "res": res})
    _pump(root, lambda: started)
    assert asked and "docs/big.txt" in asked[0] and started == [1]
    assert quickprep.includes_for(proj) == ["docs/big.txt"] and "(1)" in view.include_btn["text"]
    view._lean_tries = 3                                                           # no endless loop
    view._handle({"type": "ui_result", "res": res})
    assert len(asked) == 1


def test_a_missing_local_module_is_copied_not_pip_installed(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v, lean_copy as lc
    proj = _project(tmp_path / "p")
    (proj / "pkg").mkdir()
    (proj / "pkg" / "__init__.py").write_text("")
    (proj / "pkg" / "sub.py").write_text("Z = 1\n")
    view = v.AppPreviewView(root)
    view.pack()
    view.set_source(str(proj))
    _pump(root, lambda: view.names)
    asked = []
    monkeypatch.setattr(lc.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or False)
    pip_asked = []
    monkeypatch.setattr(view, "_quick_after_result", lambda r: pip_asked.append(1))
    res = types.SimpleNamespace(status="failed", exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'pkg.sub'")
    view._handle({"type": "ui_result", "res": res})
    assert asked and "pkg/sub.py" in asked[0]
    assert pip_asked == [1]                                                        # declined: the normal follow-up still runs


def test_include_dialog_lists_files_and_folders_and_adds_them(root, tmp_path):
    from ui import app_preview_view as v
    proj = _project(tmp_path / "p")
    view = v.AppPreviewView(root)
    view.pack()
    view.set_source(str(proj))
    _pump(root, lambda: view.names)
    view.open_includes()
    dlg = [w for w in view.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    assert "docs/" in dlg.items and "docs/big.txt" in dlg.items and "assets/" in dlg.items
    idx = dlg._shown.index("docs/")
    dlg.lb.selection_set(idx)
    dlg.ok()
    assert quickprep.includes_for(proj) == ["docs"] and "(1)" in view.include_btn["text"]
    dlg.clear()
    assert quickprep.includes_for(proj) == [] and "(" not in view.include_btn["text"]
    dlg.destroy()
