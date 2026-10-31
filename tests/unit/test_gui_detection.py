"""App Preview discovery: GUI apps behind a launcher, very long files, launcher names, GUI test suites (read-only parsing)."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_preview.discovery import discover_entries  # noqa: E402
from app_preview.models import PreviewOptions  # noqa: E402
from app_preview.planner import make_plan  # noqa: E402


def _zip(tmp_path, files):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n, t in files.items():
            f.writestr(n, t)
    return str(z)


def test_launcher_that_imports_a_gui_package_gets_an_extra_gui_row(tmp_path):
    z = _zip(tmp_path, {"main.py": "from pkg.app import App\nif __name__ == '__main__':\n    App().mainloop()\n",
                        "pkg/__init__.py": "", "pkg/app.py": "import tkinter as tk\nclass App(tk.Tk):\n    pass\n"})
    entries, _ = discover_entries(z)
    kinds = {e.id: e.kind for e in entries}
    assert kinds["python:main.py"] == "console"                 # the existing row is unchanged
    assert kinds["python:main.py:gui-via-imports"] == "gui"


def test_main_guard_at_the_end_of_a_very_long_file_is_found(tmp_path):
    body = "import tkinter as tk\n" + "# filler line with some text\n" * 9000 + "if __name__ == '__main__':\n    print('x')\n"
    z = _zip(tmp_path, {"bighub.py": body})
    assert len(body) > 200_000
    assert any(e.id == "python:bighub.py:gui" for e in discover_entries(z)[0])


def test_launcher_without_main_guard_is_listed(tmp_path):
    z = _zip(tmp_path, {"run_hub.py": "import hub\nhub.go()\n", "hub.py": "import tkinter\n\ndef go():\n    pass\n"})
    ids = {e.id for e in discover_entries(z)[0]}
    assert "python:run_hub.py" in ids and "python:run_hub.py:gui-via-imports" in ids


def test_plain_console_project_unchanged(tmp_path):
    z = _zip(tmp_path, {"main.py": "import os\nif __name__ == '__main__':\n    print(os.name)\n"})
    ids = [e.id for e in discover_entries(z)[0]]
    assert ids == ["python:main.py"]


def test_gui_project_tests_get_a_virtual_screen_but_console_tests_do_not(tmp_path):
    gui = _zip(tmp_path, {"app.py": "import tkinter\n", "tests/test_a.py": "def test_a():\n    assert True\n"})
    entry = next(e for e in discover_entries(gui)[0] if e.kind == "test")
    plan = make_plan(gui, entry, "test", PreviewOptions())
    assert plan.display and plan.argv[:2] == ["sh", "/ase/ase_gui_entry.sh"] and plan.image == "ase-preview-gui:latest"
    off = make_plan(gui, entry, "test", PreviewOptions(autotune=False))
    assert not off.display
    (tmp_path / "c").mkdir()
    con = _zip(tmp_path / "c", {"app.py": "import os\n", "tests/test_a.py": "def test_a():\n    assert True\n"})
    entry = next(e for e in discover_entries(con)[0] if e.kind == "test")
    assert not make_plan(con, entry, "test", PreviewOptions()).display


def test_console_row_of_a_gui_program_runs_on_the_virtual_screen(tmp_path):
    from app_preview.discovery import custom_entry
    z = _zip(tmp_path, {"main.py": "from pkg.app import App\nif __name__ == '__main__':\n    App().mainloop()\n",
                        "pkg/__init__.py": "", "pkg/app.py": "import tkinter as tk\nclass App(tk.Tk):\n    pass\n"})
    entry = next(e for e in discover_entries(z)[0] if e.id == "python:main.py")
    for mode in ("run", "trace", "debug"):
        plan = make_plan(z, entry, mode, PreviewOptions())
        assert plan.display and plan.image == "ase-preview-gui:latest" and "/ase/ase_gui_entry.sh" in plan.argv
    typed = make_plan(z, custom_entry(["python", "main.py"]), "run", PreviewOptions())
    assert typed.display
    module = make_plan(z, custom_entry(["python", "-m", "pkg"]), "run", PreviewOptions())
    assert module.display
    assert not make_plan(z, entry, "run", PreviewOptions(autotune=False)).display          # opt-out keeps the old plan


def test_console_program_keeps_the_plain_image(tmp_path):
    z = _zip(tmp_path, {"main.py": "import os\nif __name__ == '__main__':\n    print(os.name)\n"})
    entry = discover_entries(z)[0][0]
    plan = make_plan(z, entry, "run", PreviewOptions())
    assert not plan.display and plan.image != "ase-preview-gui:latest"


def test_tk_error_gets_a_hint():
    from run_insights.hints import hints_for
    txt = "ImportError: libtk8.6.so: cannot open shared object file: No such file or directory"
    assert any("App Preview" in h["text"] for h in hints_for("failed", 1, txt))
