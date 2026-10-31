"""Build & Test: programs with a window run on a virtual screen in the GUI image (no Docker needed: parsing + a shell test)."""
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.build_execution import apply_autotune, build_plan, container_command, custom_candidate  # noqa: E402
from plugins.build_execution.gui_screen import GUI_IMAGE, SCRIPT, apply_gui_screen  # noqa: E402
from plugins.build_execution.models import Limits  # noqa: E402
from plugins.build_execution.run_targets import discover_all  # noqa: E402

GUI_FILES = {"main.py": "from pkg.app import App\nif __name__ == '__main__':\n    App().mainloop()\n", "pkg/__init__.py": "",
             "pkg/app.py": "import tkinter as tk\nclass App(tk.Tk):\n    pass\n", "tests/test_a.py": "def test_a():\n    assert True\n",
             "requirements.txt": "pillow\n"}


def _zip(tmp_path, files):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n, t in files.items():
            f.writestr(n, t)
    return str(z)


def test_run_row_of_a_gui_program_is_wrapped_and_uses_the_gui_image(tmp_path):
    z = _zip(tmp_path, GUI_FILES)
    cands, _ = discover_all(z)
    run = next(c for c in cands if c.id == "python:run:main.py")
    c2, image, notes = apply_gui_screen(run, z, "")
    assert image == GUI_IMAGE and notes and c2.argv[:2] == ["sh", "-c"] and c2.argv[-2:] == ["python", "./main.py"]
    assert c2.env["ASE_SMOKE"] == "1" and run.argv == ["python", "./main.py"]            # input not modified
    plan = build_plan(z, c2, image=image)
    assert plan.image == GUI_IMAGE and GUI_IMAGE in " ".join(container_command("docker", plan, "/w", "n"))


def test_pip_run_row_is_tuned_first_then_wrapped(tmp_path):
    z = _zip(tmp_path, dict(GUI_FILES, **{"requirements.txt": "easyocr\n"}))
    cand = custom_candidate(["sh", "-c", "pip install --no-input --target /tmp/deps -r requirements.txt && PYTHONPATH=/tmp/deps python ./main.py"])
    tuned, lim, _ = apply_autotune(cand, Limits(), z)
    c2, image, _ = apply_gui_screen(tuned, z, "")
    assert c2.argv[3] == "ase" and c2.argv[4:6] == ["sh", "-c"] and "/work/.deps" in c2.argv[6] and lim.memory_mb == 3072


def test_tests_of_a_gui_project_run_to_the_end_and_console_is_untouched(tmp_path):
    z = _zip(tmp_path, GUI_FILES)
    pytest_row = next(c for c in discover_all(z)[0] if c.id == "python:pytest")
    c2, image, _ = apply_gui_screen(pytest_row, z, "")
    assert c2.env["ASE_SMOKE"] == "0" and image == GUI_IMAGE
    (tmp_path / "c").mkdir()
    con = _zip(tmp_path / "c", {"main.py": "import os\nif __name__ == '__main__':\n    print(os.name)\n"})
    run = next(c for c in discover_all(con)[0] if c.id == "python:run:main.py")
    assert apply_gui_screen(run, con, "") == (run, "", [])


def test_typed_image_is_kept(tmp_path):
    z = _zip(tmp_path, GUI_FILES)
    c = custom_candidate(["python", "main.py"])
    assert apply_gui_screen(c, z, "my/image:1")[1] == "my/image:1"


@pytest.mark.skipif(os.name != "posix" or Path("/tmp/.X11-unix/X99").exists(), reason="needs a POSIX shell and a free X99 slot")
def test_wrapper_script_smoke_and_exit_codes(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    x = fake / "Xvfb"
    x.write_text("#!/bin/sh\nmkdir -p /tmp/.X11-unix\npython3 -c \"import socket,time;s=socket.socket(socket.AF_UNIX);"
                 "s.bind('/tmp/.X11-unix/X99');time.sleep(20)\"\n")
    x.chmod(x.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", ASE_SMOKE="1", ASE_SMOKE_TICKS="10")

    def run(*cmd, smoke="1"):
        try:
            return subprocess.run(["sh", "-c", SCRIPT, "ase", *cmd], env=dict(env, ASE_SMOKE=smoke), capture_output=True, text=True, timeout=30)
        finally:
            try:
                os.unlink("/tmp/.X11-unix/X99")
            except OSError:
                pass

    r = run("sleep", "5")
    assert r.returncode == 0 and "still running after 1 s" in r.stdout and "DISPLAY" not in r.stderr
    assert run("sh", "-c", "exit 3").returncode == 3
    assert run("sh", "-c", "exit 4", smoke="0").returncode == 4
